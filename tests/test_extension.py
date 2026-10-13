"""Опубликованная база с расширением MCP_HTTPService (/hs/mcp-1c): клиент, MCP-сервер, проверка, панель.

Вместо 1С — HTTP-сервер с тем же контрактом, что у модуля расширения 0.4.8.
"""
import asyncio
import base64
import json
import re
import shutil
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from bridge import onec_extension, toolkit_secrets
from bridge.access import User, load_bases
from bridge.app import create_app
from bridge.assistant import Assistant, Job
from bridge.claude_runner import RunResult
from bridge.config import Settings
from bridge.mcp_config_builder import build_mcp_config, session_tools
from bridge.store import Store
from mcp_1c.onec_client import EXTENSION_NOT_PUBLISHED, EXTENSION_WRONG_PASSWORD, OneCError, build_client

REPO = Path(__file__).resolve().parent.parent
GOOD_AUTH = "Basic " + base64.b64encode("Администратор:1".encode()).decode()


class Fake1C(BaseHTTPRequestHandler):
    published = True
    requests_seen: list = []

    def log_message(self, *a):
        pass

    def _send(self, code: int, body, ctype="application/json; charset=utf-8"):
        data = (json.dumps(body, ensure_ascii=False) if not isinstance(body, str) else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if code == 401:
            self.send_header("WWW-Authenticate", 'Basic realm="1C:Enterprise 8.3"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _route(self, method: str):
        if self.headers.get("Authorization") != GOOD_AUTH:
            return self._send(401, "<html>401</html>", "text/html")
        path = unquote(self.path)
        if not path.startswith("/Base1/hs/mcp-1c/") or not Fake1C.published:
            return self._send(404, "<html>IIS 404</html>", "text/html; charset=utf-8")
        path = path[len("/Base1/hs/mcp-1c"):]
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}") \
            if method == "POST" else {}
        Fake1C.requests_seen.append((method, path, body))
        if path == "/version":
            return self._send(200, {"version": "0.4.8"})
        if path == "/configuration":
            return self._send(200, {"name": "УправлениеТорговлей", "version": "11.5.1", "vendor": "1С",
                                    "platform_version": "8.3.24.1368", "mode": "server"})
        if path == "/metadata":
            return self._send(200, {"Справочники": ["Номенклатура"], "Документы": ["ПоступлениеТоваров"]})
        if path == "/object/Document/ПоступлениеТоваров":
            return self._send(200, {"name": "ПоступлениеТоваров", "attributes": [{"name": "Контрагент"}]})
        if path.startswith("/object/"):
            return self._send(404, {"error": "Object not found"})
        if path == "/form/Document/ПоступлениеТоваров":
            return self._send(200, {"name": "ФормаДокумента", "elements": [], "commands": [], "handlers": []})
        if path == "/query":
            if not body["query"].upper().startswith("ВЫБРАТЬ"):
                return self._send(400, {"error": "Only SELECT queries allowed"})
            return self._send(200, {"columns": ["Наименование", "Цена"], "rows": [["Сахар", 0]], "total": 1,
                                    "truncated": body.get("limit") == 1})
        if path == "/validate-query":
            return self._send(200, {"valid": True, "errors": []})
        if path == "/eventlog":
            return self._send(200, {"events": [{"level": "Ошибка", "comment": "нет цены"}], "total": 1})
        return self._send(404, {"error": "unknown"})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")


@pytest.fixture
def fake_1c():
    Fake1C.published, Fake1C.requests_seen = True, []
    server = HTTPServer(("127.0.0.1", 0), Fake1C)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/Base1"
    server.shutdown()


def test_client_follows_extension_contract(fake_1c):
    c = build_client({"mode": "extension", "url": fake_1c})
    assert c.version() == "0.4.8"
    assert c.get_metadata()["objects"]["Документы"] == ["ПоступлениеТоваров"]
    assert c.get_metadata("Документ.ПоступлениеТоваров")["attributes"][0]["name"] == "Контрагент"
    with pytest.raises(OneCError, match="Object not found"):
        c.get_metadata("Справочник.НетТакого")
    res = c.run_query("ВЫБРАТЬ 1", {"Дата": "2026-10-01"})
    assert res == {"columns": ["Наименование", "Цена"], "rows": [["Сахар", 0]], "truncated": False}
    assert Fake1C.requests_seen[-1][2] == {"query": "ВЫБРАТЬ 1", "parameters": {"Дата": "2026-10-01"}, "limit": 1000}
    assert c.validate_query("ВЫБРАТЬ 1") == {"ok": True, "error": ""}
    c.get_event_log(level="Ошибка", start_date=None, limit=50)
    assert Fake1C.requests_seen[-1][2] == {"level": "Ошибка", "limit": 50}      # пустые отборы не уходят
    with pytest.raises(OneCError, match="Only SELECT"):
        c.run_query("УДАЛИТЬ")


def test_probe_reports_password_and_publication(fake_1c):
    ok = onec_extension.probe(fake_1c)
    assert ok.ok and ok.user == "Администратор" and ok.config == "УправлениеТорговлей" and "0.4.8" in ok.detail
    Fake1C.published = False
    assert onec_extension.probe(fake_1c).message == EXTENSION_NOT_PUBLISHED
    other = onec_extension.OneCExtensionClient(fake_1c, auth=("Администратор", "2"))
    with pytest.raises(OneCError) as err:
        other.version()
    assert str(err.value) == EXTENSION_WRONG_PASSWORD
    assert not onec_extension.probe("http://127.0.0.1:9/nobase", timeout=2).ok


def _env(tmp_path, url):
    s = Settings.load(REPO / "config" / "settings.example.yaml")
    s.bases_path = tmp_path / "bases.yaml"
    s.bases_path.write_text(f'ut_pub:\n  title: "УТ опубликованная"\n  tags: ["ут"]\n  mode: extension\n'
                            f'  url: "{url}"\n', encoding="utf-8")
    s.users_path = tmp_path / "users.yaml"
    s.users_path.write_text("allow_all: true\n", encoding="utf-8")
    s.data_dir = tmp_path / "data"
    s.knowledge_base.root, s.knowledge_base.index_path = tmp_path / "kb", tmp_path / "kb.sqlite3"
    return s


async def _session(cfg: dict, cwd: Path, calls):
    params = StdioServerParameters(command=cfg["command"], args=cfg["args"], env=cfg["env"], cwd=str(cwd))
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        tools = [t.name for t in (await s.list_tools()).tools]
        return tools, [await s.call_tool(name, args) for name, args in calls]


def test_mcp_server_tools_match_bot_allow_list(fake_1c, tmp_path):
    s = _env(tmp_path, fake_1c)
    base = load_bases(s.bases_path)["ut_pub"]
    assert base.via_extension and not base.via_toolkit
    cfg = json.loads(build_mcp_config(s, base_id=base.id, outbox=tmp_path / "out", attachments=tmp_path / "att",
                                      target=tmp_path / "mcp.json", base=base).read_text(encoding="utf-8"))
    (tmp_path / "work").mkdir()
    tools, (meta, query, log, form) = asyncio.run(_session(cfg["mcpServers"]["onec"], tmp_path / "work", [
        ("get_metadata", {}), ("run_query", {"query_text": "ВЫБРАТЬ 1"}),
        ("get_event_log", {"level": "Ошибка"}), ("get_form_structure", {"object_name": "Документ.ПоступлениеТоваров"}),
    ]))
    allowed = {t.removeprefix("mcp__onec__") for t in session_tools(s, base) if t.startswith("mcp__onec__")}
    assert set(tools) == allowed                     # иначе проверка при старте Claude остановит запуск
    assert {"get_event_log", "get_form_structure"} <= set(tools)
    assert "Номенклатура" in meta.content[0].text and "Сахар" in query.content[0].text
    assert "нет цены" in log.content[0].text and "ФормаДокумента" in form.content[0].text


class Runner:
    def __init__(self):
        self.calls = []

    def run(self, **kw):
        self.calls.append(kw)
        return RunResult(text="Готово", is_error=False, session_total_cost=0.01)


def test_gate_and_prompt(fake_1c, tmp_path):
    s = _env(tmp_path, fake_1c)
    runner = Runner()
    a = Assistant(s, Store(s.data_dir / "s.sqlite3"), runner=runner)
    ask = lambda text: a.handle(Job(channel="connect", user=User(key="u1", name="Иван", bases="*"), text=text))  # noqa: E731
    reply = ask("#ут почему не проводится поступление")
    assert reply.status == "ok" and len(runner.calls) == 1
    assert "опубликованная база" in runner.calls[0]["question"] and "Только чтение" in runner.calls[0]["question"]
    assert "mcp__onec__get_event_log" in runner.calls[0]["allowed"]
    Fake1C.published = False
    a.toolkit_checks.clear()
    reply = ask("#ут остатки")
    assert "HTTP-сервис расширения" in reply.text and len(runner.calls) == 1   # Claude не запускался


class FakeConnect:
    def send_colleague_message(self, **kw):
        pass


def test_panel_adds_and_checks_published_base(fake_1c, tmp_path, monkeypatch):
    monkeypatch.setattr(toolkit_secrets, "DEFAULT_PATH", tmp_path / "secrets.json")
    s = _env(tmp_path, fake_1c)
    s.bases_path.write_text("", encoding="utf-8")
    settings_file = tmp_path / "settings.yaml"
    shutil.copy(REPO / "config" / "settings.example.yaml", settings_file)
    s.source = settings_file
    app = create_app(s, connect=FakeConnect(), colleague_source=lambda on, stop: None)
    client = TestClient(app, base_url=f"http://127.0.0.1:{s.port}", client=("127.0.0.1", 50000))
    with client:
        token = re.search(r'name="panel-token" content="([^"]+)"', client.get("/panel").text).group(1)
        auth = {"X-Panel-Token": token}
        bad = client.post("/panel/api/bases", headers=auth, json={"id": "pub", "title": "x", "kind": "extension",
                                                                   "url": "ftp://x"})
        assert bad.status_code == 400
        r = client.post("/panel/api/bases", headers=auth, json={
            "id": "prosmotr1", "title": "Просмотр", "tags": "просмотр", "kind": "extension",
            "url": fake_1c + "/hs/mcp-1c/"})
        assert r.status_code == 200, r.text
        row = next(b for b in r.json()["bases"] if b["id"] == "prosmotr1")
        assert row["kind"] == "extension" and row["url"] == fake_1c
        assert "password" not in s.bases_path.read_text(encoding="utf-8")
        job = client.post("/panel/api/bases/prosmotr1/check", headers=auth).json()
        for _ in range(50):
            if job["state"] != "running":
                break
            job = client.get(f"/panel/api/jobs/{job['id']}", headers=auth).json()
        assert job["state"] == "done" and job["result"]["ok"] and "0.4.8" in job["result"]["detail"]
