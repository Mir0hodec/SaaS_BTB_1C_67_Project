"""Мост 1С-Коннект ↔ ИИ-помощник + API-мост для внутренних сервисов.

Запуск:  python scripts/start.py  (или uvicorn bridge.app:app --host 127.0.0.1 --port 8010)

channel: colleague (по умолчанию) — личные сообщения учётной записи бота,
  транспорт в bridge/connect_adapter.py (приём через «API приложений»,
  ответ через REST SendMessagecolleague);
channel: line — линия поддержки, вебхук /connect/hook.
Входящее сразу ставится в очередь; один рабочий поток отвечает по очереди.
"""
from __future__ import annotations

import base64
import hmac
import logging
import queue
import sys
import threading
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

from bridge import connect_history, connect_pipe  # noqa: E402
from bridge.access import User, resolve_user  # noqa: E402
from bridge.connect_adapter import ConnectAdapter  # noqa: E402
from bridge.connect_pipe import ColleagueMessage  # noqa: E402
from bridge.answer import split_message  # noqa: E402
from bridge.assistant import Assistant, Job, Reply  # noqa: E402
from bridge.config import Settings  # noqa: E402
from bridge.connect_client import ConnectClient, ConnectError  # noqa: E402
from bridge.store import Store  # noqa: E402

log = logging.getLogger("bridge")

TEXT_MESSAGE = 1
FILE_MESSAGE = 70


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
                    task.deliver(self.assistant.handle(job))
            except Exception:  # noqa: BLE001 — один сбой не должен остановить очередь
                log.exception("ошибка обработки вопроса")
                try:
                    task.deliver(Reply("Не получилось ответить: внутренняя ошибка. Попробуйте ещё раз.", "error"))
                except Exception:  # noqa: BLE001
                    log.exception("не удалось сообщить об ошибке")
            finally:
                self.queue.task_done()


class AskRequest(BaseModel):
    question: str
    session: str = "default"


MessageSource = Callable[[Callable[[ColleagueMessage], None], threading.Event], None]


def create_app(settings: Settings | None = None, *, connect: ConnectClient | None = None,
               assistant: Assistant | None = None, colleague_source: MessageSource | None = None) -> FastAPI:
    settings = settings or Settings.load()
    store = Store(settings.data_dir / "bridge.sqlite3")
    assistant = assistant or Assistant(settings, store)
    cs = settings.connect
    connect = connect or ConnectClient(cs.api_base_url, cs.login, cs.password, timeout=cs.request_timeout_seconds)
    worker = Worker(assistant)
    stop_listening = threading.Event()

    def start_colleague_listener() -> None:
        source = colleague_source
        if source is None and cs.receive == "history":
            if not (cs.bot_specialist_id and cs.login and cs.password):
                log.error("приём из истории: нужны CONNECT_BOT_SPECIALIST_ID и логин/пароль API — не запущен")
                return
            client = connect_history.HistoryClient(cs.login, cs.password, timeout=cs.request_timeout_seconds)
            source = lambda on_msg, stop: connect_history.poll(  # noqa: E731
                cs.bot_specialist_id, cs.history_colleagues, on_msg, stop, fetch=client.fetch,
                interval=cs.history_interval_seconds, hours=cs.history_hours)
        elif source is None:
            if not cs.agent_login:
                log.error("режим личных сообщений: не задан %s — приём сообщений не запущен", cs.agent_login_env)
                return
            source = lambda on_msg, stop: connect_pipe.listen(cs.agent_login, on_msg, stop)  # noqa: E731
        threading.Thread(target=source, args=(adapter.accept_pipe, stop_listening),
                         name="connect-colleague-listener", daemon=True).start()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        threading.Thread(target=assistant.refresh_knowledge_base, kwargs={"force": True}, daemon=True).start()
        worker.start()
        if cs.channel == "colleague":
            start_colleague_listener()
        yield
        stop_listening.set()
        worker.stop()

    app = FastAPI(title="ИИ-помощник ГК «Эксперт»", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.worker = worker
    app.state.store = store

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

    # --- личные сообщения сотрудников учётной записи бота ---

    adapter = ConnectAdapter(settings, store, connect,
                             submit=lambda job, deliver: worker.submit(Task(job=job, deliver=deliver)))
    app.include_router(adapter.router())
    app.state.adapter = adapter

    # --- линия поддержки (channel: line) ---

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
        if event.get("event_type") != "line" or event.get("message_type") not in (TEXT_MESSAGE, FILE_MESSAGE):
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

        deliver = lambda reply: send_reply(user_id, reply)  # noqa: E731
        if event["message_type"] == TEXT_MESSAGE:
            text = str(event.get("text") or "").strip()
            if not text:
                return
            worker.submit(Task(job=None, deliver=deliver, prepare=lambda: Job(
                channel="connect", user=user, text=text,
                attachments=store.take_pending_files(user.key), on_start=ack(user_id))))
            return

        file_info = event.get("file") or {}
        worker.submit(Task(job=None, deliver=deliver,
                           prepare=lambda: _prepare_file_job(user, file_info, deliver, ack(user_id))))

    def _prepare_file_job(user: User, info: dict, deliver: Callable[[Reply], None],
                          on_start: Callable[[], None] | None) -> Job | None:
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
        comment = str(info.get("comment") or "").strip()
        if not comment:
            store.add_pending_file(user.key, dest)
            deliver(Reply(f"Файл «{name}» получил. Напишите вопрос к нему.", "command"))
            return None
        return Job(channel="connect", user=user, text=comment,
                   attachments=[*store.take_pending_files(user.key), dest], on_start=on_start)

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
        worker.submit(Task(job=Job(channel="api", user=user, text=body.question), deliver=done.set_result))
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
