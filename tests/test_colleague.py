import io
import platform
import re
import threading
import time

import pytest
from fastapi.testclient import TestClient

from bridge.app import create_app
from bridge.config import Settings
from bridge.connect_pipe import (
    SUBSCRIBE, ColleagueMessage, PipeProtocolError, encode_packet, listen, parse_colleague_message, read_packet,
)

BOT = "b0b00000-0000-0000-0000-00000000b0b0"
ALICE = "a1a1a1a1-0000-0000-0000-000000000001"
BORIS = "b2b2b2b2-0000-0000-0000-000000000002"


def event_xml(message_id, colleague, text, author=None, initiator="Incoming"):
    return (f'<Event Time="2026-09-28T10:00:00" Mode="Colleagues" Object="Message" Initiator="{initiator}">'
            f"<MessageID>{message_id}</MessageID><ColleagueID>{colleague}</ColleagueID>"
            f"<AuthorID>{author or colleague}</AuthorID><MessageBody>{text}</MessageBody>"
            f"<Sended>2026-09-28T10:00:00</Sended></Event>")


# --- протокол канала ---

def test_packet_roundtrip_with_cyrillic_and_entities():
    raw = encode_packet(event_xml("m1", ALICE, "остатки по кассе &amp; банку"))
    assert raw[2:4] == b"\x00\x00"
    msg = parse_colleague_message(read_packet(io.BytesIO(raw)))
    assert msg == ColleagueMessage("m1", ALICE, ALICE, "остатки по кассе & банку", "2026-09-28T10:00:00")


def test_other_events_are_ignored():
    assert parse_colleague_message('<Event Time="t" Object="AgentOnlineStatus"><Status>Away</Status></Event>') is None
    assert parse_colleague_message(event_xml("m2", ALICE, "x", initiator="self")) is None
    assert parse_colleague_message('<CommandResult ID="1" Action="EventSubscribe"><Result/></CommandResult>') is None


def test_bad_header_and_truncated_packet():
    with pytest.raises(PipeProtocolError):
        read_packet(io.BytesIO(b"\x05\x00\x01\x00abcde"))
    with pytest.raises(EOFError):
        read_packet(io.BytesIO(encode_packet(event_xml("m3", ALICE, "текст"))[:-3]))


class FakePipe(io.BytesIO):
    def __init__(self, data: bytes):
        super().__init__(data)
        self.written = b""

    def write(self, b):
        self.written += bytes(b)
        return len(b)


def test_listen_reconnects_and_resubscribes():
    pipes = [FakePipe(encode_packet(event_xml("m1", ALICE, "первый"))),
             FakePipe(encode_packet('<CommandResult ID="x" Action="EventSubscribe"><Result/></CommandResult>')
                      + encode_packet(event_xml("m2", BORIS, "второй")))]
    opened, got, stop = [], [], threading.Event()

    def open_pipe(path):
        if len(opened) == 1 and not hasattr(open_pipe, "failed"):
            open_pipe.failed = True
            raise OSError("приложение 1С-Коннект перезапускается")
        if not pipes:
            stop.set()
            raise OSError("больше нет")
        opened.append(path)
        return pipes.pop(0)

    first, second = pipes
    listen("gke_ai_bot", got.append, stop, open_pipe=open_pipe, retry_min=0.01, retry_max=0.02)
    assert [m.text for m in got] == ["первый", "второй"]
    assert opened == ["\\\\.\\pipe\\BuhphoneAgentAPI2_gke_ai_bot"] * 2
    assert first.written == SUBSCRIBE and second.written == SUBSCRIBE   # подписка после каждого подключения


# --- два сотрудника: истории не смешиваются ---

class FakeConnect:
    def __init__(self):
        self.sent = []

    def send_colleague_message(self, *, recipient_id, author_id, text):
        self.sent.append((recipient_id, author_id, text))

    def send_colleague_file(self, *, recipient_id, author_id, path, comment=None):
        self.sent.append((recipient_id, author_id, f"[файл {path.name}]"))


