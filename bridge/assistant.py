"""Обработка одного обращения — общий путь для 1С-Коннект, API-моста и консоли.

Путь вопроса (как на слайде «Путь одного вопроса»):
  1. белый список проверен до этого (линия «ИИ-помощник»);
  2. по ссылке в сообщении находим базу (или берём базу разговора), конфиг MCP — только для неё;
  3. Claude Code продолжает сессию этого сотрудника; что мост ответил сам, без Claude
     (отказы, подключение базы), уходит заметкой в следующий вопрос — разговор не рвётся;
  4. Claude изучает метаданные, пишет запросы, проверяет и считает;
  5. ответ чистится, файлы собираются, всё уходит в чат.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from bridge.access import Base, User, load_bases, load_users
from bridge import docs, onec_extension, onec_toolkit, toolkit_secrets
from bridge.answer import clean_answer
from bridge.claude_runner import ClaudeRunner, RunResult
from bridge.config import REPO_ROOT, Settings
from bridge.mcp_config_builder import build_mcp_config, session_disallowed, session_tools
from bridge.store import Store, now
from common.doc_extract import SUPPORTED_EXTENSIONS, extract_text
from common.doc_index import BuildReport, DocIndex, IndexNotBuiltError
from common.yaml_edit import YamlEditError, set_value

log = logging.getLogger("bridge")
INLINE_ATTACHMENT_CHARS = 20_000
_SAFE_KEY_RE = re.compile(r"[^A-Za-z0-9_-]+")

HELP = (
    "Я ИИ-помощник ГК «Эксперт». Спрашивайте обычным языком.\n"
    "Вопрос по данным 1С — пришлите ссылку на базу, можно сразу с вопросом: "
    "«http://сервер/База почему не проводится поступление №15?». Дальше ссылку повторять не нужно — "
    "я помню разговор и базу.\n"
    "Новая опубликованная база: установите в неё расширение (/расширение) и пришлите ссылку — подключу сам.\n"
    "Вопросы «как сделать» — ищу в базе знаний компании.\n"
    "Проверка контрагента: «проверь контрагента 750710450345» — долг и неподписанные ЭАВР.\n"
    "Можно прислать скриншот, PDF или файл вместе с вопросом.\n"
    "Я помню разговор 8 часов. Вернуться к прошлому (например, вчерашнему) — /продолжить, "
    "список последних — /разговоры.\n"
    "Команды: /базы — ваши базы и ссылки на них, /новый — начать разговор заново, "
    "/инструкция — подробная инструкция файлом, /toolkit — подключить к помощнику свою копию базы."
)


TOOLKIT_HELP = (
    "Как подключить свою копию базы к помощнику:\n"
    "1. В своей копии базы задайте пользователю «Администратор» пароль 1 (именно 1, не рабочий пароль).\n"
    "2. Запустите employee_toolkit.ps1 правой кнопкой → «Выполнить с помощью PowerShell»: он откроет порт для "
    "сервера помощника и создаст ярлык «1С + MCP Toolkit».\n"
    "3. Откройте базу этим ярлыком — MCP Toolkit запустится сам. В первый раз нажмите «Сгенерировать» у поля "
    "«Токен доступа». Если помощник должен исправлять данные — внизу окна, в блоке «Автоматически разрешать операции», "
    "поставьте галочку «Записать» "
    "(после каждого открытия базы). Окно не закрывайте.\n"
    "4. Сообщите администратору помощника адрес из окна скрипта (вида http://192.168.0.51:6003/mcp) и токен — "
    "он внесёт базу в список. Потом пришлите мне этот адрес — и спрашивайте про базу.\n"
    "Подробно, с ответами на частые проблемы — в файле «Инструкция для сотрудников»."
)
EXTENSION_HELP = (
    "Как подключить базу через расширение MCP_HTTPService:\n"
    "1. В базе: Все функции → Стандартные → Управление расширениями конфигурации → «Добавить из файла» "
    "(в конфигурациях на БСП: Администрирование → Печатные формы, отчёты и обработки → Расширения). "
    "Снимите «Безопасный режим», перезапустите 1С.\n"
    "2. У пользователя «Администратор» в этой базе должен быть пароль 1.\n"
    "3. Пришлите мне ссылку на базу (адрес из браузера) — подключу её сам и отвечу. Если скажу, что "
    "HTTP-сервис расширения не опубликован, — в публикации базы на веб-сервере нужно включить «Публиковать "
    "HTTP сервисы расширений по умолчанию» (Конфигуратор → Администрирование → Публикация на веб-сервере)."
)


@dataclass
class Job:
    channel: str                 # connect | api | cli
    user: User
    text: str
    attachments: list[Path] = field(default_factory=list)
    on_start: Callable[[], None] | None = None
    base_id: str | None = None   # база задана явно (API, консоль); в линии — ссылкой в тексте


@dataclass
class Reply:
    text: str
    status: str                  # ok | error | limit | paused | command | need_base | denied | toolkit
    files: list[Path] = field(default_factory=list)


class Assistant:
    KB_CHECK_INTERVAL_SECONDS = 300

    def __init__(self, settings: Settings, store: Store, runner: ClaudeRunner | None = None):
        self.settings = settings
        self.store = store
        self.runner = runner or ClaudeRunner(settings)
        self._kb_checked_at: float | None = None
        self._kb_lock = threading.Lock()
        self.kb_last: dict = {}      # итог последней переиндексации — для панели
        self.toolkit_checks: dict[str, onec_toolkit.ProbeResult] = {}   # база → последняя полная проверка

    def refresh_knowledge_base(self, force: bool = False) -> None:
        """Папку «База знаний» пополняют — индекс догоняет её сам."""
        now_ts = time.monotonic()
        if not force and self._kb_checked_at is not None \
                and now_ts - self._kb_checked_at < self.KB_CHECK_INTERVAL_SECONDS:
            return
        self._kb_checked_at = now_ts
        try:
            self._rebuild_knowledge_base(full=False)
        except Exception:  # noqa: BLE001 — старый индекс лучше, чем отказ отвечать
            log.exception("не удалось обновить индекс базы знаний")

    def rebuild_knowledge_base(self) -> dict:
        """Полная переиндексация по кнопке в панели. Возвращает итог."""
        self._kb_checked_at = time.monotonic()
        self._rebuild_knowledge_base(full=True)
        return self.kb_last

    def _rebuild_knowledge_base(self, *, full: bool) -> BuildReport | None:
        kb = self.settings.knowledge_base
        with self._kb_lock:
            if full and not kb.root.is_dir():
                raise FileNotFoundError(f"нет папки базы знаний: {kb.root}")
            idx = DocIndex(kb.index_path)
            try:
                report = idx.rebuild_from_dir(kb.root, kb.exclude) if full \
                    else idx.rebuild_if_changed(kb.root, kb.exclude)
            finally:
                idx.close()
        if report:
            log.info("база знаний переиндексирована: %d документов", len(report.indexed))
            self.kb_last = {
                "at": now().isoformat(), "indexed": len(report.indexed), "chunks": report.chunks,
                "duplicates": len(report.duplicates), "excluded": len(report.excluded),
                "empty": [Path(p).name for p in report.empty][:20],
                "failed": [f"{Path(p).name}: {e}" for p, e in report.failed][:20],
                "unsupported": [Path(p).name for p in report.unsupported][:20],
            }
        return report

    def knowledge_base_info(self) -> dict:
        kb = self.settings.knowledge_base
        info = {"root": str(kb.root), "root_exists": kb.root.is_dir(), "documents": 0, "built_at": None,
                "products": {}, "last": self.kb_last}
        try:
            idx = DocIndex(kb.index_path, readonly=True)
        except IndexNotBuiltError:
            return info
        try:
            docs = idx.list_documents()
            info["documents"] = len(docs)
            info["built_at"] = idx.built_at()
            for d in docs:
                info["products"][d["product"]] = info["products"].get(d["product"], 0) + 1
        except sqlite3.OperationalError:
            pass                    # индекс как раз создаётся в фоне — таблиц ещё нет
        finally:
            idx.close()
        return info

    def users(self) -> dict[str, User]:
        return load_users(self.settings.users_path)

    def bases(self) -> dict[str, Base]:
        return load_bases(self.settings.bases_path)

    # --- команды ---

    def _command(self, job: Job) -> Reply | None:
        cmd = job.text.strip().split()[0].lower() if job.text.strip().startswith("/") else ""
        if cmd in ("/пауза", "/pause", "/стоп"):
            if not job.user.owner:
                return Reply("Ставить помощника на паузу может только владелец.", "command")
            self.store.set_state("paused", now().isoformat())
            return Reply("Помощник на паузе. Включить: /старт", "command")
        if cmd in ("/старт", "/start", "/resume"):
            if not job.user.owner:
                return Reply("Включать помощника может только владелец.", "command")
            self.store.set_state("paused", None)
            self.store.set_state("limit_until", None)
            return Reply("Помощник снова работает.", "command")
        if cmd in ("/новый", "/new", "/сброс"):
            self.store.reset_session(job.user.key)
            self._clear_pending(job.user.key)
            return Reply("Начинаем разговор заново. Вернуться к прошлому — /продолжить.", "command")
        if cmd in ("/разговоры", "/история", "/history"):
            return self._list_conversations(job)
        if cmd in ("/продолжить", "/continue", "/вернуться"):
            return self._continue(job)
        if cmd in ("/базы", "/bases"):
            mine = [b for b in self.bases().values()
                    if job.user.can_use(b.id) and b.allowed_for(job.user.key) and b.link]
            if not mine:
                return Reply("Подключённых баз пока нет. Опубликованную базу подключите сами: установите в неё "
                             "расширение (/расширение) и пришлите мне ссылку на базу.", "command")
            lines = [f"• {b.title} — {b.link}" for b in mine]
            return Reply("Ваши базы 1С:\n" + "\n".join(lines) + "\n\nЧтобы работать с базой, пришлите ссылку "
                         "на неё — можно сразу с вопросом.", "command")
        if cmd in ("/помощь", "/help"):
            return Reply(HELP, "command")
        if cmd in ("/toolkit", "/тулкит", "/мсп"):
            files = docs.toolkit_files()
            if not files:
                return Reply("Обработка MCP Toolkit на сервере не установлена — попросите администратора выполнить "
                             "scripts/install_1c_skills.ps1.", "command")
            extensions = docs.extension_files()
            text = TOOLKIT_HELP + ("\n\n" + EXTENSION_HELP if extensions else "")
            return Reply(text, "command", files + [self._instruction()] + extensions)
        if cmd in ("/инструкция", "/instruction", "/docs"):
            return Reply("Инструкция для сотрудников — в файле: откройте его в браузере, можно распечатать.",
                         "command", [self._instruction()])
        if cmd in ("/расширение", "/extension"):
            extensions = docs.extension_files()
            if not extensions:
                return Reply("Расширения на сервере пока нет — спросите администратора помощника.", "command")
            return Reply(EXTENSION_HELP, "command", extensions)
        if cmd in ("/id", "/ид"):
            return Reply(f"Ваш user_id: {job.user.key}", "command")
        return None

    def _instruction(self) -> Path:
        return docs.instruction_file(self.settings.data_dir / "docs")

    # --- основной путь ---

    def handle(self, job: Job) -> Reply:
        started = time.monotonic()
        command = self._command(job)
        if command:
            return command
        if self.store.get_state("paused"):
            return self._bridge_reply(job, Reply("Помощник временно выключен. Попробуйте позже.", "paused"))

        limit_until = self.store.get_state("limit_until")
        if limit_until and datetime.fromisoformat(limit_until) > datetime.now():
            # Не повторяем попытку зря: до сброса лимита Claude не запускаем.
            return self._bridge_reply(job, Reply(
                f"Лимит подписки исчерпан. Обновится в {datetime.fromisoformat(limit_until):%H:%M}.", "limit"))

        bases = self.bases()
        base, question = None, job.text.strip()
        if job.base_id:
            base = bases.get(job.base_id)
            if base is None:
                return Reply(f"Не знаю базу «{job.base_id}».", "need_base")
        else:
            linked = self._base_by_link(job, bases, started)
            if isinstance(linked, Reply):
                return self._bridge_reply(job, linked)
            if linked is not None:
                base, question = linked
            else:
                base = self._retry_pending(job, bases, started)
        if base is not None and base.id not in bases:
            bases = self.bases()             # базу только что внесли по ссылке
        if base is not None and not job.user.can_use(base.id):
            self._log(job, base.id, "denied", started)
            return self._bridge_reply(job, Reply(f"У вас нет доступа к базе «{base.title}».", "denied"))
        if not question and not job.attachments:
            if base is None:
                return Reply("Напишите вопрос.", "need_base")
            # Только ссылка — проверяем базу и остаёмся в ней, Claude не запускаем.
            refusal = self._gate(job, base, started)[0]
            if refusal:
                return self._bridge_reply(job, refusal, link=base.link)
            self._clear_pending(job.user.key, base.link)
            self.store.session(job.user.key, base.id, self.settings.idle_timeout_minutes, keep_base=False)
            return self._bridge_reply(job, Reply(self._connected_text(base), "command"))

        session = self.store.session(job.user.key, base.id if base else None,
                                     self.settings.idle_timeout_minutes, keep_base=base is None)
        base = bases.get(session.base_id) if session.base_id else None
        refusal, extra_env = self._gate(job, base, started) if base is not None else (None, {})
        if refusal:
            return self._bridge_reply(job, refusal, link=base.link)
        if base is not None:
            self._clear_pending(job.user.key, base.link)
        if job.on_start:
            job.on_start()
        self.refresh_knowledge_base()

        job_id = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
        workdir = self.settings.data_dir / "work" / (_SAFE_KEY_RE.sub("_", job.user.key) or "user")
        outbox = workdir / "outbox" / job_id
        attach_dir = workdir / "attachments" / job_id
        attach_dir.mkdir(parents=True, exist_ok=True)
        attachments = [Path(shutil.move(str(p), attach_dir / p.name)) for p in job.attachments if p.exists()]

        check = self.toolkit_checks.get(base.id) if base is not None and base.via_toolkit else None
        mcp_config = build_mcp_config(self.settings, base_id=base.id if base else None, outbox=outbox,
                                      attachments=attach_dir, target=workdir / "mcp" / f"{job_id}.json", base=base)
        notes = self.store.notes(job.user.key, self.settings.idle_timeout_minutes)

        def ask(sess, is_new: bool) -> RunResult:
            recap = self._recap(job.user.key) if is_new and sess.recap else []
            return self.runner.run(
                question=self._compose(job.user, base, question, attachments,
                                       notes=[t for _, t in notes[-10:]], recap=recap),
                session_id=sess.session_id, is_new=is_new, cwd=workdir, mcp_config=mcp_config,
                allowed=session_tools(self.settings, base),
                disallowed=session_disallowed(self.settings, base, check.tools if check else ()),
                extra_env=extra_env,
            )

        is_new = self._is_new(session)
        result = ask(session, is_new)
        if not result.has_result and not is_new and result.session_missing:
            # Разговора у Claude нет (удалён, не сохранился) — новый, с напоминанием истории из журнала.
            log.warning("разговор %s не найден у Claude — начинаю новый", session.session_id)
            self.store.restart_session(job.user.key)
            session = self.store.session(job.user.key, base.id if base else None,
                                         self.settings.idle_timeout_minutes, keep_base=base is None)
            result = ask(session, True)
        elif not result.has_result and is_new and result.session_taken:
            result = ask(session, False)    # разговор уже есть — продолжаем его
        if result.has_result and notes:
            self.store.clear_notes(job.user.key, notes[-1][0])
        return self._finish(job, base, question, result, outbox, started, session.session_id)

    TOOLKIT_RECHECK_SECONDS = 900
    PENDING_TRIES = 3            # сколько раз повторить неподключившуюся ссылку на следующих сообщениях
    PENDING_MINUTES = 60

    def _is_new(self, session) -> bool:
        """Есть ли у Claude этот разговор: по транскрипту в профиле бота, если его видно, иначе — по отметке моста."""
        exists = getattr(self.runner, "session_exists", lambda _sid: None)(session.session_id)
        return session.is_new if exists is None else not exists

    def _bridge_reply(self, job: Job, reply: Reply, *, link: str = "") -> Reply:
        """Ответ моста без Claude: Claude узнает о нём из заметки в следующем вопросе,
        а неподключившуюся ссылку мост повторит сам на следующих сообщениях."""
        self.store.add_note(job.user.key, f"Сотрудник написал: «{_short(job.text, 500)}». Ответил мост (без тебя): "
                                          f"«{_short(reply.text, 500)}»")
        if link and reply.status in ("toolkit", "denied"):
            self._set_pending(job, link)
        return reply

    def _pending(self, user_key: str) -> dict | None:
        raw = self.store.get_state(f"pending_link:{user_key}")
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return None

    def _set_pending(self, job: Job, link: str) -> None:
        old = self._pending(job.user.key)
        if old and onec_extension.same_link(old["link"], link):
            old["tries"] += 1
        else:
            old = {"link": link, "question": _short(job.text, 1000), "tries": 0, "at": now().isoformat()}
        self.store.set_state(f"pending_link:{job.user.key}", json.dumps(old, ensure_ascii=False))

    def _clear_pending(self, user_key: str, link: str | None = None) -> None:
        """Забыть неподключившуюся ссылку; с link — только если она ведёт на эту базу (база заработала)."""
        pending = self._pending(user_key)
        if pending and (link is None or onec_extension.same_link(pending["link"], link)):
            self.store.set_state(f"pending_link:{user_key}", None)

    def _retry_pending(self, job: Job, bases: dict[str, Base], started: float) -> Base | None:
        """Сотрудник прислал ссылку, база не подключилась, он что-то поправил и пишет «поставила 1» или
        «подключись» уже без ссылки. Мост сам повторяет подключение по той ссылке — если сотрудник
        тем временем не работает в другой базе."""
        pending = self._pending(job.user.key)
        if not pending:
            return None
        if pending["tries"] >= self.PENDING_TRIES or \
                now() - datetime.fromisoformat(pending["at"]) > timedelta(minutes=self.PENDING_MINUTES):
            self._clear_pending(job.user.key)
            return None
        current = self.store.current(job.user.key, self.settings.idle_timeout_minutes)
        current_base = bases.get(current[1]) if current and current[1] else None
        if current_base is not None and not onec_extension.same_link(current_base.link, pending["link"]):
            return None
        probe_job = Job(channel=job.channel, user=job.user, text=pending["link"])
        linked = self._base_by_link(probe_job, bases, started)   # неудача сама отметит попытку
        base = linked[0] if isinstance(linked, tuple) else None
        refusal = linked if isinstance(linked, Reply) else None
        if linked is None:
            self._set_pending(job, pending["link"])
        elif base is not None and job.user.can_use(base.id):
            refusal = self._gate(job, base, started)[0]
            if refusal is not None:
                self._set_pending(job, pending["link"])
        if base is None or refusal is not None:
            why = refusal.text if refusal else "по ссылке не нашлась база 1С"
            self.store.add_note(job.user.key, f"Мост ещё раз попробовал подключить базу по ссылке {pending['link']} — "
                                              f"не вышло: «{_short(why, 400)}». Объясни сотруднику, что поправить.")
            return None
        self._clear_pending(job.user.key)
        self.store.add_note(job.user.key, f"Раньше сотрудник прислал ссылку {pending['link']} с сообщением "
                                          f"«{_short(pending['question'], 600)}», тогда база не подключилась. Сейчас мост "
                                          f"подключил её сам — база «{base.title}». Если сотрудник ждёт ответа на то "
                                          "сообщение — ответь на него.")
        log.info("база %s подключена повторно по ссылке для %s", base.id, job.user.name)
        return base

    def _recap(self, user_key: str, since: datetime | None = None, until: datetime | None = None) -> list[str]:
        """Последние вопросы и ответы из журнала — новому разговору Claude вместо сломанного."""
        since = since or now() - timedelta(minutes=self.settings.idle_timeout_minutes)
        rows = self.store.dialog(user_key, since, until)
        return [f"— Сотрудник: «{_short(r['question'], 400)}»\n  Ты ответил: «{_short(r['answer'], 700)}»"
                for r in rows]

    def _conversation_list(self, job: Job) -> list[dict]:
        current = self.store.current(job.user.key, self.settings.idle_timeout_minutes)
        return self.store.conversations(job.user.key, limit=5, exclude=current[0] if current else None)

    def _conversation_line(self, n: int, conv: dict) -> str:
        base = self.bases().get(conv["base_id"]) if conv["base_id"] else None
        at = datetime.fromisoformat(conv["last_active"]).astimezone()
        where = f"база «{base.title}»" if base else "без базы"
        return f"{n}. {at:%d.%m %H:%M} · {where} · «{_short(conv['title'] or '', 90)}»"

    def _list_conversations(self, job: Job) -> Reply:
        convs = self._conversation_list(job)
        if not convs:
            return Reply("Прошлых разговоров пока нет.", "command")
        return Reply("Последние разговоры:\n" + "\n".join(self._conversation_line(i + 1, c)
                                                            for i, c in enumerate(convs))
                     + "\n\nВернуться: /продолжить 1 (номер из списка). Просто /продолжить — к последнему.", "command")

    def _continue(self, job: Job) -> Reply:
        """/продолжить [N] — вернуться к прошлому разговору: тот же разговор Claude и та же база."""
        convs = self._conversation_list(job)
        if not convs:
            return Reply("Прошлых разговоров пока нет — просто задайте вопрос.", "command")
        parts = job.text.split()
        n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
        if not 1 <= n <= len(convs):
            return Reply(f"Нет разговора с номером {n}. Список — /разговоры.", "command")
        conv = convs[n - 1]
        exists = getattr(self.runner, "session_exists", lambda _sid: None)(conv["session_id"])
        alive = exists is not False
        self.store.resume_conversation(job.user.key, conv, started=alive)
        self._clear_pending(job.user.key)
        at = datetime.fromisoformat(conv["last_active"]).astimezone()
        if alive:
            self.store.add_note(job.user.key, f"Сотрудник вернулся к этому разговору командой /продолжить "
                                              f"(последнее сообщение было {at:%d.%m в %H:%M}).")
        else:
            history = self._recap(job.user.key, datetime.fromisoformat(conv["started_at"]),
                                  datetime.fromisoformat(conv["last_active"]) + timedelta(seconds=1))
            self.store.add_note(job.user.key, "Сотрудник вернулся к разговору от "
                                              f"{at:%d.%m %H:%M} командой /продолжить. Так он шёл:\n" + "\n".join(history))
        base = self.bases().get(conv["base_id"]) if conv["base_id"] else None
        where = f", база «{base.title}»" if base else ""
        return Reply(f"Вернулся к разговору от {at:%d.%m %H:%M}{where}: «{_short(conv['title'] or '', 120)}». "
                     "Продолжайте — я помню, о чём шла речь.", "command")

    def _gate(self, job: Job, base: Base, started: float) -> tuple[Reply | None, dict[str, str]]:
        if base.via_toolkit:
            return self._toolkit_gate(job, base, started)
        if base.via_extension:
            return self._extension_gate(job, base, started), {}
        return None, {}

    def _toolkit_gate(self, job: Job, base: Base, started: float) -> tuple[Reply | None, dict[str, str]]:
        """База на ПК сотрудника: закреплена ли за ним, есть ли токен, открыта ли,
        и открыта ли под «Администратор»/«1». Всё — до запуска Claude, без траты лимита."""
        def refuse(text: str, status: str = "toolkit") -> tuple[Reply, dict]:
            self._log(job, base.id, status, started, error=text)
            return Reply(text, status), {}

        if not base.allowed_for(job.user.key):
            return refuse(f"База «{base.title}» закреплена за другим сотрудником.", "denied")
        token = toolkit_secrets.get_token(base.id)
        if not token:
            return refuse(f"Для базы «{base.title}» не задан токен MCP Toolkit — внесите его в панели "
                          "управления, раздел «Базы 1С».")
        alive, why = onec_toolkit.health(base.toolkit_url)
        if not alive:
            self.toolkit_checks.pop(base.id, None)
            return refuse(f"База «{base.title}» сейчас недоступна ({why}). Откройте базу ярлыком «1С + MCP Toolkit» "
                          "или в обработке MCP Toolkit нажмите «Запустить сервер», и повторите вопрос.")
        last = self.toolkit_checks.get(base.id)
        if last is not None and last.ok and not onec_toolkit.token_ok(base.toolkit_url, token):
            last = None         # токен сгенерировали заново — полная проверка скажет об этом
        if last is None or not last.ok or time.time() - last.checked_at > self.TOOLKIT_RECHECK_SECONDS:
            last = onec_toolkit.probe(base.toolkit_url, token)
            self.toolkit_checks[base.id] = last
        if not last.ok:
            return refuse(last.message)
        return None, {toolkit_secrets.env_name(base.id): token}

    def _base_by_link(self, job: Job, bases: dict[str, Base], started: float) -> tuple[Base, str] | Reply | None:
        """Ссылка на базу в сообщении — так сотрудник выбирает базу. Уже известная (адрес публикации или
        MCP Toolkit) — берём её; новая опубликованная — проверяем расширение MCP_HTTPService и вносим
        в bases.yaml. Возвращает (база, сообщение без ссылки) или None, если ссылки на базу нет."""
        for raw, rest in onec_extension.find_links(job.text):
            known = next((b for b in bases.values() if b.link and onec_extension.same_link(b.link, raw)), None)
            if known is not None:
                return known, rest
            if onec_extension.is_toolkit_url(raw):
                text = (f"Копии базы по адресу {raw} нет в списке помощника. Передайте этот адрес и токен "
                        "MCP Toolkit администратору помощника — он внесёт базу, и пришлите ссылку ещё раз.")
                self._log(job, None, "toolkit", started, error=text)
                self._set_pending(job, raw)
                return Reply(text, "toolkit")
        found = onec_extension.find_publication(job.text)
        if found is None:
            return None
        url, rest = found
        check = onec_extension.probe(url, timeout=15)
        if not check.ok:
            if not onec_extension.looks_like_1c(url):
                return None             # обычная ссылка в вопросе, не база 1С
            text = (f"Не получилось подключить базу {url}. {check.message} Как поправите — просто напишите мне, "
                    "ссылку повторять не нужно.")
            self._log(job, None, "toolkit", started, error=text)
            self._set_pending(job, url)
            return Reply(text, "toolkit")
        base_id = onec_extension.base_id_for(url, set(bases))
        title = f"{check.config or 'База'} ({url.rstrip('/').split('/')[-1]})"
        try:
            for key, value in (("title", title), ("mode", "extension"), ("url", url),
                               ("owners", []), ("added_by", job.user.name)):
                set_value(self.settings.bases_path, (base_id, key), value)
        except (YamlEditError, OSError) as exc:
            log.exception("не удалось записать базу %s", url)
            return Reply(f"База отвечает, но записать её в список не получилось: {exc}", "error")
        log.info("база %s подключена по ссылке от %s: %s", base_id, job.user.name, url)
        self.toolkit_checks[base_id] = check
        return self.bases()[base_id], rest

    def _connected_text(self, base: Base) -> str:
        check = self.toolkit_checks.get(base.id)
        about = f" Конфигурация {check.config_title or check.config} {check.version}." if check and check.ok else ""
        access = ("Данные в ней я могу исправлять." if base.via_toolkit and base.write
                  else "Базу я только читаю, данные в ней не меняю.")
        return (f"Работаю с базой «{base.title}».{about} Спрашивайте — ссылку повторять не нужно, я помню "
                f"разговор. {access} Другая база — пришлите ссылку на неё, /новый — начать заново.")

    def _extension_gate(self, job: Job, base: Base, started: float) -> Reply | None:
        """Опубликованная база с расширением MCP_HTTPService: отвечает ли, пускает ли «Администратор»/«1»."""
        def refuse(text: str, status: str = "toolkit") -> Reply:
            self._log(job, base.id, status, started, error=text)
            return Reply(text, status)

        if not base.allowed_for(job.user.key):
            return refuse(f"База «{base.title}» закреплена за другим сотрудником.", "denied")
        last = self.toolkit_checks.get(base.id)
        if last is None or not last.ok or time.time() - last.checked_at > self.TOOLKIT_RECHECK_SECONDS:
            last = onec_extension.probe(base.url)
            self.toolkit_checks[base.id] = last
        if not last.ok:
            return refuse(f"База «{base.title}» сейчас недоступна. {last.message}")
        return None

    def _compose(self, user: User, base: Base | None, question: str, attachments: list[Path], *,
                 notes: list[str] = (), recap: list[str] = ()) -> str:
        parts = [f"Сотрудник: {user.name}",
                 f"База 1С: {base.title}" if base else "База 1С не выбрана (только база знаний).",
                 "", "Вопрос:", question or "(без текста — см. вложения)"]
        if notes:
            parts[2:2] = ["", "Перед этим в чате — без тебя (это видел сотрудник, но не ты):",
                          *[f"- {n}" for n in notes]]
        if recap:
            parts[2:2] = ["", "Предыдущий разговор прервался из-за сбоя. Последние вопросы и твои ответы в нём:", *recap]
        check = self.toolkit_checks.get(base.id) if base is not None else None
        if check is not None and base.via_toolkit:
            parts[2:2] = [f"Подключение: MCP Toolkit {base.toolkit_url}, пользователь {check.user}, конфигурация "
                          f"{check.config_title or check.config} {check.version}. "
                          + ("Запись в базу разрешена (execute_code)." if base.write else "Только чтение.")]
        elif check is not None and base.via_extension:
            parts[2:2] = [f"Подключение: опубликованная база {base.url}, {check.detail}, конфигурация "
                          f"{check.config} {check.version}. Только чтение: менять данные в этой базе нельзя."]
        for p in attachments:
            parts.append("")
            if p.suffix.lower() in SUPPORTED_EXTENSIONS:
                try:
                    text = extract_text(p)
                except Exception as exc:  # noqa: BLE001 — битое вложение не должно ронять ответ
                    text = f"(не удалось прочитать: {type(exc).__name__})"
                cut = text[:INLINE_ATTACHMENT_CHARS]
                more = " (обрезано; полностью — view_attachment)" if len(text) > len(cut) else ""
                parts += [f"Вложение «{p.name}»{more}:", cut]
            else:
                parts.append(f"Вложение «{p.name}» — картинка или файл: открой через view_attachment.")
        return "\n".join(parts)

    def _finish(self, job: Job, base: Base | None, question: str, result: RunResult,
                outbox: Path, started: float, session_id: str) -> Reply:
        base_id = base.id if base else None
        if result.has_result:
            # Разговор у Claude есть — следующий вопрос его продолжит (и после ошибки тоже).
            self.store.mark_started(job.user.key, session_id, base_id, question or job.text,
                                    result.session_total_cost, ok=not result.is_error)
        if result.limit_hit:
            until = result.limit_reset
            if until:
                self.store.set_state("limit_until", until.isoformat())
            self._log(job, base_id, "limit", started, error=result.error)
            return Reply(f"Лимит подписки исчерпан. Обновится в {until:%H:%M}. Разговор я помню — потом просто "
                         "напишите «продолжай»." if until else "Лимит подписки исчерпан. Попробуйте позже.", "limit")

        cost = self.store.add_cost(job.user.key, result.session_total_cost) \
            if result.session_total_cost is not None else None
        files = sorted(p for p in outbox.iterdir() if p.is_file()) if outbox.is_dir() else []
        if result.is_error or not result.has_result:
            self._log(job, base_id, "error", started, cost=cost, error=result.error or "нет ответа")
            if result.subtype == "error_max_turns":
                return Reply("Задача оказалась длинной: я не успел закончить за отведённое число шагов. "
                             "Напишите «продолжай» — продолжу с того же места.", "error", files)
            if result.timed_out:
                return Reply(f"Ответ занял больше {self.settings.claude.timeout_seconds // 60} минут, и я остановился. "
                             "Напишите «продолжай» — продолжу с того же места, или уточните задачу.", "error", files)
            if self.store.note_failure(job.user.key) >= 2:
                # Две ошибки подряд в одном разговоре — он, похоже, сломан: следующий вопрос начнёт новый
                # в той же базе, с напоминанием последних вопросов и ответов.
                self.store.restart_session(job.user.key)
            return Reply("Не получилось ответить: произошла ошибка. Попробуйте ещё раз "
                         "или переформулируйте вопрос.", "error", files)

        text = clean_answer(result.text)
        if not text and not files:
            text = "Ответ получился пустым. Попробуйте переформулировать вопрос."
        self._log(job, base_id, "ok", started, cost=cost, answer_chars=len(text), files=len(files),
                  question=question, answer=text)
        return Reply(text, "ok", files)

    def _log(self, job: Job, base_id: str | None, status: str, started: float, *, cost: float | None = None,
             error: str | None = None, answer_chars: int = 0, files: int = 0, question: str | None = None,
             answer: str | None = None) -> None:
        self.store.log(channel=job.channel, user_key=job.user.key, user_name=job.user.name, base_id=base_id,
                       question=question if question is not None else job.text, status=status,
                       answer_chars=answer_chars, files=files,
                       duration_ms=int((time.monotonic() - started) * 1000), cost_usd=cost, error=error,
                       answer=answer)


def _short(text: str | None, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"
