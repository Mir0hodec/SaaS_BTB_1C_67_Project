"""Мост 1С-Коннект ↔ ИИ-помощник + API-мост для внутренних сервисов.

Запуск:  python scripts/start.py  (или uvicorn bridge.app:app --host 127.0.0.1 --port 8010)

Сотрудники пишут в свою линию «ИИ-помощник»: 1С-Коннект присылает каждое
сообщение вебхуком на /connect/hook (туннель и SetHook поднимает scripts/start.py),
ответ уходит REST SendMessageLine от имени учётной записи бота.
Сообщения сотрудника, пришедшие подряд (скриншот и текст к нему — два события),
собираются в один вопрос (Batcher); дальше очередь, один рабочий поток отвечает по очереди.
"""
from __future__ import annotations

import base64
import hmac
import logging
import queue
import sys
import threading
import time
from concurrent.futures import Future
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.win_utf8 import ensure_utf8_stdio  # noqa: E402

ensure_utf8_stdio()

from fastapi import FastAPI, Header, HTTPException, Request  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from bridge.connect_directory import Directory, fetch_specialists  # noqa: E402
from bridge.panel import panel_router  # noqa: E402
from bridge.access import User, resolve_user  # noqa: E402
from bridge.answer import split_message  # noqa: E402
from bridge.assistant import Assistant, Job, Reply  # noqa: E402
from bridge.config import Settings  # noqa: E402
from bridge.connect_client import ConnectClient, ConnectError  # noqa: E402
from bridge.store import Store  # noqa: E402

log = logging.getLogger("bridge")

TEXT_MESSAGE = 1
FILE_MESSAGE = 70
FOLLOWUP_SECONDS = 180      # файл без подписи в течение 3 минут после вопроса — к этому вопросу


@dataclass
class Task:
    job: Job | None
    deliver: Callable[[Reply], None]
    prepare: Callable[[], Job | None] | None = None   # долгие действия до вопроса (скачать вложение)


class Worker:
    def __init__(self, assistant: Assistant):
        self.assistant = assistant
        self.queue: queue.Queue[Task | None] = queue.Queue()
        self.thread = threading.Thread(target=self._loop, name="assistant-worker", daemon=True)
        self.current: dict | None = None   # на чей вопрос сейчас отвечает — для панели

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.queue.put(None)
        self.thread.join(timeout=5)

    def submit(self, task: Task) -> int:
        self.queue.put(task)
        return self.queue.qsize()

    def _loop(self) -> None:
        while (task := self.queue.get()) is not None:
            try:
                job = task.prepare() if task.prepare else task.job
                if job is not None:
                    self.current = {"user_key": job.user.key, "user_name": job.user.name,
                                    "text": job.text[:300], "started": time.time()}
                    task.deliver(self.assistant.handle(job))
            except Exception:  # noqa: BLE001 — один сбой не должен остановить очередь
                log.exception("ошибка обработки вопроса")
                try:
                    task.deliver(Reply("Не получилось ответить: внутренняя ошибка. Попробуйте ещё раз.", "error"))
                except Exception:  # noqa: BLE001
                    log.exception("не удалось сообщить об ошибке")
            finally:
                self.current = None
                self.queue.task_done()


class Batcher:
    """Сообщения одного сотрудника, пришедшие с паузой меньше window секунд, — один вопрос.

    1С-Коннект присылает картинку и подпись к ней отдельными событиями, и текст бывает первым:
    без склейки бот отвечал «ничего не приложено», а файл ждал «напишите вопрос к нему».
    Команда (/новый, /продолжить…) не склеивается с соседними сообщениями."""

    def __init__(self, window: float, submit: Callable[[User, list[str], list[dict]], None]):
        self.window = window
        self.submit = submit
        self._lock = threading.Lock()
        self._open: dict[str, dict] = {}

    def add(self, user: User, *, text: str = "", file: dict | None = None) -> None:
        if text.startswith("/"):
            self.flush(user.key)
            self.submit(user, [text], [])
            return
        with self._lock:
            batch = self._open.setdefault(user.key, {"user": user, "texts": [], "files": [], "timer": None})
            if text:
                batch["texts"].append(text)
            if file:
                batch["files"].append(file)
            if batch["timer"]:
                batch["timer"].cancel()
            if self.window > 0:
                batch["timer"] = threading.Timer(self.window, self._fire, args=(user.key,))
                batch["timer"].daemon = True
                batch["timer"].start()
        if self.window <= 0:
            self._fire(user.key)

    def _fire(self, key: str) -> None:
        with self._lock:
            batch = self._open.pop(key, None)
            if batch and batch["timer"]:
                batch["timer"].cancel()
        if batch:
            self.submit(batch["user"], batch["texts"], batch["files"])

    def flush(self, key: str | None = None) -> None:
        """Отдать накопленное сразу: перед командой, при остановке, в тестах."""
        with self._lock:
            keys = [key] if key else list(self._open)
        for k in keys:
            self._fire(k)


