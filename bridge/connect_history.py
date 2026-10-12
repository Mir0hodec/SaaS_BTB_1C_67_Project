"""Приём личных сообщений через SOAP PartnerWebAPI2.GetHistoryOfColleaguesChat.

Новое приложение «1C-Connect Desktop» не открывает канал «API приложений»
(bridge/connect_pipe.py), поэтому на таком сервере остаётся опрос истории —
так же работал старый бот C:\\connect-bot.

Ответ метода — ZIP в base64, внутри по HTML на пару собеседников и день:
  ChatHistory/ChatHistory_Collegue_<id1>_<id2>_<ГГГГ-ММ-ДД>.html
  <p>…<b>Имя 1</b><br>…<b>Имя 2</b><br>…</p>
  <table>… <td colspan=3><b>2026-10-04</b></td> …
           <tr><td>Автор</td><td>Текст</td><td>16:30:10</td></tr> …

Specialist2ID обязателен (без него — PARAM_NOT_EXIST), то есть запрос отдаёт
переписку одной пары. Лимит API — 100 запросов в час днём (OUT_OF_LIMIT
«limit reached»), его делят все, кто опрашивает этой учёткой API. Поэтому
запросы идут не чаще interval секунд по очереди по парам из colleagues:
задержка ответа — до interval × число сотрудников.

Идентификатора сообщения в истории нет — он собирается из пары, даты, времени,
автора и текста. При первом опросе после запуска всё, что уже было в истории,
запоминается без ответа (как у старого бота).
"""
from __future__ import annotations

import base64
import hashlib
import io
import logging
import re
import threading
import time
import zipfile
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from html import unescape
from threading import Event as StopFlag
from typing import Callable, Iterable

import requests

from bridge.connect_pipe import ColleagueMessage

log = logging.getLogger("bridge.history")

SOAP_URL = "https://cus.1c-connect.com/cus/ws/PartnerWebAPI2"
SOAP_ACTION = "http://buhphone.com/PartnerWebAPI2#PartnerWebAPI2:GetHistoryOfColleaguesChat"
OUT_OF_LIMIT = "OUT_OF_LIMIT"

_FILE_RE = re.compile(r"ChatHistory_Collegue_([0-9a-fA-F-]{36})_([0-9a-fA-F-]{36})_")
_HEADER_NAMES_RE = re.compile(r"<p>(.*?)</p>", re.S | re.I)
_BOLD_RE = re.compile(r"<b>(.*?)</b>", re.S | re.I)
_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.S | re.I)
_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIME_RE = re.compile(r"\d{2}:\d{2}:\d{2}")


class HistoryError(Exception):
    pass


class LimitReached(HistoryError):
    pass


def _text(fragment: str) -> str:
    fragment = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    fragment = re.sub(r"<[^>]+>", "", fragment)
    lines = [line.strip() for line in unescape(fragment).replace("\r", "").split("\n")]
    return "\n".join(lines).strip()


def message_id(pair: tuple[str, str], date: str, time: str, author_id: str, text: str) -> str:
    raw = "|".join([*sorted(pair), date, time, author_id, text])
    return "h-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def parse_chat_html(file_name: str, html: str, bot_id: str) -> list[ColleagueMessage]:
    """Сообщения одного файла истории. Сообщения бота тоже возвращаются
    (author_id == bot_id) — их отсекает приёмник."""
    ids = _FILE_RE.search(file_name)
    if not ids:
        raise HistoryError(f"неизвестное имя файла истории: {file_name}")
    id1, id2 = ids.group(1).lower(), ids.group(2).lower()
    header = _HEADER_NAMES_RE.search(html)
    names = [_text(b) for b in _BOLD_RE.findall(header.group(1))] if header else []
    if len(names) < 2:
        raise HistoryError(f"в {file_name} нет имён собеседников")
    by_name = {names[0]: id1, names[1]: id2}
    colleague = id2 if id1 == bot_id.lower() else id1

    messages, date = [], ""
    for row in _ROW_RE.findall(html):
        cells = _CELL_RE.findall(row)
        if len(cells) == 1:
            found = _DATE_RE.search(_text(cells[0]))
            date = found.group(0) if found else date
            continue
        if len(cells) < 3:
            continue
        author, text, time = _text(cells[0]), _text(cells[1]), _text(cells[2])
        if not _TIME_RE.fullmatch(time) or not text:
            continue
        author_id = by_name.get(author)
        if author_id is None:
            log.warning("автор «%s» не совпал с именами в заголовке %s — сообщение пропущено", author, file_name)
            continue
        messages.append(ColleagueMessage(
            message_id=message_id((id1, id2), date, time, author_id, text), colleague_id=colleague,
            author_id=author_id, text=text, sent_at=f"{date}T{time}" if date else time))
    return messages


