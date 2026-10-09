import base64
import io
import threading
import zipfile

import pytest

from bridge.connect_history import (
    HistoryError, LimitReached, build_request, parse_chat_html, parse_response, poll,
)

BOT = "f9d6119c-ec7a-4dfe-88db-ddb0f444e867"
IGOR = "b07b8c7f-4b74-11e3-93ef-e839352bba69"
OLGA = "0a0a0a0a-1111-2222-3333-444444444444"


def chat_html(names, rows):
    """Разметка как в ответе 1С-Коннект (ChatHistory_Collegue_…html)."""
    body = "".join(
        f'<tr><td align="center" colspan=3><b>{r[1]}</b></td></tr>' if r[0] == "date" else
        f'<tr><td align="center">{r[0]}</td><td align="justify">{r[1]}</td><td align="center">{r[2]}</td></tr>'
        for r in rows)
    return ('<!DOCTYPE HTML><html><head><meta http-equiv="Content-Type" content="text/html;charset=UTF-8">'
            '<title>История чата</title><style>td{}</style></head><body>'
            f'<p>Сотрудник: <b>{names[0]}</b><br>Сотрудник: <b>{names[1]}</b><br>'
            'Период: <b>2026-10-09 10:00:00</b> - <b>2026-10-09 16:00:00</b><br><br></p>'
            '<table width="100%" border="1" cellspacing="0" cellpadding="4"><col width="25%"><col width="60%">'
            f'<col width="15%">{body}</tbody></table></body></html>')


def soap_response(files=None, code="OK", data=""):
    props = f'<Property name="ResultCode"><Value>{code}</Value></Property>'
    if files is not None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name, html in files.items():
                z.writestr(name, html)
        data = base64.b64encode(buf.getvalue()).decode()
    props += f'<Property name="ResultData"><Value>{data}</Value></Property>'
    return ('<Envelope xmlns="http://schemas.xmlsoap.org/soap/envelope/"><Body>'
            '<GetHistoryOfColleaguesChatResponse xmlns="http://buhphone.com/PartnerWebAPI2"><return>'
            f'{props}</return></GetHistoryOfColleaguesChatResponse></Body></Envelope>').encode()


def igor_file(rows, day="2026-10-09"):
    return {f"ChatHistory/ChatHistory_Collegue_{BOT}_{IGOR}_{day}.html":
            chat_html(["Александр", "Бубнов Игорь Сергеевич"], [("date", day), *rows])}


def test_parse_chat_html_authors_dates_and_multiline():
    name, html = next(iter(igor_file([
        ("Бубнов Игорь Сергеевич", "остатки по кассе<br>на 1 октября &amp; позже", "10:00:05"),
        ("Александр", "Принял, работаю над ответом…", "10:00:09"),
    ]).items()))
    msgs = parse_chat_html(name, html, BOT)
    assert [(m.author_id, m.colleague_id, m.sent_at) for m in msgs] == [
        (IGOR, IGOR, "2026-10-09T10:00:05"), (BOT, IGOR, "2026-10-09T10:00:09")]
    assert msgs[0].text == "остатки по кассе\nна 1 октября & позже"
    assert msgs[0].message_id != msgs[1].message_id
    assert parse_chat_html(name, html, BOT)[0].message_id == msgs[0].message_id   # стабильный id


def test_parse_response_several_pairs_and_limit():
    files = {**igor_file([("Бубнов Игорь Сергеевич", "привет", "11:00:00")]),
             f"ChatHistory/ChatHistory_Collegue_{OLGA}_{BOT}_2026-10-09.html":
                 chat_html(["Ольга", "Александр"], [("date", "2026-10-09"), ("Ольга", "как начислить пени", "11:01:00")])}
    msgs = parse_response(soap_response(files), BOT)
    assert {(m.colleague_id, m.text) for m in msgs} == {(IGOR, "привет"), (OLGA, "как начислить пени")}
    with pytest.raises(LimitReached):
        parse_response(soap_response(code="OUT_OF_LIMIT", data="limit reached"), BOT)
    assert parse_response(soap_response(code="OK", data=""), BOT) == []
    with pytest.raises(HistoryError):
        parse_chat_html("bad.html", "<p></p>", BOT)


def test_request_without_second_specialist():
    from datetime import datetime
    body = build_request(BOT, "", datetime(2026, 10, 9, 10), datetime(2026, 10, 9, 16)).decode()
    assert "Specialist1ID" in body and "Specialist2ID" not in body and "2026-10-09T10:00:00" in body
    assert "Specialist2ID" in build_request(BOT, IGOR, datetime.now(), datetime.now()).decode()


def test_poll_seeds_existing_then_emits_only_new():
    old = ("Бубнов Игорь Сергеевич", "старый вопрос", "09:00:00")
    new = ("Бубнов Игорь Сергеевич", "новый вопрос", "10:00:00")
    responses = [soap_response(igor_file([old])),
                 soap_response(code="OUT_OF_LIMIT", data="limit reached"),
                 b"<not xml",
                 soap_response(igor_file([old, new, ("Александр", "ответ", "10:00:30")])),
                 soap_response(igor_file([old, new]))]
    got, stop, calls = [], threading.Event(), []

    def fetch(bot, colleague, hours):
        calls.append(colleague)
        if not responses:
            stop.set()
            return soap_response(code="OK", data="")
        return responses.pop(0)

    poll(BOT, [IGOR], got.append, stop, fetch=fetch, interval=0)
    assert [m.text for m in got] == ["новый вопрос", "ответ"]   # ответ бота отсекает приёмник (author != colleague)
    assert set(calls) == {IGOR}


def test_poll_without_colleagues_does_not_call_api():
    calls = []
    poll(BOT, [], lambda m: None, threading.Event(), fetch=lambda *a: calls.append(a), interval=0)
    assert calls == []


def test_param_error_is_reported():
    with pytest.raises(HistoryError, match="PARAM_NOT_EXIST"):
        parse_response(soap_response(code="PARAM_NOT_EXIST", data="SPECIALIST2ID"), BOT)


def test_poll_round_robin_pairs():
    calls, stop = [], threading.Event()

    def fetch(bot, colleague, hours):
        calls.append(colleague)
        if len(calls) >= 4:
            stop.set()
        return soap_response(code="OK", data="")

    poll(BOT, [IGOR, OLGA], lambda m: None, stop, fetch=fetch, interval=0)
    assert calls == [IGOR, OLGA, IGOR, OLGA]
