"""Приём личных сообщений сотрудников через «API приложений» 1С-Коннект.

Протокол (документация 1С-Коннект, раздел 5.1, и эталонный клиент из неё —
github.com/ros-tel/1c-connect-pipe):
  * десктопное приложение 1С-Коннект, запущенное на этой машине под учётной
    записью бота («ГК Эксперт | ИИ-помощник»), открывает Named Pipe
    \\\\.\\pipe\\BuhphoneAgentAPI2_<логин учётной записи>;
  * входящий пакет: 2 байта длины (little-endian) + 2 нулевых байта, затем
    XML в UTF-16LE — <Event …> или <CommandResult …>;
  * команда пишется как XML в UTF-8 без заголовка;
  * по умолчанию приходят только события смены статуса — на входящие
    сообщения коллег нужно подписаться командой EventSubscribe.

Раздел помечен в документации как DEPRECATED: это единственный официальный
способ получать личные сообщения мгновенно, замены в публичном API нет
(см. «Техническое заключение по 1С-Коннект.docx»). Отвечаем через REST
(/v1/colleague/send/message/) — он актуальный.

Пока запрос бота выполняется, в канал ничего не пишется: команда подписки
отправляется только сразу после подключения, дальше канал только читается.
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from threading import Event as StopFlag
from typing import BinaryIO, Callable

log = logging.getLogger("bridge.pipe")

PIPE_PREFIX = "\\\\.\\pipe\\BuhphoneAgentAPI2_"
SUBSCRIBE = ('<Command ID="subscribe-colleague-messages" Action="EventSubscribe" '
             'Mode="Colleagues" Object="Message" Initiator="Incoming"></Command>').encode("utf-8")


class PipeProtocolError(Exception):
    pass


@dataclass(frozen=True)
class ColleagueMessage:
    message_id: str
    colleague_id: str    # с кем чат — сотрудник, написавший боту
    author_id: str
    text: str
    sent_at: str


def encode_packet(xml_text: str) -> bytes:
    """Пакет в формате десктопного приложения (для тестов и отладки)."""
    body = xml_text.encode("utf-16-le")
    if len(body) > 0xFFFF:
        raise PipeProtocolError("пакет длиннее 65535 байт")
    return bytes([len(body) & 0xFF, len(body) >> 8, 0, 0]) + body


def _read_exact(stream: BinaryIO, n: int) -> bytes:
    chunks, got = [], 0
    while got < n:
        chunk = stream.read(n - got)
        if not chunk:
            raise EOFError("канал закрыт")
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def read_packet(stream: BinaryIO) -> str:
    header = _read_exact(stream, 4)
    size = header[0] | (header[1] << 8)
    if header[2:] != b"\x00\x00" or size < 2 or size % 2:
        raise PipeProtocolError(f"неверный заголовок пакета: {header.hex()}")
    return _read_exact(stream, size).decode("utf-16-le")


def parse_colleague_message(xml_text: str) -> ColleagueMessage | None:
    """Входящее сообщение от коллеги или None для прочих событий."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise PipeProtocolError(f"не XML: {exc}") from None
    if root.tag != "Event" or root.get("Object") != "Message" or root.get("Mode") != "Colleagues":
        return None
    if (root.get("Initiator") or "").lower() != "incoming":
        return None

    def tag(name: str) -> str:
        return (root.findtext(name) or "").strip()

    message_id, colleague_id = tag("MessageID"), tag("ColleagueID")
    if not message_id or not colleague_id:
        return None
    return ColleagueMessage(message_id=message_id, colleague_id=colleague_id,
                            author_id=tag("AuthorID") or colleague_id,
                            text=root.findtext("MessageBody") or "", sent_at=tag("Sended") or root.get("Time", ""))


def _open_pipe(path: str) -> BinaryIO:
    return open(path, "r+b", buffering=0)


def listen(login: str, on_message: Callable[[ColleagueMessage], None], stop: StopFlag, *,
           open_pipe: Callable[[str], BinaryIO] = _open_pipe,
           retry_min: float = 5.0, retry_max: float = 60.0) -> None:
    """Слушать канал до stop.set(). Обрыв, закрытое приложение, битый пакет —
    переподключение с растущей паузой; после подключения — повторная подписка."""
    path = PIPE_PREFIX + login
    delay = retry_min
    while not stop.is_set():
        try:
            stream = open_pipe(path)
        except OSError as exc:
            log.warning("нет канала %s (%s): запущено ли 1С-Коннект под учёткой бота? повтор через %.0f с",
                        path, exc, delay)
            stop.wait(delay)
            delay = min(delay * 2, retry_max)
            continue
        log.info("подключено к 1С-Коннект: %s", path)
        try:
            stream.write(SUBSCRIBE)
            stream.flush()
            delay = retry_min
            while not stop.is_set():
                packet = read_packet(stream)
                try:
                    message = parse_colleague_message(packet)
                except PipeProtocolError as exc:
                    log.warning("пропущен пакет: %s", exc)
                    continue
                if message is not None:
                    on_message(message)
                elif packet.lstrip().startswith("<CommandResult"):
                    log.info("ответ 1С-Коннект на подписку: %s", packet[:300])
        except (OSError, EOFError, PipeProtocolError) as exc:
            log.warning("канал 1С-Коннект прерван (%s), переподключение через %.0f с", exc, delay)
        finally:
            try:
                stream.close()
            except OSError:
                pass
        stop.wait(delay)
        delay = min(delay * 2, retry_max)