def parse_response(xml_bytes: bytes, bot_id: str) -> list[ColleagueMessage]:
    root = ET.fromstring(xml_bytes)
    props: dict[str, str] = {}
    blobs: list[bytes] = []
    for el in root.iter():
        if el.tag.endswith("faultstring"):
            raise HistoryError(f"SOAP fault: {(el.text or '').strip()[:300]}")
        if el.tag.endswith("Property") and el.get("name"):
            value = next((c for c in el if c.tag.endswith("Value")), None)
            if value is not None and value.text:
                props[el.get("name")] = value.text.strip()
    code = props.get("ResultCode", "")
    if code == OUT_OF_LIMIT:
        raise LimitReached(props.get("ResultData", "limit reached"))
    for value in props.values():
        if len(value) < 100:
            continue
        try:
            blob = base64.b64decode("".join(value.split()), validate=True)
        except ValueError:
            continue
        if blob.startswith(b"PK"):
            blobs.append(blob)
    messages: list[ColleagueMessage] = []
    for blob in blobs:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            for name in sorted(z.namelist()):
                if name.lower().endswith(".html"):
                    messages += parse_chat_html(name, z.read(name).decode("utf-8", "ignore"), bot_id)
    if code and code not in ("SUCCESS", "OK", "0"):
        # PARAM_NOT_EXIST, ACCESS_DENIED («нет доступа к просмотру переписки» — права учётки API) и т.п.
        raise HistoryError(f"ResultCode={code} {props.get('ResultData', '')[:200]}".strip())
    return messages


def _prop(name: str, typ: str, value: str) -> str:
    return (f'<Property name="{name}" xmlns="http://v8.1c.ru/8.1/data/core">'
            f'<Value xsi:type="xs:{typ}">{value}</Value></Property>')


def build_request(bot_id: str, colleague_id: str, period_from: datetime, period_to: datetime) -> bytes:
    props = (_prop("PeriodFrom", "dateTime", period_from.strftime("%Y-%m-%dT%H:%M:%S"))
             + _prop("PeriodTo", "dateTime", period_to.strftime("%Y-%m-%dT%H:%M:%S"))
             + _prop("Specialist1ID", "string", bot_id))
    if colleague_id:
        props += _prop("Specialist2ID", "string", colleague_id)
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"'
            ' xmlns:par="http://buhphone.com/PartnerWebAPI2" xmlns:xs="http://www.w3.org/2001/XMLSchema"'
            ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><soap:Body>'
            f'<par:GetHistoryOfColleaguesChat><par:Params>{props}</par:Params>'
            '</par:GetHistoryOfColleaguesChat></soap:Body></soap:Envelope>').encode("utf-8")


