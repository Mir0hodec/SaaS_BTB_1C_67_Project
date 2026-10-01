"""Обработка одного обращения — общий путь для 1С-Коннект, API-моста и консоли.

Путь вопроса (как на слайде «Путь одного вопроса»):
  1. белый список проверен до этого (канал);
  2. по хэштегу находим базу, конфиг MCP — только для неё;
  3. Claude Code продолжает сессию этого сотрудника;
  4. Claude изучает метаданные, пишет запросы, проверяет и считает;
  5. ответ чистится, файлы собираются, всё уходит в чат.
"""
from __future__ import annotations

import logging
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from bridge.access import Base, User, load_bases, load_users, pick_base
from bridge.answer import clean_answer
from bridge.claude_runner import ClaudeRunner, RunResult
from bridge.config import Settings
from bridge.mcp_config_builder import allowed_tools, build_mcp_config
from bridge.store import Store, now
from common.doc_extract import SUPPORTED_EXTENSIONS, extract_text
from common.doc_index import DocIndex

log = logging.getLogger("bridge")
INLINE_ATTACHMENT_CHARS = 20_000
_SAFE_KEY_RE = re.compile(r"[^A-Za-z0-9_-]+")

HELP = (
    "Я ИИ-помощник ГК «Эксперт». Спрашивайте обычным языком.\n"
    "Вопрос по данным 1С — укажите базу хэштегом: «#демо остатки по кассе на 1 сентября».\n"
    "Дальше можно без хэштега: «а теперь по месяцам в Excel» — я помню разговор.\n"
    "Вопросы «как сделать» — ищу в базе знаний компании.\n"
    "Проверка контрагента: «проверь контрагента 750710450345» — долг и неподписанные ЭАВР.\n"
    "Можно прислать скриншот, PDF или файл вместе с вопросом.\n"
    "Команды: /базы — список баз, /новый — начать разговор заново."
)


@dataclass
class Job:
    channel: str                 # connect | api | cli
    user: User
    text: str
    attachments: list[Path] = field(default_factory=list)
    on_start: Callable[[], None] | None = None


@dataclass
class Reply:
    text: str
    status: str                  # ok | error | limit | paused | command | need_base | denied
    files: list[Path] = field(default_factory=list)


