"""Справочник сотрудников 1С-Коннект: SOAP PartnerWebAPI2.SpecialistRead.

Нужен панели управления (найти сотрудника по ФИО, а не по UUID) и журналу
(имя вместо «Сотрудник»). Ответ — таблица значений 1С: колонки в <column>,
строки в <row>, пустое значение — пустой или самозакрытый <Value/>.

Справочник кэшируется в SQLite моста на сутки: метод вызывается редко.
Телефон и дату рождения не храним — панели они не нужны.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from typing import Callable

import requests

from bridge.connect_history import SOAP_URL
from bridge.store import Store

log = logging.getLogger("bridge.directory")

SOAP_ACTION = "http://buhphone.com/PartnerWebAPI2#PartnerWebAPI2:SpecialistRead"
CACHE_KEY = "directory"
CACHE_TTL_SECONDS = 24 * 3600


class DirectoryError(Exception):
    pass


@dataclass(frozen=True)
class Specialist:
    id: str
    surname: str = ""
    name: str = ""
    secondary_name: str = ""
    post: str = ""
    email: str = ""
    deleted: bool = False

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.surname, self.name, self.secondary_name) if p) or self.id

    @property
    def short_name(self) -> str:
        """«Бубнова Юлия» — для журнала и подписи в чате."""
        return " ".join(p for p in (self.surname, self.name) if p) or self.full_name


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_specialists(xml_bytes: bytes) -> list[Specialist]:
    root = ET.fromstring(xml_bytes)
    code = ""
    for el in root.iter():
        if _local(el.tag) == "faultstring":
            raise DirectoryError(f"SOAP fault: {(el.text or '').strip()[:300]}")
        if _local(el.tag) == "Property" and el.get("name") == "ResultCode":
            value = next((c for c in el if _local(c.tag) == "Value"), None)
            code = (value.text or "").strip() if value is not None else ""
    if code and code != "SUCCESS":
        raise DirectoryError(f"ResultCode={code}")
    # Колонки: <column><Name>SpecialistID</Name>…</column> — берём Name только внутри column.
    columns = []
    for col in root.iter():
        if _local(col.tag) == "column":
            name = next((c for c in col if _local(c.tag) == "Name"), None)
            columns.append((name.text or "").strip() if name is not None else "")
    people: list[Specialist] = []
    for row in root.iter():
        if _local(row.tag) != "row":
            continue
        values = [(v.text or "").strip() for v in row if _local(v.tag) == "Value"]
        rec = dict(zip(columns, values))
        sid = rec.get("SpecialistID", "").lower()
        if not sid:
            continue
        people.append(Specialist(
            id=sid, surname=rec.get("Surname", ""), name=rec.get("Name", ""),
            secondary_name=rec.get("SecondaryName", ""), post=rec.get("Post", ""), email=rec.get("EMail", ""),
            deleted=rec.get("Deleted", "").lower() == "true"))
    return people


def fetch_specialists(login: str, password: str, *, url: str = SOAP_URL, timeout: int = 60) -> list[Specialist]:
    body = ('<?xml version="1.0" encoding="utf-8"?><soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"'
            ' xmlns:par="http://buhphone.com/PartnerWebAPI2"><soap:Body><par:SpecialistRead><par:Params/>'
            '</par:SpecialistRead></soap:Body></soap:Envelope>')
    r = requests.post(url, data=body.encode("utf-8"), auth=(login, password), timeout=timeout,
                      headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": f'"{SOAP_ACTION}"'})
    if r.status_code != 200:
        raise DirectoryError(f"HTTP {r.status_code}: {r.text[:300]}")
    return parse_specialists(r.content)


class Directory:
    """Кэш справочника в Store; обновляется не чаще раза в сутки или по кнопке."""

    def __init__(self, store: Store, fetch: Callable[[], list[Specialist]] | None):
        self.store = store
        self.fetch = fetch
        self._lock = threading.Lock()
        self._people: dict[str, Specialist] | None = None
        self._loaded_at: float = 0.0

    def _load_cache(self) -> None:
        if self._people is not None:
            return
        raw = self.store.get_state(CACHE_KEY)
        if raw:
            try:
                data = json.loads(raw)
                self._people = {p["id"]: Specialist(**p) for p in data.get("people", [])}
                self._loaded_at = float(data.get("loaded_at", 0))
                return
            except (ValueError, TypeError, KeyError):
                log.warning("кэш справочника повреждён — будет загружен заново")
        self._people, self._loaded_at = {}, 0.0

    @property
    def loaded_at(self) -> float:
        with self._lock:
            self._load_cache()
            return self._loaded_at

    def refresh(self) -> int:
        """Загрузить справочник из 1С-Коннект. Возвращает число сотрудников."""
        if self.fetch is None:
            raise DirectoryError("не заданы логин и пароль API 1С-Коннект")
        people = self.fetch()
        with self._lock:
            self._people = {p.id: p for p in people}
            self._loaded_at = time.time()
            self.store.set_state(CACHE_KEY, json.dumps(
                {"loaded_at": self._loaded_at, "people": [asdict(p) for p in people]}, ensure_ascii=False))
        log.info("справочник 1С-Коннект загружен: %d сотрудников", len(people))
        return len(people)

    def ensure_fresh(self) -> None:
        """Обновить, если кэш старше суток. Ошибку только пишем в лог."""
        if self.fetch is None or time.time() - self.loaded_at < CACHE_TTL_SECONDS:
            return
        try:
            self.refresh()
        except (requests.RequestException, DirectoryError, ET.ParseError) as exc:
            log.warning("справочник 1С-Коннект не загружен: %s", exc)

    def all(self) -> list[Specialist]:
        with self._lock:
            self._load_cache()
            return sorted(self._people.values(), key=lambda p: p.full_name.lower())

    def get(self, specialist_id: str) -> Specialist | None:
        with self._lock:
            self._load_cache()
            return self._people.get((specialist_id or "").lower())

    def name(self, specialist_id: str) -> str | None:
        p = self.get(specialist_id)
        return p.short_name if p else None
