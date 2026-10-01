"""Транспорт 1С-Коннект ↔ существующий ИИ-помощник: личные сообщения («Коллеги»).

Входящие сообщения приводятся к универсальному контракту InboundMessage —
неважно, откуда они пришли:
  * «API приложений» 1С-Коннект (Named Pipe десктопного приложения под
    учёткой бота) — bridge/connect_pipe.py, работает сейчас;
  * HTTP-приёмник POST /connect/colleague/message — для тестов и для будущего
    официального push-механизма, если 1С-Коннект его добавит.
Ответ уходит подтверждённым методом REST SendMessagecolleague
(POST /v1/colleague/send/message/), файлы — SendFileСollegue.

Контракт (наша структура, не формат API 1С-Коннект):
  {"platform": "1c-connect", "event": "message_received", "message_id": "...",
   "sender_id": "...", "recipient_id": "...", "text": "...", "timestamp": "..."}
"""
from __future__ import annotations

import hmac
import logging
from dataclasses import asdict, dataclass
from typing import Callable

from fastapi import APIRouter, Header, HTTPException, Request

from bridge.access import resolve_user
from bridge.answer import split_message
from bridge.assistant import Job, Reply
from bridge.config import Settings
from bridge.connect_client import ConnectClient, ConnectError
from bridge.connect_pipe import ColleagueMessage
from bridge.store import Store

log = logging.getLogger("bridge.connect_adapter")


@dataclass(frozen=True)
class InboundMessage:
    message_id: str
    sender_id: str
    recipient_id: str
    text: str
    timestamp: str = ""
    platform: str = "1c-connect"
    event: str = "message_received"

    @staticmethod
    def from_json(data: dict) -> "InboundMessage":
        missing = [k for k in ("message_id", "sender_id", "text") if not str(data.get(k) or "").strip()]
        if missing:
            raise ValueError("нет обязательных полей: " + ", ".join(missing))
        if data.get("event", "message_received") != "message_received":
            raise ValueError(f"неизвестное событие: {data.get('event')}")
        return InboundMessage(message_id=str(data["message_id"]), sender_id=str(data["sender_id"]),
                              recipient_id=str(data.get("recipient_id") or ""), text=str(data["text"]),
                              timestamp=str(data.get("timestamp") or ""))

    @staticmethod
    def from_pipe(msg: ColleagueMessage, bot_id: str) -> "InboundMessage":
        return InboundMessage(message_id=msg.message_id, sender_id=msg.author_id, recipient_id=bot_id,
                              text=msg.text, timestamp=msg.sent_at)


@dataclass(frozen=True)
class OutboundMessage:
    recipient_id: str
    author_id: str
    text: str


class ConnectAdapter:
    def __init__(self, settings: Settings, store: Store, rest: ConnectClient,
                 submit: Callable[[Job, Callable[[Reply], None]], None]):
        self.settings = settings
        self.store = store
        self.rest = rest
        self.submit = submit

    @property
    def bot_id(self) -> str:
        return self.settings.connect.bot_specialist_id

    # --- вход ---

    def accept(self, msg: InboundMessage) -> str:
        """Принять сообщение. Возвращает статус: accepted | duplicate |
        own_message | wrong_recipient | unknown_user | empty."""
        if msg.sender_id == self.bot_id:
            return "own_message"          # ответы самого бота — без зацикливания
        if msg.recipient_id and self.bot_id and msg.recipient_id != self.bot_id:
            return "wrong_recipient"
        user = resolve_user(self.settings.users_path, msg.sender_id)
        if user is None:
            self.store.log(channel="colleague", user_key=msg.sender_id, user_name=None, base_id=None,
                           question=msg.text[:200], status="unknown_user")
            return "unknown_user"
        if not msg.text.strip():
            return "empty"
        if not self.store.first_time(f"colleague:{msg.message_id}"):
            return "duplicate"            # повторная доставка после переподключения

        def ack() -> None:
            if self.settings.connect.ack_text:
                self.send(OutboundMessage(msg.sender_id, self.bot_id, self.settings.connect.ack_text))

        self.submit(Job(channel="colleague", user=user, text=msg.text.strip(), on_start=ack),
                    lambda reply: self.deliver(msg.sender_id, reply))
        return "accepted"

    def accept_pipe(self, msg: ColleagueMessage) -> None:
        if msg.author_id != msg.colleague_id:
            return                        # сообщение бота, отправленное с другого устройства
        status = self.accept(InboundMessage.from_pipe(msg, self.bot_id))
        log.info("личное сообщение %s от %s: %s", msg.message_id, msg.colleague_id, status)

    # --- выход ---

    def send(self, out: OutboundMessage) -> None:
        self.rest.send_colleague_message(recipient_id=out.recipient_id, author_id=out.author_id, text=out.text)

    def deliver(self, recipient_id: str, reply: Reply) -> None:
        try:
            for part in split_message(reply.text, self.settings.connect.max_message_chars):
                self.send(OutboundMessage(recipient_id, self.bot_id, part))
            for f in reply.files:
                try:
                    self.rest.send_colleague_file(recipient_id=recipient_id, author_id=self.bot_id, path=f)
                except ConnectError as exc:
                    self._log_error(recipient_id, f"файл {f.name}: {exc}")
                    self.send(OutboundMessage(recipient_id, self.bot_id,
                                              f"Файл {f.name} собран, но не отправился. Повторите вопрос позже."))
        except ConnectError as exc:
            self._log_error(recipient_id, str(exc))
            raise

    def _log_error(self, recipient_id: str, error: str) -> None:
        log.error("ответ сотруднику %s не доставлен: %s", recipient_id, error)
        self.store.log(channel="colleague", user_key=recipient_id, user_name=None, base_id=None,
                       question="", status="send_error", error=error[:1000])

    # --- HTTP-приёмник универсального JSON ---

    def router(self) -> APIRouter:
        r = APIRouter()

        @r.post("/connect/colleague/message", status_code=202)
        async def colleague_message(request: Request, authorization: str | None = Header(default=None)) -> dict:
            expected = self.settings.connect.webhook_token
            if not expected:
                raise HTTPException(503, "webhook token is not configured")
            if not authorization or not hmac.compare_digest(authorization, f"Bearer {expected}"):
                raise HTTPException(401, "unauthorized")
            try:
                data = await request.json()
                msg = InboundMessage.from_json(data if isinstance(data, dict) else {})
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
            return {"status": self.accept(msg), "message": asdict(msg)}

        return r