class Assistant:
    KB_CHECK_INTERVAL_SECONDS = 300

    def __init__(self, settings: Settings, store: Store, runner: ClaudeRunner | None = None):
        self.settings = settings
        self.store = store
        self.runner = runner or ClaudeRunner(settings)
        self._kb_checked_at: float | None = None

    def refresh_knowledge_base(self, force: bool = False) -> None:
        """Папку «База знаний» пополняют — индекс догоняет её сам."""
        now_ts = time.monotonic()
        if not force and self._kb_checked_at is not None \
                and now_ts - self._kb_checked_at < self.KB_CHECK_INTERVAL_SECONDS:
            return
        self._kb_checked_at = now_ts
        kb = self.settings.knowledge_base
        try:
            idx = DocIndex(kb.index_path)
            try:
                report = idx.rebuild_if_changed(kb.root, kb.exclude)
            finally:
                idx.close()
            if report:
                log.info("база знаний переиндексирована: %d документов", len(report.indexed))
        except Exception:  # noqa: BLE001 — старый индекс лучше, чем отказ отвечать
            log.exception("не удалось обновить индекс базы знаний")

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
            return Reply("Начинаем разговор заново.", "command")
        if cmd in ("/базы", "/bases"):
            lines = [f"#{b.tags[1] if len(b.tags) > 1 else b.id} — {b.title}"
                     for b in self.bases().values() if job.user.can_use(b.id)]
            return Reply("Доступные базы:\n" + "\n".join(lines) if lines else "Вам не открыта ни одна база 1С.",
                         "command")
        if cmd in ("/помощь", "/help"):
            return Reply(HELP, "command")
        if cmd in ("/id", "/ид"):
            return Reply(f"Ваш user_id: {job.user.key}", "command")
        return None

    # --- основной путь ---

    def handle(self, job: Job) -> Reply:
        started = time.monotonic()
        command = self._command(job)
        if command:
            return command
        if self.store.get_state("paused"):
            return Reply("Помощник временно выключен. Попробуйте позже.", "paused")

        limit_until = self.store.get_state("limit_until")
        if limit_until and datetime.fromisoformat(limit_until) > datetime.now():
            # Не повторяем попытку зря: до сброса лимита Claude не запускаем.
            return Reply(f"Лимит подписки исчерпан. Обновится в {datetime.fromisoformat(limit_until):%H:%M}.",
                         "limit")

        bases = self.bases()
        choice = pick_base(job.text, bases)
        if choice.tag and choice.base is None:
            known = ", ".join(f"#{b.tags[1] if len(b.tags) > 1 else b.id}" for b in bases.values()
                              if job.user.can_use(b.id))
            return Reply(f"Не знаю базу «#{choice.tag}». Доступные: {known or 'нет'}.", "need_base")
        if choice.base and not job.user.can_use(choice.base.id):
            self._log(job, choice.base.id, "denied", started)
            return Reply(f"У вас нет доступа к базе «{choice.base.title}».", "denied")
        question = choice.text
        if not question and not job.attachments:
            return Reply("Напишите вопрос после хэштега базы.", "need_base")

        session = self.store.session(job.user.key, choice.base.id if choice.base else None,
                                     self.settings.idle_timeout_minutes, keep_base=choice.base is None)
        base = bases.get(session.base_id) if session.base_id else None
        if job.on_start:
            job.on_start()
        self.refresh_knowledge_base()

        job_id = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
        workdir = self.settings.data_dir / "work" / (_SAFE_KEY_RE.sub("_", job.user.key) or "user")
        outbox = workdir / "outbox" / job_id
        attach_dir = workdir / "attachments" / job_id
        attach_dir.mkdir(parents=True, exist_ok=True)
        attachments = [Path(shutil.move(str(p), attach_dir / p.name)) for p in job.attachments if p.exists()]

        mcp_config = build_mcp_config(self.settings, base_id=base.id if base else None, outbox=outbox,
                                      attachments=attach_dir, target=workdir / "mcp" / f"{job_id}.json")
        result = self.runner.run(
            question=self._compose(job.user, base, question, attachments),
            session_id=session.session_id, is_new=session.is_new, cwd=workdir,
            mcp_config=mcp_config,
            allowed=allowed_tools(base.id if base else None, self.settings.counterparty_check_enabled),
        )
        return self._finish(job, base, question, result, outbox, started)

    def _compose(self, user: User, base: Base | None, question: str, attachments: list[Path]) -> str:
        parts = [f"Сотрудник: {user.name}",
                 f"База 1С: {base.title}" if base else "База 1С не выбрана (только база знаний).",
                 "", "Вопрос:", question or "(без текста — см. вложения)"]
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
                outbox: Path, started: float) -> Reply:
        base_id = base.id if base else None
        if result.limit_hit:
            until = result.limit_reset
            if until:
                self.store.set_state("limit_until", until.isoformat())
            self._log(job, base_id, "limit", started, error=result.error)
            return Reply(f"Лимит подписки исчерпан. Обновится в {until:%H:%M}." if until
                         else "Лимит подписки исчерпан. Попробуйте позже.", "limit")

        cost = self.store.add_cost(job.user.key, result.session_total_cost) \
            if result.session_total_cost is not None else None
        files = sorted(p for p in outbox.iterdir() if p.is_file()) if outbox.is_dir() else []
        if result.is_error:
            # Сломанную сессию не продолжаем — следующий вопрос начнёт новую.
            self.store.reset_session(job.user.key)
            self._log(job, base_id, "error", started, cost=cost, error=result.error)
            return Reply("Не получилось ответить: произошла ошибка. Попробуйте ещё раз "
                         "или переформулируйте вопрос.", "error", files)

        text = clean_answer(result.text)
        if not text and not files:
            text = "Ответ получился пустым. Попробуйте переформулировать вопрос."
        self._log(job, base_id, "ok", started, cost=cost, answer_chars=len(text), files=len(files),
                  question=question)
        return Reply(text, "ok", files)

    def _log(self, job: Job, base_id: str | None, status: str, started: float, *, cost: float | None = None,
             error: str | None = None, answer_chars: int = 0, files: int = 0, question: str | None = None) -> None:
        self.store.log(channel=job.channel, user_key=job.user.key, user_name=job.user.name, base_id=base_id,
                       question=question if question is not None else job.text, status=status,
                       answer_chars=answer_chars, files=files,
                       duration_ms=int((time.monotonic() - started) * 1000), cost_usd=cost, error=error)