class AskRequest(BaseModel):
    question: str
    session: str = "default"
    base: str = ""                     # код базы из bases.yaml; пусто — по ссылке в вопросе


def create_app(settings: Settings | None = None, *, connect: ConnectClient | None = None,
               assistant: Assistant | None = None) -> FastAPI:
    settings = settings or Settings.load()
    store = Store(settings.data_dir / "bridge.sqlite3")
    assistant = assistant or Assistant(settings, store)
    cs = settings.connect
    connect = connect or ConnectClient(cs.api_base_url, cs.login, cs.password, timeout=cs.request_timeout_seconds,
                                       bot_as_spec=cs.line_hook != "bot")
    worker = Worker(assistant)
    last_text: dict[str, float] = {}       # когда сотрудник последний раз задал вопрос — для досланных файлов
    directory = Directory(store, (lambda: fetch_specialists(cs.login, cs.password))
                          if cs.login and cs.password else None)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        threading.Thread(target=assistant.refresh_knowledge_base, kwargs={"force": True}, daemon=True).start()
        worker.start()
        threading.Thread(target=directory.ensure_fresh, daemon=True).start()
        yield
        batcher.flush()
        worker.stop()

    app = FastAPI(title="ИИ-помощник ГК «Эксперт»", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.worker = worker
    app.state.store = store
    app.state.started_at = time.time()

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "queue": worker.queue.qsize(), "paused": bool(store.get_state("paused"))}

    # --- 1С-Коннект ---

    def send_reply(user_id: str, reply: Reply) -> None:
        for part in split_message(reply.text, cs.max_message_chars):
            connect.send_message(line_id=cs.line_id, user_id=user_id, author_id=cs.bot_specialist_id, text=part)
        for f in reply.files:
            try:
                connect.send_file(line_id=cs.line_id, user_id=user_id, author_id=cs.bot_specialist_id, path=f)
            except ConnectError as exc:
                log.error("не удалось отправить файл %s: %s", f.name, exc)
                connect.send_message(line_id=cs.line_id, user_id=user_id, author_id=cs.bot_specialist_id,
                                     text=f"Файл {f.name} собран, но не отправился. Повторите вопрос позже.")

    def ack(user_id: str) -> Callable[[], None] | None:
        if not cs.ack_text:
            return None
        return lambda: connect.send_message(line_id=cs.line_id, user_id=user_id,
                                            author_id=cs.bot_specialist_id, text=cs.ack_text)

    app.include_router(panel_router(settings=settings, store=store, assistant=assistant, worker=worker,
                                    directory=directory, started_at=time.time()))

    # --- линия «ИИ-помощник» ---

    @app.post("/connect/hook")
    async def connect_hook(request: Request, authorization: str | None = Header(default=None)) -> dict:
        expected = cs.webhook_token
        if not expected:
            raise HTTPException(503, "webhook token is not configured")
        if not authorization or not hmac.compare_digest(authorization, f"Bearer {expected}"):
            raise HTTPException(401, "unauthorized")
        try:
            event = await request.json()
        except ValueError:
            raise HTTPException(400, "bad json") from None
        handle_event(event if isinstance(event, dict) else {})
        return {"status": "ok"}

    def handle_event(event: dict) -> None:
        if event.get("event_type") == "line" and event.get("message_type") not in (TEXT_MESSAGE, FILE_MESSAGE):
            # Служебные события линии (80, 81, 83…): что в них приходит — только имена полей, без значений.
            log.info("линия: событие %s, поля: %s", event.get("message_type"), ", ".join(sorted(event)))
            return
        if event.get("event_type") != "line":
            return
        if cs.line_id and event.get("line_id") != cs.line_id:
            return
        user_id, message_id = event.get("user_id"), event.get("message_id")
        if not user_id or event.get("author_id") != user_id or not message_id:
            return   # сообщения специалистов и самого бота
        user = resolve_user(settings.users_path, user_id)
        if user is None:
            # Чужим бот молча не отвечает — даже «доступ запрещён» не пишет.
            store.log(channel="connect", user_key=user_id, user_name=None, base_id=None,
                      question=str(event.get("text") or "")[:200], status="unknown_user")
            return
        if not store.first_time(message_id):
            return   # повторная доставка того же события

        # Линия с внешним сервером обработки: на каждое сообщение — ответ в течение 30 секунд,
        # иначе 1С-Коннект передаст обращение специалисту. Поэтому «Принял…» — сразу, не из очереди.
        quick_ack = ack(user_id)
        if quick_ack is not None:
            threading.Thread(target=_safe(quick_ack), daemon=True).start()
        if event["message_type"] == TEXT_MESSAGE:
            text = str(event.get("text") or "").strip()
            if text:
                batcher.add(user, text=text)
            return
        batcher.add(user, file=event.get("file") or {})

    def submit_batch(user: User, texts: list[str], files: list[dict]) -> None:
        deliver = lambda reply: send_reply(user.key, reply)  # noqa: E731
        worker.submit(Task(job=None, deliver=deliver, prepare=lambda: _prepare_batch(user, texts, files, deliver)))

    batcher = Batcher(cs.batch_seconds, submit_batch)
    app.state.batcher = batcher

    def _safe(fn: Callable[[], None]) -> Callable[[], None]:
        def run() -> None:
            try:
                fn()
            except Exception:  # noqa: BLE001 — «Принял» не отправился: ответ всё равно придёт
                log.exception("линия: не удалось отправить «Принял»")
        return run

    def _download(user: User, info: dict, deliver: Callable[[Reply], None]) -> Path | None:
        name = Path(str(info.get("file_name") or "вложение")).name
        url = info.get("file_path")
        if not url:
            return None
        dest = settings.data_dir / "incoming" / user.key / f"{info.get('file_id', 'file')}_{name}"
        try:
            connect.download(url, dest, max_bytes=cs.max_attachment_mb * 1_000_000,
                             auth_hosts=cs.file_download_hosts)
        except ConnectError as exc:
            deliver(Reply(f"Не смог получить файл «{name}»: {exc}.", "error"))
            return None
        return dest

    def _prepare_batch(user: User, texts: list[str], files: list[dict],
                       deliver: Callable[[Reply], None]) -> Job | None:
        texts = list(texts)
        got = []
        for info in files:
            path = _download(user, info, deliver)
            if path is not None:
                got.append(path)
                texts.append(str(info.get("comment") or "").strip())
        text = "\n".join(t for t in texts if t).strip()
        if not text:
            if not got:
                return None
            if time.time() - last_text.get(user.key, 0) <= FOLLOWUP_SECONDS:
                # Скриншот дошёл уже после вопроса — разбираем его вместе с этим вопросом.
                return Job(channel="connect", user=user, attachments=[*store.take_pending_files(user.key), *got],
                           text="(Сотрудник дослал файл к своему предыдущему сообщению — посмотри его в контексте "
                                "этого сообщения и ответь заново, если файл что-то меняет.)")
            for path in got:
                store.add_pending_file(user.key, path)
            names = ", ".join(f"«{p.name.split('_', 1)[-1]}»" for p in got)
            deliver(Reply(f"{'Файлы' if len(got) > 1 else 'Файл'} {names} получил. Напишите вопрос к "
                          f"{'ним' if len(got) > 1 else 'нему'}.", "command"))
            return None
        last_text[user.key] = time.time()
        return Job(channel="connect", user=user, text=text, attachments=[*store.take_pending_files(user.key), *got])

    # --- API-мост ---

    @app.post("/api/ask")
    def api_ask(body: AskRequest, authorization: str | None = Header(default=None)) -> dict:
        client = next((c for c in settings.api_clients
                       if c.token and authorization and hmac.compare_digest(authorization, f"Bearer {c.token}")),
                      None)
        if client is None:
            raise HTTPException(401, "unauthorized")
        user = User(key=f"api-{client.name}-{body.session}"[:120], name=f"API {client.name}",
                    bases="*" if client.bases == "*" else tuple(client.bases))
        done: Future[Reply] = Future()
        worker.submit(Task(job=Job(channel="api", user=user, text=body.question, base_id=body.base or None),
                           deliver=done.set_result))
        reply = done.result(timeout=settings.claude.timeout_seconds + 120)
        return {
            "status": reply.status,
            "answer": reply.text,
            "files": [{"name": f.name, "content_base64": base64.b64encode(f.read_bytes()).decode("ascii")}
                      for f in reply.files],
        }

    return app


def _lazy_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return create_app()


def __getattr__(name: str):
    # `uvicorn bridge.app:app` — приложение создаётся при первом обращении,
    # чтобы импорт модуля в тестах не требовал config/settings.yaml.
    if name == "app":
        globals()["app"] = _lazy_app()
        return globals()["app"]
    raise AttributeError(name)