@pytest.mark.skipif(platform.system() != "Windows", reason="заглушка CLI — .cmd-шим Windows")
def test_two_colleagues_histories_do_not_mix(repo_root, tmp_path, monkeypatch):
    s = Settings.load(repo_root / "config" / "settings.example.yaml")
    s.claude.binary = str(repo_root / "scripts" / "mock_claude.cmd")
    s.data_dir = tmp_path / "data"
    s.knowledge_base.root, s.knowledge_base.index_path = tmp_path, tmp_path / "kb.sqlite3"
    s.users_path = tmp_path / "users.yaml"
    s.bases_path = repo_root / "config" / "bases.example.yaml"
    s.users_path.write_text("allow_all: true\ndefault_bases: '*'\n", encoding="utf-8")
    s.connect.channel, s.connect.bot_specialist_id = "colleague", BOT

    incoming = [
        ColleagueMessage("a-1", ALICE, ALICE, "#демо остатки по кассе", ""),
        ColleagueMessage("b-1", BORIS, BORIS, "как начислить пени", ""),
        ColleagueMessage("a-2", ALICE, ALICE, "а теперь по месяцам [mock:file]", ""),
        ColleagueMessage("a-1", ALICE, ALICE, "#демо остатки по кассе", ""),   # повторная доставка
        ColleagueMessage("x-1", ALICE, BOT, "эхо ответа бота", ""),            # своё сообщение бота
    ]

    def source(on_message, stop):
        for m in incoming:
            on_message(m)

    fake = FakeConnect()
    with TestClient(create_app(s, connect=fake, colleague_source=source)) as client:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and sum(1 for r, _, t in fake.sent if t.startswith("[mock]")) < 3:
            time.sleep(0.2)
        client.app.state.worker.queue.join()

    answers = {ALICE: [], BORIS: []}
    for recipient, author, text in fake.sent:
        assert author == BOT
        if text.startswith("[mock]"):
            answers[recipient].append(text)
    assert len(answers[ALICE]) == 2 and len(answers[BORIS]) == 1   # дубль и эхо не обработаны

    session = lambda t: re.search(r"Сессия: ([0-9a-f-]+)", t).group(1)  # noqa: E731
    assert session(answers[ALICE][0]) == session(answers[ALICE][1])   # Алиса продолжает свой разговор
    assert session(answers[BORIS][0]) != session(answers[ALICE][0])   # у Бориса — свой
    assert "по месяцам" in answers[ALICE][1] and "Демо-база" in answers[ALICE][1]   # контекст базы сохранён
    assert "пени" not in "".join(answers[ALICE]) and "касс" not in answers[BORIS][0]
    assert (ALICE, BOT, "[файл отчет.txt]") in fake.sent and all(r != BOT for r, _, _ in fake.sent)
    acks = [r for r, _, t in fake.sent if t == s.connect.ack_text]
    assert sorted(acks) == sorted([ALICE, BORIS, ALICE])


# --- универсальный JSON-приёмник ---

@pytest.mark.skipif(platform.system() != "Windows", reason="заглушка CLI — .cmd-шим Windows")
def test_http_json_receiver(repo_root, tmp_path, monkeypatch):
    monkeypatch.setenv("CONNECT_WEBHOOK_TOKEN", "hook-secret")
    s = Settings.load(repo_root / "config" / "settings.example.yaml")
    s.claude.binary = str(repo_root / "scripts" / "mock_claude.cmd")
    s.data_dir = tmp_path / "data"
    s.knowledge_base.root, s.knowledge_base.index_path = tmp_path, tmp_path / "kb.sqlite3"
    s.users_path = tmp_path / "users.yaml"
    s.bases_path = repo_root / "config" / "bases.example.yaml"
    s.users_path.write_text("allow_all: true\n", encoding="utf-8")
    s.connect.bot_specialist_id = BOT
    fake = FakeConnect()
    body = {"platform": "1c-connect", "event": "message_received", "message_id": "j-1",
            "sender_id": ALICE, "recipient_id": BOT, "text": "Привет, бот!", "timestamp": "2026-09-28T16:00:00+05:00"}
    with TestClient(create_app(s, connect=fake, colleague_source=lambda on, stop: None)) as client:
        url, auth = "/connect/colleague/message", {"Authorization": "Bearer hook-secret"}
        assert client.post(url, json=body).status_code == 401
        assert client.post(url, json={"text": "x"}, headers=auth).status_code == 400
        first = client.post(url, json=body, headers=auth)
        assert first.status_code == 202 and first.json()["status"] == "accepted"
        assert client.post(url, json=body, headers=auth).json()["status"] == "duplicate"
        own = {**body, "message_id": "j-2", "sender_id": BOT, "recipient_id": ALICE}
        assert client.post(url, json=own, headers=auth).json()["status"] == "own_message"
        other = {**body, "message_id": "j-3", "recipient_id": BORIS}
        assert client.post(url, json=other, headers=auth).json()["status"] == "wrong_recipient"
        client.app.state.worker.queue.join()
    replies = [t for r, a, t in fake.sent if r == ALICE and t.startswith("[mock]")]
    assert len(replies) == 1 and "Привет, бот!" in replies[0]


def test_send_failure_is_journaled(repo_root, tmp_path):
    from bridge.assistant import Reply
    from bridge.connect_adapter import ConnectAdapter
    from bridge.connect_client import ConnectError
    from bridge.store import Store

    class Broken:
        def send_colleague_message(self, **kw):
            raise ConnectError("/v1/colleague/send/message/: HTTP 403 Недостаточно прав")

    s = Settings.load(repo_root / "config" / "settings.example.yaml")
    store = Store(tmp_path / "s.sqlite3")
    adapter = ConnectAdapter(s, store, Broken(), submit=lambda job, deliver: None)
    with pytest.raises(ConnectError):
        adapter.deliver(ALICE, Reply("ответ", "ok"))
    row = store.journal()[-1]
    assert row["status"] == "send_error" and "403" in row["error"] and row["user_key"] == ALICE
