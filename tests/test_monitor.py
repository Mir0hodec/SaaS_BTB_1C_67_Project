"""Самопроверка: без Claude, оповещение ответственному, без спама, «снова работает»."""
import json
from pathlib import Path

from bridge import monitor
from bridge.config import Settings
from bridge.monitor import Check, Monitor
from bridge.store import Store

REPO = Path(__file__).resolve().parent.parent


def _settings(tmp_path):
    s = Settings.load(REPO / "config" / "settings.example.yaml")
    s.data_dir = tmp_path
    return s


def test_alert_once_repeat_and_recovery(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    store = Store(tmp_path / "s.sqlite3")
    sent = []
    m = Monitor(s, store, send=lambda uid, text: sent.append((uid, text)), recipients=lambda: ["danil"])
    results = [[Check("Claude Code", False, "Your organization has disabled Claude"), Check("База знаний", True, "ок")]]
    monkeypatch.setattr(monitor, "run_checks", lambda settings, store: results[-1])

    m.tick()
    assert len(sent) == 1 and sent[0][0] == "danil"
    assert "Claude Code: Your organization has disabled" in sent[0][1] and "зайти на сервер" in sent[0][1]
    m.tick()
    assert len(sent) == 1                                   # та же проблема — без повтора до repeat_hours
    state = json.loads(store.get_state("monitor"))
    state["alerted_at"] -= s.monitor.repeat_hours * 3600 + 1
    store.set_state("monitor", json.dumps(state))
    m.tick()
    assert len(sent) == 2                                   # напоминание
    results.append([Check("Claude Code", True, "2.1"), Check("База знаний", True, "ок")])
    m.tick()
    assert len(sent) == 3 and "снова работает" in sent[-1][1]
    m.tick()
    assert len(sent) == 3


def test_claude_check_pings_only_when_idle(tmp_path, monkeypatch, repo_root):
    from bridge.claude_runner import ClaudeRunner, RunResult
    s = _settings(tmp_path)
    s.claude.binary = str(repo_root / "scripts" / "mock_claude.cmd")
    s.claude.config_dir = ""
    store = Store(tmp_path / "s.sqlite3")
    asked = []
    real_run = ClaudeRunner.run

    def run(self, **kw):
        asked.append(self.settings.claude.model)
        return real_run(self, **kw)
    monkeypatch.setattr(ClaudeRunner, "run", run)
    assert monitor.check_claude(s, store).ok and asked == ["claude-haiku-5-5"]   # за час ответов не было — спросил
    store.log(channel="connect", user_key="u", user_name=None, base_id=None, question="q", status="ok")
    assert monitor.check_claude(s, store).ok and len(asked) == 1                 # бот отвечал — не тратим ни запроса

    monkeypatch.setattr(ClaudeRunner, "run", lambda self, **kw: RunResult(
        text="You've hit your org's monthly spend limit", is_error=True, has_result=True,
        error="You've hit your org's monthly spend limit"))
    store._exec("DELETE FROM journal")
    bad = monitor.check_claude(s, store)
    assert not bad.ok and "monthly spend limit" in bad.text


def test_line_and_kb_checks(tmp_path):
    s = _settings(tmp_path)
    s.connect.line_id = "L"
    line = tmp_path / "line.json"
    line.write_text(json.dumps({"hooked": False, "error": "SetHook 403"}), encoding="utf-8")
    assert "SetHook 403" in monitor.check_line(s, line).text
    line.write_text(json.dumps({"hooked": True}), encoding="utf-8")
    assert monitor.check_line(s, line).ok
    s.knowledge_base.root, s.knowledge_base.index_path = tmp_path, tmp_path / "none.sqlite3"
    assert not monitor.check_kb(s).ok


def test_recipients_by_name(tmp_path, monkeypatch, repo_root):
    from fastapi.testclient import TestClient

    from bridge.app import create_app

    s = _settings(tmp_path)
    s.users_path, s.bases_path = tmp_path / "u.yaml", tmp_path / "b.yaml"
    s.users_path.write_text("allow_all: true\n", encoding="utf-8")
    s.bases_path.write_text("", encoding="utf-8")
    s.knowledge_base.root, s.knowledge_base.index_path = tmp_path / "kb", tmp_path / "kb.sqlite3"
    s.monitor.interval_minutes = 0
    s.connect.bot_specialist_id = "bot"
    sent = []

    class Connect:
        def send_colleague_message(self, **kw):
            sent.append(kw)

    app = create_app(s, connect=Connect())
    with TestClient(app):
        app.state.store.set_state("directory", json.dumps({"loaded_at": 9999999999, "people": [
            {"id": "11111111-1111-1111-1111-111111111111", "surname": "Конжуровский", "name": "Данил"},
            {"id": "22222222-2222-2222-2222-222222222222", "surname": "Иванов", "name": "Иван"}]}, ensure_ascii=False))
        mon = app.state.monitor
        assert mon.recipients() == ["11111111-1111-1111-1111-111111111111"]
        mon.send("11111111-1111-1111-1111-111111111111", "тест")
    assert sent == [{"recipient_id": "11111111-1111-1111-1111-111111111111", "author_id": "bot", "text": "тест"}]


def test_check_must_fail_three_times(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    store = Store(tmp_path / "s.sqlite3")
    calls = {"n": 0}

    def flaky(settings):
        calls["n"] += 1
        return Check("API 1С-Коннекта", calls["n"] >= 2, "нет связи" if calls["n"] < 2 else "отвечает")
    monkeypatch.setattr(monitor, "check_connect", flaky)
    monkeypatch.setattr(monitor, "check_claude", lambda st, sto: Check("Claude Code", True, "ok"))
    monkeypatch.setattr(monitor, "check_line", lambda st: Check("Линия", True, "ok"))
    monkeypatch.setattr(monitor, "check_kb", lambda st: Check("База знаний", True, "ok"))
    pauses = []
    checks = monitor.run_checks(s, store, sleep=pauses.append)
    assert all(c.ok for c in checks) and calls["n"] == 2 and pauses == [60]      # второй раз прошла — не пишем
    monkeypatch.setattr(monitor, "check_connect", lambda st: Check("API 1С-Коннекта", False, "HTTP 401"))
    checks = monitor.run_checks(s, store, sleep=lambda x: None)
    bad = [c for c in checks if not c.ok]
    assert len(bad) == 1 and "3 раза подряд" in bad[0].text
