"""Базы на ПК сотрудников через MCP Toolkit: конфиг MCP, инструменты, проверки до запуска Claude."""
import json
import re
import shutil
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from bridge import onec_toolkit, toolkit_secrets
from bridge.access import User, load_bases
from bridge.app import create_app
from bridge.assistant import Assistant, Job
from bridge.claude_runner import RunResult
from bridge.config import Settings
from bridge.mcp_config_builder import allowed_tools, build_mcp_config
from bridge.store import Store

REPO = Path(__file__).resolve().parent.parent
IVAN = "6a24f7b0-b9bb-42fa-91f2-250a02329a8a"
YULIA = "d5766bf1-d9f3-11e5-80e1-0025904f970d"
BASES = f"""gke_ivan:
  title: "ГКЭ у Ивана"
  toolkit_url: "http://192.168.0.51:6003/mcp"
  owners: ["{IVAN}"]
  write: true
osi_ro:
  title: "ОСИ только чтение"
  toolkit_url: "http://192.168.0.52:6003/mcp"
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(toolkit_secrets, "DEFAULT_PATH", tmp_path / "secrets.json")
    s = Settings.load(REPO / "config" / "settings.example.yaml")
    s.bases_path = tmp_path / "bases.yaml"
    s.bases_path.write_text(BASES, encoding="utf-8")
    s.users_path = tmp_path / "users.yaml"
    s.users_path.write_text("allow_all: true\n", encoding="utf-8")
    s.data_dir = tmp_path / "data"
    s.knowledge_base.root, s.knowledge_base.index_path = tmp_path / "kb", tmp_path / "kb.sqlite3"
    return s


def test_mcp_config_keeps_token_out_of_files(env, tmp_path):
    toolkit_secrets.set_token("gke_ivan", "super-secret-token")
    base = load_bases(env.bases_path)["gke_ivan"]
    cfg = build_mcp_config(env, base_id=base.id, outbox=tmp_path / "o", attachments=tmp_path / "a",
                           target=tmp_path / "mcp.json", base=base)
    text = cfg.read_text(encoding="utf-8")
    server = json.loads(text)["mcpServers"]["onec"]
    assert server == {"type": "http", "url": "http://192.168.0.51:6003/mcp",
                      "headers": {"Authorization": "Bearer ${ONEC_TOOLKIT_TOKEN_GKE_IVAN}"}}
    assert "super-secret-token" not in text


def test_tools_depend_on_write_flag(env):
    bases = load_bases(env.bases_path)
    rw = allowed_tools("gke_ivan", base=bases["gke_ivan"])
    ro = allowed_tools("osi_ro", base=bases["osi_ro"])
    assert "mcp__onec__execute_code" in rw and "mcp__onec__execute_code" not in ro
    assert "mcp__onec__get_metadata" in ro and "mcp__onec__submit_for_deanonymization" in ro
    assert not any(t.endswith(("restart_1c_session", "close_1c_session")) for t in rw)
    assert "mcp__onec__run_query" not in rw            # демо-инструменты — только у демо-базы


class Runner:
    def __init__(self):
        self.calls = []

    def run(self, **kw):
        self.calls.append(kw)
        return RunResult(text="Готово", is_error=False, session_total_cost=0.01)


def _ask(env, monkeypatch, user_key, text, *, health=(True, ""), probe=None):
    runner = Runner()
    a = Assistant(env, Store(env.data_dir / "s.sqlite3"), runner=runner)
    monkeypatch.setattr(onec_toolkit, "health", lambda url, timeout=3.0: health)
    monkeypatch.setattr(onec_toolkit, "probe", lambda url, token, timeout=60.0: probe or onec_toolkit.ProbeResult(
        ok=True, user="Администратор", password_ok=True, config="ОСИ", config_title="ОСИ ЖКХ", version="3.1",
        tools=["execute_query", "execute_code", "get_metadata", "get_screenshot", "restart_1c_session"]))
    reply = a.handle(Job(channel="connect", user=User(key=user_key, name="Иван", bases="*"), text=text))
    return reply, runner


def test_gate_refusals_do_not_start_claude(env, monkeypatch):
    reply, runner = _ask(env, monkeypatch, YULIA, "http://192.168.0.51:6003/mcp остатки")
    assert reply.status == "denied" and not runner.calls                     # база закреплена за Иваном
    reply, runner = _ask(env, monkeypatch, IVAN, "http://192.168.0.51:6003/mcp остатки")
    assert "не задан токен" in reply.text and not runner.calls
    toolkit_secrets.set_token("gke_ivan", "tok")
    reply, runner = _ask(env, monkeypatch, IVAN, "http://192.168.0.51:6003/mcp остатки", health=(False, "нет связи"))
    assert "недоступна" in reply.text and not runner.calls
    bad = onec_toolkit.ProbeResult(ok=False, message=onec_toolkit.WRONG_PASSWORD)
    reply, runner = _ask(env, monkeypatch, IVAN, "http://192.168.0.51:6003/mcp остатки", probe=bad)
    assert "Пароль «1» не подходит" in reply.text and not runner.calls


def test_gate_passes_token_only_via_env(env, monkeypatch):
    toolkit_secrets.set_token("gke_ivan", "tok-123")
    reply, runner = _ask(env, monkeypatch, IVAN, "http://192.168.0.51:6003/mcp почему не проводится документ")
    assert reply.status == "ok" and len(runner.calls) == 1
    call = runner.calls[0]
    assert call["extra_env"] == {"ONEC_TOOLKIT_TOKEN_GKE_IVAN": "tok-123"}
    assert "mcp__onec__execute_code" in call["allowed"]
    assert "Подключение: MCP Toolkit http://192.168.0.51:6003/mcp" in call["question"]
    assert "Запись в базу разрешена" in call["question"]
    assert "tok-123" not in call["mcp_config"].read_text(encoding="utf-8")
    # лишние инструменты базы убраны из сессии, иначе проверка при старте Claude остановит запуск
    assert {"mcp__onec__get_screenshot", "mcp__onec__restart_1c_session",
            "mcp__onec__close_1c_session"} <= set(call["disallowed"])
    assert not set(call["disallowed"]) & set(call["allowed"])


def test_read_only_base_disallows_execute_code(env, monkeypatch):
    toolkit_secrets.set_token("osi_ro", "tok")
    reply, runner = _ask(env, monkeypatch, YULIA, "http://192.168.0.52:6003/mcp/ остатки")
    call = runner.calls[0]
    assert "mcp__onec__execute_code" in call["disallowed"] and "mcp__onec__execute_code" not in call["allowed"]


def test_runner_passes_disallowed_tools(env, tmp_path):
    from bridge.claude_runner import ClaudeRunner
    args = ClaudeRunner(env).build_args(session_id="s", is_new=True, mcp_config=tmp_path / "m.json",
                                        allowed=["mcp__onec__get_metadata"], disallowed=["mcp__onec__get_screenshot"])
    denied = args[args.index("--disallowedTools") + 1].split(",")
    assert "Bash" in denied and "mcp__onec__get_screenshot" in denied


def test_parse_check_is_anonymization_safe():
    ok = onec_toolkit.parse_check('{"data": "ai_bot_check|1|1|[PER-00001]|OSI|ОСИ ЖКХ|3.1|"}')
    assert ok.ok and ok.config_title == "ОСИ ЖКХ"
    assert onec_toolkit.parse_check("ai_bot_check|1|0|Администратор|c|t|v|").message == onec_toolkit.WRONG_PASSWORD
    assert "без имени" in onec_toolkit.parse_check("ai_bot_check|0|0||c|t|v|").message
    assert onec_toolkit.PASSWORD_FINGERPRINT == "NWoZK3kTsExUV00Ywo1G5jlUKKs="


class FakeConnect:
    def send_message(self, **kw):
        pass


def test_panel_bases_crud(env, tmp_path):
    settings_file = tmp_path / "settings.yaml"
    shutil.copy(REPO / "config" / "settings.example.yaml", settings_file)
    env.source = settings_file
    app = create_app(env, connect=FakeConnect())
    client = TestClient(app, base_url=f"http://127.0.0.1:{env.port}", client=("127.0.0.1", 50000))
    with client:
        token = re.search(r'name="panel-token" content="([^"]+)"', client.get("/panel").text).group(1)
        auth = {"X-Panel-Token": token}
        bad = client.post("/panel/api/bases", json={"id": "Кириллица", "title": "x", "toolkit_url": "http://a:1/mcp"},
                          headers=auth)
        assert bad.status_code == 400
        r = client.post("/panel/api/bases", headers=auth, json={
            "id": "new_base", "title": "Новая", "toolkit_url": "http://10.0.0.5:6003/mcp",
            "token": "t-1", "write": True, "owners": [YULIA]})
        assert r.status_code == 200, r.text
        row = next(b for b in r.json()["bases"] if b["id"] == "new_base")
        assert row["token_set"] and row["write"] and row["link"] == "http://10.0.0.5:6003/mcp"
        assert "t-1" not in env.bases_path.read_text(encoding="utf-8")
        assert toolkit_secrets.get_token("new_base") == "t-1"
        raw = yaml.safe_load(env.bases_path.read_text(encoding="utf-8"))
        assert raw["new_base"]["owners"] == [YULIA] and raw["gke_ivan"]["title"] == "ГКЭ у Ивана"
        assert client.post("/panel/api/bases/new_base/check", headers=auth).status_code == 200
        assert client.delete("/panel/api/bases/new_base", headers=auth).status_code == 200
        assert "new_base" not in yaml.safe_load(env.bases_path.read_text(encoding="utf-8"))
        assert toolkit_secrets.get_token("new_base") == ""


def test_check_and_bot_allow_the_same_tools(env):
    """Проверка Claude в панели и обычный ответ бота разрешают одинаковые инструменты."""
    from bridge.mcp_config_builder import session_tools
    env.dev.skills_dir = REPO / "tools" / "cc-1c-skills" / ".claude" / "skills"
    tools = session_tools(env)
    assert any(t.startswith("mcp__dev__") for t in tools) == env.dev.enabled
    base = load_bases(env.bases_path)["gke_ivan"]
    assert set(session_tools(env)) < set(session_tools(env, base))