class HistoryClient:
    def __init__(self, login: str, password: str, *, url: str = SOAP_URL, timeout: int = 60):
        self.url, self.auth, self.timeout = url, (login, password), timeout

    def fetch(self, bot_id: str, colleague_id: str, hours: float) -> bytes:
        now = datetime.now()
        r = requests.post(self.url, data=build_request(bot_id, colleague_id, now - timedelta(hours=hours), now),
                          auth=self.auth, timeout=self.timeout,
                          headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": f'"{SOAP_ACTION}"'})
        if r.status_code != 200:
            raise HistoryError(f"HTTP {r.status_code}: {r.text[:300]}")
        return r.content


@dataclass
class TargetStatus:
    """Как читается переписка с одним сотрудником — для панели управления."""
    colleague_id: str
    state: str = "waiting"       # waiting | ok | limit | access_denied | error
    error: str = ""
    last_poll_at: float | None = None
    last_ok_at: float | None = None
    messages: int = 0            # сообщений в окне истории при последнем чтении
    last_message_at: str = ""    # время последнего сообщения сотрудника (из истории)


class HistoryStatus:
    """Состояние опроса истории: по сотрудникам и запросы к API за последний час."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.targets: dict[str, TargetStatus] = {}
        self.calls: deque[float] = deque()
        self.limit_hits: deque[float] = deque()
        self.running = False
        self.interval = 0.0
        self.next_poll_at: float | None = None

    def _trim(self, now: float) -> None:
        for q in (self.calls, self.limit_hits):
            while q and now - q[0] > 3600:
                q.popleft()

    def record(self, target: str, state: str, *, error: str = "", messages: list[ColleagueMessage] | None = None,
               counted: bool = True) -> None:
        now = time.time()
        with self._lock:
            t = self.targets.setdefault(target, TargetStatus(target))
            t.state, t.error, t.last_poll_at = state, error, now
            if counted:
                self.calls.append(now)
            if state == "limit":
                self.limit_hits.append(now)
            if messages is not None:
                t.last_ok_at, t.messages = now, len(messages)
                own = [m.sent_at for m in messages if m.author_id == m.colleague_id]
                t.last_message_at = max(own) if own else t.last_message_at
            self._trim(now)

    def sync_targets(self, targets: list[str]) -> None:
        with self._lock:
            for t in targets:
                self.targets.setdefault(t, TargetStatus(t))
            for gone in set(self.targets) - set(targets):
                del self.targets[gone]

    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            self._trim(now)
            return {
                "running": self.running,
                "interval": self.interval,
                "next_poll_in": max(0.0, self.next_poll_at - now) if self.next_poll_at else None,
                "calls_last_hour": len(self.calls),
                "limit_hits_last_hour": len(self.limit_hits),
                "targets": [asdict(t) for t in self.targets.values()],
            }


def _classify(exc: Exception) -> str:
    text = str(exc)
    return "access_denied" if "ACCESS_DENIED" in text else "error"


def poll(bot_id: str, colleagues: Iterable[str] | Callable[[], Iterable[str]],
         on_message: Callable[[ColleagueMessage], None], stop: StopFlag, *,
         fetch: Callable[[str, str, float], bytes], interval: float | Callable[[], float] = 75.0,
         hours: float = 6.0, status: HistoryStatus | None = None) -> None:
    """Опрашивать историю до stop.set(): один запрос раз в interval секунд,
    по очереди по парам «бот — сотрудник из colleagues».

    colleagues и interval можно передать функциями — тогда список сотрудников
    и частота подхватываются на лету (панель управления меняет их без перезапуска).
    Новый сотрудник при первом чтении «засевается»: то, что уже было в истории,
    запоминается без ответа."""
    get_targets = colleagues if callable(colleagues) else (lambda fixed=list(colleagues): fixed)
    get_interval = interval if callable(interval) else (lambda fixed=interval: fixed)
    status = status or HistoryStatus()
    status.running = True
    seeded: set[str] = set()
    known: set[str] = set()
    limit_logged_at: datetime | None = None
    empty_logged = False
    i = 0
    try:
        while not stop.is_set():
            targets = [t.lower() for t in get_targets()]
            wait = max(0.0, float(get_interval()))
            status.interval = wait
            status.sync_targets(targets)
            seeded &= set(targets)    # убранный и снова добавленный сотрудник засевается заново
            if not targets:
                if not empty_logged:
                    log.error("история 1С-Коннект: список сотрудников пуст (connect.history_colleagues) — "
                              "жду, пока его заполнят")
                    empty_logged = True
                idle = min(5.0, wait) or 0.01
                status.next_poll_at = time.time() + idle
                stop.wait(idle)
                continue
            empty_logged = False
            target = targets[i % len(targets)]
            i += 1
            try:
                messages = parse_response(fetch(bot_id, target, hours), bot_id)
            except LimitReached as exc:
                status.record(target, "limit", error=str(exc))
                if limit_logged_at is None or datetime.now() - limit_logged_at > timedelta(minutes=30):
                    log.warning("история 1С-Коннект: лимит запросов исчерпан (%s) — жду", exc)
                    limit_logged_at = datetime.now()
            except (requests.RequestException, HistoryError, ET.ParseError, zipfile.BadZipFile) as exc:
                status.record(target, _classify(exc), error=str(exc)[:300])
                log.warning("история 1С-Коннект (%s): %s", target, exc)
            else:
                status.record(target, "ok", messages=messages)
                if target not in seeded:
                    seeded.add(target)
                    known.update(m.message_id for m in messages)
                    log.info("история 1С-Коннект (%s): при запуске уже было %d сообщений — без ответа",
                             target, len(messages))
                else:
                    for m in messages:
                        if m.message_id not in known:
                            known.add(m.message_id)
                            try:
                                on_message(m)
                            except Exception:  # noqa: BLE001 — одно сообщение не должно остановить приём
                                log.exception("не удалось принять сообщение %s", m.message_id)
            status.next_poll_at = time.time() + wait
            stop.wait(wait)
    finally:
        status.running = False
        status.next_poll_at = None
