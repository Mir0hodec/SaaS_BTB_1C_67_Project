"""Внутренняя линия «ИИ-помощник» в 1С-Коннекте — сообщения боту приходят мгновенно, вебхуком.

    python scripts/connect_line_setup.py             создать линию (если её ещё нет), дать компетенцию
                                                     учётке бота, подключить сотрудников, вписать line_id
    python scripts/connect_line_setup.py status      линии организации: какая с ботом, где вебхук

Линия только для своих сотрудников (ServiceUsersAudience=FORSPECIALISTS), клиенты её не видят.
Галочку «Внешний сервер для обработки данных» (карточка линии → Чат-бот) API не ставит —
её включают в кабинете администратора. После этого бот сам поднимет туннель и вебхук.
Методы — SOAP PartnerWebAPI2 (ServiceLineKindAdd, CompetenceAdd, UserServiceLineAdd) и
REST GetLines (/v1/line/) по публичной документации 1С-Коннект. Пароли не печатаются.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from xml.sax.saxutils import escape

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.win_utf8 import ensure_utf8_stdio  # noqa: E402

ensure_utf8_stdio()

from bridge.config import Settings  # noqa: E402
from common.yaml_edit import set_value  # noqa: E402

LINE_NAME = "ИИ-помощник"
SOAP_URL = "https://cus.1c-connect.com/cus/ws/PartnerWebAPI2"
NS = "http://buhphone.com/PartnerWebAPI2"


def _prop(name: str, typ: str, value) -> str:
    if typ == "boolean":
        value = "true" if value else "false"
    return (f'<Property xmlns="http://v8.1c.ru/8.1/data/core" name="{name}">'
            f'<Value xsi:type="xs:{typ}">{escape(str(value))}</Value></Property>')


def _table(columns: list[tuple[str, str]], rows: list[list]) -> str:
    cols = "".join(f"<d:column><d:Name>{n}</d:Name><d:ValueType><d:Type>xs:{t}</d:Type></d:ValueType></d:column>"
                   for n, t in columns)
    body = "".join("<d:row>" + "".join(f"<d:Value>{escape(str(v))}</d:Value>" for v in r) + "</d:row>"
                   for r in rows)
    return cols + body


def soap(s: Settings, method: str, params: str, table: bool = False) -> tuple[str, str]:
    attrs = ' xmlns:d="http://v8.1c.ru/8.1/data/core" xsi:type="d:ValueTable"' if table else ""
    body = ('<?xml version="1.0" encoding="utf-8"?>'
            '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" xmlns:par="' + NS + '"'
            ' xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            f'<soap:Body><par:{method}><par:Params{attrs}>{params}</par:Params></par:{method}></soap:Body>'
            '</soap:Envelope>').encode("utf-8")
    r = requests.post(SOAP_URL, data=body, auth=(s.connect.login, s.connect.password), timeout=60,
                      headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": f'"{NS}#PartnerWebAPI2:{method}"'})
    if r.status_code != 200:
        raise SystemExit(f"{method}: HTTP {r.status_code} {r.text[:300]}")
    code = (re.findall(r'name="ResultCode"[^>]*>\s*<Value[^>]*>([^<]*)', r.text) or [""])[0]
    return code, r.text


def lines(s: Settings) -> list[dict]:
    r = requests.get(f"{s.connect.api_base_url}/v1/line/", auth=(s.connect.login, s.connect.password), timeout=30)
    r.raise_for_status()
    return r.json()


def status(s: Settings) -> int:
    for ln in lines(s):
        mark = "  ← бот" if ln["line_id"] == s.connect.line_id else ""
        old = "  (старый бот)" if ln["line_id"] == s.connect.foreign_line_id else ""
        print(f"{ln['name'][:45]:45} allow_bot={ln.get('allow_bot')} hook={ln.get('hook_line') or '—'}{mark}{old}")
    return 0


def setup(s: Settings) -> int:
    cs = s.connect
    if not (cs.login and cs.password and cs.bot_specialist_id):
        raise SystemExit("Нужны CONNECT_API_LOGIN, CONNECT_API_PASSWORD и учётка бота (CONNECT_BOT_SPECIALIST_ID)")
    line = next((ln for ln in lines(s) if ln["name"] == LINE_NAME), None)
    if line:
        line_id = line["line_id"]
        print(f"Линия «{LINE_NAME}» уже есть: {line_id}")
    else:
        params = "".join([
            _prop("Name", "string", LINE_NAME),
            _prop("DisplayedName", "string", "ИИ-помощник ГК «Эксперт»"),
            _prop("DisplayedHint", "string", "Вопросы по программам, базам 1С и контрагентам — отвечает ИИ"),
            _prop("ServiceUsersAudience", "string", "FORSPECIALISTS"),
            _prop("OfferServiceForClients", "boolean", False),
            _prop("OfferServiceForSpecialists", "boolean", True),
            _prop("AutoConnectServiceForClients", "boolean", False),
            _prop("AutoConnectServiceForSpecialists", "boolean", True),
            _prop("AutoAcceptRequestForConnectService", "boolean", True),
        ])
        code, text = soap(s, "ServiceLineKindAdd", params)
        found = re.findall(r'name="ServiceID">\s*<Value[^>]*>([0-9a-f-]{36})', text)
        if code != "SUCCESS" or not found:
            raise SystemExit(f"ServiceLineKindAdd: {code} {re.sub(r'<[^>]+>', ' ', text)[-300:]}")
        line_id = found[0]
        print(f"Создана линия «{LINE_NAME}»: {line_id}")
    if line_id == cs.foreign_line_id:
        raise SystemExit("Это линия старого бота — не трогаем")

    code, text = soap(s, "CompetenceAdd", _prop("SpecialistID", "string", cs.bot_specialist_id)
                      + _prop("ServiceID", "string", line_id) + _prop("Priority", "integer", 1))
    print(f"Компетенция учётке бота: {code} {'' if code == 'SUCCESS' else re.sub(r'<[^>]+>', ' ', text)[-200:]}")

    people = [p for p in cs.history_colleagues if p]
    if people:
        code, text = soap(s, "UserServiceLineAdd", _table(
            [("ServiceUserID", "string"), ("ServiceID", "string")], [[p, line_id] for p in people]), table=True)
        print(f"Подключение линии сотрудникам ({len(people)}): {code} "
              f"{'' if code == 'SUCCESS' else re.sub(r'<[^>]+>', ' ', text)[-300:]}")

    if s.source and s.connect.line_id != line_id:
        set_value(s.source, ("connect", "line_id"), line_id)
        print(f"В {s.source.name} записано connect.line_id = {line_id}")
    print("Дальше: кабинет администратора 1С-Коннект → линия «ИИ-помощник» → Чат-бот → включить "
          "«Внешний сервер для обработки данных». Бот поднимет вебхук сам в течение 5 минут.")
    return 0


def main() -> int:
    s = Settings.load()
    if sys.argv[1:2] == ["status"]:
        return status(s)
    return setup(s)


if __name__ == "__main__":
    sys.exit(main())
