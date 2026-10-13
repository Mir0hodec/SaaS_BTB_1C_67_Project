import re
from pathlib import Path
import shutil

import pytest
import yaml
from fastapi.testclient import TestClient

from bridge.app import create_app
from bridge.config import Settings
from bridge.connect_directory import Specialist, parse_specialists

BOT = "f9d6119c-ec7a-4dfe-88db-ddb0f444e867"
IVAN = "6a24f7b0-b9bb-42fa-91f2-250a02329a8a"
YULIA = "d5766bf1-d9f3-11e5-80e1-0025904f970d"
IGOR = "b07b8c7f-4b74-11e3-93ef-e839352bba69"
PORT = 8010


class FakeConnect:
    def send_message(self, **kw):
        pass


@pytest.fixture
def panel(repo_root, tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    settings_file = cfg / "settings.yaml"
    shutil.copy(repo_root / "config" / "settings.example.yaml", settings_file)
    (cfg / "users.yaml").write_text('allow_all: true\n', encoding="utf-8")
    s = Settings.load(settings_file)
    s.users_path = cfg / "users.yaml"
    s.bases_path = repo_root / "config" / "bases.example.yaml"
    s.data_dir = tmp_path / "data"
    s.knowledge_base.root, s.knowledge_base.index_path = tmp_path / "kb", tmp_path / "kb.sqlite3"
    s.connect.bot_specialist_id = BOT
    app = create_app(s, connect=FakeConnect())
    client = TestClient(app, base_url=f"http://127.0.0.1:{PORT}", client=("127.0.0.1", 50000))
    with client:
        html = client.get("/panel").text
        token = re.search(r'name="panel-token" content="([^"]+)"', html).group(1)
        app.state.store.set_state("directory", '{"loaded_at": 9999999999, "people": ['
                                  f'{{"id": "{IVAN}", "surname": "Бубнов", "name": "Иван"}},'
                                  f'{{"id": "{YULIA}", "surname": "Бубнова", "name": "Юлия", "post": "Руководитель"}},'
                                  f'{{"id": "{IGOR}", "surname": "Бубнов", "name": "Игорь"}}]}}')
        yield client, {"X-Panel-Token": token}, s, settings_file


def test_page_and_guard(panel):
    client, auth, s, _ = panel
    assert client.get("/panel/static/panel.js").status_code == 200
    assert client.get("/panel/api/overview").status_code == 403                      # без токена
    assert client.get("/panel/api/overview", headers={**auth, "Host": "evil.com"}).status_code == 403
    assert client.get("/panel/api/overview", headers={**auth, "X-Forwarded-For": "1.2.3.4"}).status_code == 403
    assert client.post("/panel/api/pause", json={"paused": True},
                       headers={**auth, "Origin": "http://evil.com"}).status_code == 403
    o = client.get("/panel/api/overview", headers=auth).json()
    assert o["model"] == "claude-opus-5-5" and o["stats"]["today"]["questions"] == 0 and len(o["stats"]["series"]) == 14


def test_directory_search_and_no_dm_pages(panel):
    client, auth, s, _ = panel
    found = client.get("/panel/api/directory?q=бубнов", headers=auth).json()["people"]
    assert {p["name"] for p in found} == {"Бубнов Иван", "Бубнова Юлия", "Бубнов Игорь"}
    assert client.get("/panel/api/colleagues", headers=auth).status_code == 404     # личных сообщений больше нет
    o = client.get("/panel/api/overview", headers=auth).json()
    assert "receiver" not in o and "line" in o
    js = client.get("/panel/static/panel.js").text
    assert "Личные сообщения" not in js and "хэштег" not in js.lower()


def test_settings_validation_and_persist(panel):
    client, auth, s, settings_file = panel
    assert client.post("/panel/api/settings", json={"model": "gpt-4"}, headers=auth).status_code == 400
    r = client.post("/panel/api/settings", json={"model": "claude-sonnet-5-5", "effort": "medium",
                                                  "ack_text": "Секунду…"}, headers=auth)
    assert r.status_code == 200 and "interval" not in r.json()
    assert (s.claude.model, s.claude.effort, s.connect.ack_text) == ("claude-sonnet-5-5", "medium", "Секунду…")
    text = settings_file.read_text(encoding="utf-8")
    assert "# всегда полное имя модели" in text
    raw = yaml.safe_load(text)
    assert raw["claude"]["model"] == "claude-sonnet-5-5" and raw["connect"]["ack_text"] == "Секунду…"


def test_journal_with_answer(panel):
    client, auth, s, _ = panel
    store = client.app.state.store
    store.log(channel="connect", user_key=YULIA, user_name="Сотрудник", base_id=None, question="как начислить пени",
              status="ok", answer_chars=5, duration_ms=1700, cost_usd=0.08, answer="Ответ")
    store.log(channel="connect", user_key=IVAN, user_name="Сотрудник", base_id=None, question="привет",
              status="error", error="сбой")
    rows = client.get("/panel/api/journal", headers=auth).json()["rows"]
    assert [r["user_name"] for r in rows] == ["Бубнов Иван", "Бубнова Юлия"]
    assert [r["question"] for r in client.get("/panel/api/journal?status=ok", headers=auth).json()["rows"]] == \
        ["как начислить пени"]
    entry = client.get(f"/panel/api/journal/{rows[1]['id']}", headers=auth).json()
    assert entry["answer"] == "Ответ"
    o = client.get("/panel/api/overview", headers=auth).json()
    assert o["stats"]["today"]["questions"] == 2 and o["stats"]["today"]["ok_rate"] == 50


def test_pause_and_logs(panel):
    client, auth, s, _ = panel
    assert client.post("/panel/api/pause", json={"paused": True}, headers=auth).json()["paused"]
    assert client.get("/panel/api/overview", headers=auth).json()["paused"]
    assert not client.post("/panel/api/pause", json={"paused": False}, headers=auth).json()["paused"]
    assert client.get("/panel/api/logs?file=../x", headers=auth).status_code == 404
    assert client.post("/panel/api/restart", headers=auth).status_code == 409   # не под start.py


def test_parse_specialists_with_empty_values():
    xml = ('<Envelope xmlns="http://schemas.xmlsoap.org/soap/envelope/"><Body><r xmlns="urn:x"><return>'
           '<Property name="ResultCode"><Value>SUCCESS</Value></Property><Property name="ResultData"><Value>'
           '<column><Name>SpecialistID</Name></column><column><Name>Deleted</Name></column>'
           '<column><Name>Name</Name></column><column><Name>Surname</Name></column><column><Name>Post</Name></column>'
           f'<row><Value>{IVAN.upper()}</Value><Value>false</Value><Value>Бубнов Иван</Value><Value/><Value></Value></row>'
           f'<row><Value>{YULIA}</Value><Value>true</Value><Value>Юлия</Value><Value>Бубнова</Value><Value>Руководитель</Value></row>'
           '</Value></Property></return></r></Body></Envelope>').encode()
    people = parse_specialists(xml)
    assert people[0] == Specialist(id=IVAN, name="Бубнов Иван")
    assert people[1].full_name == "Бубнова Юлия" and people[1].deleted and people[1].post == "Руководитель"


def test_claude_account_without_tokens(tmp_path):
    import json

    from bridge.panel import claude_account
    s = Settings.load(Path(__file__).resolve().parent.parent / "config" / "settings.example.yaml")
    s.claude.config_dir = str(tmp_path)
    assert claude_account(s)["logged_in"] is False
    (tmp_path / ".claude.json").write_text(json.dumps({"oauthAccount": {
        "emailAddress": "igor@example.kz", "displayName": "Игорь", "organizationName": "ГК"}}), encoding="utf-8")
    (tmp_path / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "secret-a", "refreshToken": "secret-r", "subscriptionType": "pro"}}), encoding="utf-8")
    info = claude_account(s)
    assert info["logged_in"] and info["email"] == "igor@example.kz" and info["plan"] == "pro"
    assert "secret" not in json.dumps(info)


def test_overview_reports_account_and_windows_user(panel):
    client, auth, s, _ = panel
    o = client.get("/panel/api/overview", headers=auth).json()
    assert set(o["account"]) >= {"logged_in", "email", "plan"} and o["windows_user"]["name"]
