import asyncio
import json
import platform
import shutil
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bridge.access import load_bases
from bridge import onec_extension
from bridge.answer import clean_answer, split_message
from bridge.app import create_app
from bridge.assistant import Assistant
from bridge.claude_runner import forbidden_tools, parse_limit
from bridge.config import Settings
from bridge.store import Store
from common.yaml_edit import set_value

windows_only = pytest.mark.skipif(platform.system() != "Windows", reason="заглушка CLI — .cmd-шим Windows")
LINE, BOT, USER = "line-1", "bot-spec", "00000000-0000-0000-0000-000000000001"
DEMO_LINK = "http://demo.local/Demo"


@pytest.fixture
def settings(repo_root, tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    # Строгий режим: отвечаем только тем, кто в списке.
    (cfg / "users.yaml").write_text(
        "allow_all: false\n"
        "users:\n"
        f'  "{USER}": {{name: "Иванов И.", bases: "*", owner: true}}\n',
        encoding="utf-8")
    shutil.copy(repo_root / "config" / "bases.example.yaml", cfg / "bases.yaml")
    set_value(cfg / "bases.yaml", ("demo_base", "url"), DEMO_LINK)    # демо-базу тоже выбирают ссылкой
    s = Settings.load(repo_root / "config" / "settings.example.yaml")
    s.users_path, s.bases_path, s.data_dir = cfg / "users.yaml", cfg / "bases.yaml", tmp_path / "data"
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "Начисление пени.txt").write_text("Пеня начисляется документом.", encoding="utf-8")
    s.knowledge_base.root, s.knowledge_base.index_path = kb, tmp_path / "kb.sqlite3"
    s.claude.binary = str(repo_root / "scripts" / "mock_claude.cmd")
    s.claude.config_dir = ""
    s.connect.line_id, s.connect.bot_specialist_id = LINE, BOT
    s.connect.batch_seconds = 0        # по сообщению на вопрос; склейку проверяет отдельный тест
    monkeypatch.setenv("CONNECT_WEBHOOK_TOKEN", "hook-secret")
    monkeypatch.setenv("ASSISTANT_API_TOKEN_LANGFLOW", "api-secret")
    return s


class FakeConnect:
    def __init__(self):
        self.messages, self.files = [], []

    def send_message(self, *, line_id, user_id, author_id, text):
        self.messages.append((user_id, text))

    def send_file(self, *, line_id, user_id, author_id, path, comment=None):
        self.files.append((user_id, path.name))

    def download(self, url, dest, *, max_bytes, auth_hosts):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("Выписка: остаток 100 000 тенге", encoding="utf-8")
        return dest


def _event(text=None, *, msg_id, user=USER, author=None, mtype=1, file=None):
    e = {"event_type": "line", "message_type": mtype, "line_id": LINE, "user_id": user,
         "author_id": author or user, "message_id": msg_id}
    if text is not None:
        e["text"] = text
    if file:
        e["file"] = file
    return e


def _drain(client):
    client.app.state.batcher.flush()
    client.app.state.worker.queue.join()


# --- выбор базы, чистка ответа, лимиты, защита ---

def test_links_in_message():
    assert onec_extension.find_links("глянь http://srv/База/ru_RU/, остатки") == \
        [("http://srv/База/ru_RU/", "глянь остатки")]
    assert onec_extension.same_link("http://srv/База", "http://SRV/база/ru_RU/#e1cib/list/Документ.X")
    assert onec_extension.same_link("http://10.0.0.5:6003/mcp", "http://10.0.0.5:6003/mcp/")
    assert not onec_extension.same_link("http://srv/База", "http://srv/Другая")
    assert onec_extension.is_toolkit_url("http://10.0.0.5:6003/mcp")
    assert not onec_extension.is_toolkit_url("http://srv/mcp")
    assert onec_extension.find_publication("http://10.0.0.5:6003/mcp остатки") is None


def test_base_is_chosen_by_link_not_hashtag(settings):
    from bridge.assistant import Job
    a = Assistant(settings, Store(settings.data_dir / "b.sqlite3"))
    user = a.users()[USER]
    picked = a.handle(Job("connect", user, f"{DEMO_LINK}/"))
    assert picked.status == "command" and "Демо-база" in picked.text and "#" not in picked.text
    listed = a.handle(Job("connect", user, "/базы")).text
    assert DEMO_LINK in listed and "#" not in listed
    unknown = a.handle(Job("connect", user, "http://10.0.0.9:6003/mcp остатки"))
    assert unknown.status == "toolkit" and "нет в списке" in unknown.text
    assert "#" not in a.handle(Job("connect", user, "/помощь")).text


def test_cyrillic_base_id_rejected(tmp_path):
    p = tmp_path / "b.yaml"
    p.write_text("демо: {title: x}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="латиница"):
        load_bases(p)


def test_clean_answer_hides_paths_tools_markdown():
    out = clean_answer("## Итог\n**Остаток**: 5 (см. C:\\bot\\.data\\x.json, mcp__onec__run_query)")
    assert "C:\\" not in out and "mcp__" not in out and "**" not in out and "##" not in out
    assert "Остаток" in out


def test_split_message_respects_limit():
    parts = split_message(("абзац " * 50 + "\n\n") * 20, 400)
    assert all(len(p) <= 400 for p in parts) and len(parts) > 1


def test_parse_limit_reset_time():
    hit, reset = parse_limit("Your limit will reset at 6:20pm", now=datetime(2026, 9, 27, 12, 0))
    assert hit and reset == datetime(2026, 9, 27, 18, 20)
    assert parse_limit("всё хорошо") == (False, None)


def test_forbidden_tools_detects_terminal_and_foreign_mcp():
    allowed = ["mcp__files__create_excel"]
    assert forbidden_tools(["mcp__files__create_excel", "EndConversation"], allowed) == []
    assert forbidden_tools(["Bash", "mcp__gmail__send"], allowed) == ["Bash", "mcp__gmail__send"]


# --- сессии ---

def test_session_follows_base_and_idle(tmp_path):
    st = Store(tmp_path / "s.sqlite3")
    a = st.session("u", "demo_base", 60, keep_base=False)
    unanswered = st.session("u", None, 60, keep_base=True)   # Claude ещё не отвечал — разговор не начат
    st.mark_started("u", a.session_id, "demo_base", "первый вопрос", 0.05)
    b = st.session("u", None, 60, keep_base=True)          # без ссылки — та же база, тот же разговор
    c = st.session("u", "other", 60, keep_base=False)      # другая база — новый разговор
    assert a.is_new and unanswered.is_new and unanswered.session_id == a.session_id
    assert not b.is_new and b.base_id == "demo_base" and c.is_new
    st._exec("UPDATE sessions SET last_active='2000-01-01T00:00:00+00:00'")
    d = st.session("u", None, 60, keep_base=True)          # после простоя — заново, без базы
    assert d.is_new and d.base_id is None
    assert [x["title"] for x in st.conversations("u")] == ["первый вопрос"]   # прошлый разговор — в архиве


def test_session_restart_keeps_base_and_asks_recap(tmp_path):
    st = Store(tmp_path / "s.sqlite3")
    a = st.session("u", "test_base", 60, keep_base=False)
    st.mark_started("u", a.session_id, "test_base", "вопрос", None)
    st.restart_session("u")                                # разговор Claude сломан — заново
    b = st.session("u", None, 60, keep_base=True)          # «исправь» без ссылки
    st.mark_started("u", b.session_id, "test_base", "исправь", None)
    c = st.session("u", None, 60, keep_base=True)
    assert b.is_new and b.recap and b.base_id == "test_base" and b.session_id != a.session_id
    assert not c.is_new and not c.recap and c.session_id == b.session_id


# --- 1С-Коннект целиком (заглушка CLI) ---

@windows_only
def test_connect_webhook_end_to_end(settings):
    fake = FakeConnect()
    app = create_app(settings, connect=fake)
    with TestClient(app) as client:
        hdr = {"Authorization": "Bearer hook-secret"}
        assert client.post("/connect/hook", json=_event("x", msg_id="0"), headers={}).status_code == 401
        client.post("/connect/hook", json=_event(f"{DEMO_LINK} остатки по кассе", msg_id="1"), headers=hdr)
        client.post("/connect/hook", json=_event(f"{DEMO_LINK} остатки по кассе", msg_id="1"), headers=hdr)   # дубль
        client.post("/connect/hook", json=_event("эхо бота", msg_id="2", author=BOT), headers=hdr)     # бот сам
        client.post("/connect/hook", json=_event("привет", msg_id="3", user="stranger"), headers=hdr)  # чужой
        client.post("/connect/hook", json=_event("выгрузи [mock:file]", msg_id="4"), headers=hdr)
        _drain(client)

    texts = [t for u, t in fake.messages]
    answers = [t for t in texts if t.startswith("[mock]")]
    assert len(answers) == 2                                # дубль, эхо бота и чужой — без ответа
    assert all(u == USER for u, _ in fake.messages)
    assert "Демо-база" in answers[0] and "onec" in answers[0]
    assert "Демо-база" in answers[1]                        # без ссылки — та же база
    assert fake.files == [(USER, "отчет.txt")]
    assert "[путь скрыт]" in answers[0] and "C:\\secret" not in answers[0]
    journal = Store(settings.data_dir / "bridge.sqlite3").journal()
    assert [r["status"] for r in journal].count("unknown_user") == 1


@windows_only
def test_file_then_question_and_pause(settings):
    fake = FakeConnect()
    app = create_app(settings, connect=fake)
    with TestClient(app) as client:
        hdr = {"Authorization": "Bearer hook-secret"}
        client.post("/connect/hook", headers=hdr, json=_event(msg_id="f1", mtype=70, file={
            "file_id": "abc", "file_path": "https://filetransfer.buhphone.com/x", "file_name": "выписка.txt"}))
        client.post("/connect/hook", json=_event("что в выписке?", msg_id="q1"), headers=hdr)
        client.post("/connect/hook", json=_event("/пауза", msg_id="p1"), headers=hdr)
        client.post("/connect/hook", json=_event("вопрос на паузе", msg_id="q2"), headers=hdr)
        client.post("/connect/hook", json=_event("/старт", msg_id="p2"), headers=hdr)
        _drain(client)
    texts = [t for _, t in fake.messages]
    assert any("Файл «выписка.txt» получил" in t for t in texts)
    assert any("остаток 100 000 тенге" in t for t in texts)          # текст вложения дошёл до модели
    assert "Помощник временно выключен. Попробуйте позже." in texts
    assert "Помощник снова работает." in texts


@windows_only
def test_limit_blocks_further_calls_and_extra_tool_aborts(settings):
    st = Store(settings.data_dir / "bridge.sqlite3")
    assistant = Assistant(settings, st)
    user = assistant.users()[USER]
    from bridge.assistant import Job
    guard = assistant.handle(Job("cli", user, "вопрос [mock:extra-tool]"))
    assert guard.status == "error"
    assert "лишние инструменты: Bash" in st.journal()[-1]["error"]

    first = assistant.handle(Job("cli", user, "вопрос [mock:limit]"))
    assert first.status == "limit" and "18:20" in first.text
    second = assistant.handle(Job("cli", user, "обычный вопрос"))
    assert second.status == "limit"                                  # без нового запуска CLI
    assert [r["status"] for r in st.journal()].count("limit") == 1


@windows_only
def test_api_bridge(settings):
    app = create_app(settings, connect=FakeConnect())
    with TestClient(app) as client:
        assert client.post("/api/ask", json={"question": "x"}).status_code == 401
        r = client.post("/api/ask", json={"question": "сводка [mock:file]", "base": "demo_base"},
                        headers={"Authorization": "Bearer api-secret"}).json()
    assert r["status"] == "ok" and "Демо-база" in r["answer"]
    assert r["files"][0]["name"] == "отчет.txt"


@windows_only
def test_secrets_not_passed_to_agent(settings, monkeypatch):
    from bridge.claude_runner import ClaudeRunner
    monkeypatch.setenv("CONNECT_API_PASSWORD", "p@ss")
    env = ClaudeRunner(settings)._env()
    assert "CONNECT_API_PASSWORD" not in env and "CONNECT_WEBHOOK_TOKEN" not in env
    assert "ASSISTANT_API_TOKEN_LANGFLOW" not in env and env["MCP_TIMEOUT"] == "30000"
    monkeypatch.setenv("COUNTERPARTY_CHECK_PASSWORD", "x")
    # Пароль сервиса проверки нужен MCP-серверу — он наследует окружение claude.
    assert ClaudeRunner(settings)._env()["COUNTERPARTY_CHECK_PASSWORD"] == "x"


# --- доступ без списка, /id, ID линии из окружения, автопересборка индекса ---

def test_allow_all_line_users_and_id_command(settings, repo_root):
    from bridge.access import resolve_user
    from bridge.assistant import Job
    shutil.copy(repo_root / "config" / "users.example.yaml", settings.users_path)
    anyone = resolve_user(settings.users_path, "new-employee-id")
    assert anyone is not None and anyone.bases == "*" and not anyone.owner
    reply = Assistant(settings, Store(settings.data_dir / "b.sqlite3")).handle(Job("connect", anyone, "/id"))
    assert reply.text == "Ваш user_id: new-employee-id"
    assert "/пауза" not in reply.text


def test_strict_mode_ignores_unknown(settings):
    from bridge.access import resolve_user
    assert resolve_user(settings.users_path, "stranger") is None
    assert resolve_user(settings.users_path, USER).owner


def test_old_bot_line_is_never_taken(monkeypatch):
    """CONNECT_LINE_ID на сервере — линия старого бота: новый бот её не берёт и не подписывается."""
    from bridge.config import ConnectSettings
    monkeypatch.setenv("CONNECT_LINE_ID", "L-OLD")
    monkeypatch.setenv("CONNECT_BOT_SPECIALIST_ID", "S-1")
    cs = ConnectSettings()
    assert (cs.line_id, cs.bot_specialist_id) == ("", "S-1") and not cs.line_enabled
    assert ConnectSettings(line_id="L-AI").line_enabled
    assert not ConnectSettings(line_id="L-OLD").line_enabled


def test_knowledge_base_reindexes_when_folder_changes(settings):
    import os
    import time as _t
    from common.doc_index import DocIndex
    a = Assistant(settings, Store(settings.data_dir / "b.sqlite3"))
    a.refresh_knowledge_base(force=True)
    idx = DocIndex(settings.knowledge_base.index_path, readonly=True)
    assert [d["title"] for d in idx.list_documents()] == ["Начисление пени"]
    idx.close()

    new = settings.knowledge_base.root / "Курс валют.txt"
    new.write_text("Курсы валют загружаются из Нацбанка.", encoding="utf-8")
    a.refresh_knowledge_base()                      # ещё не прошло 5 минут — не проверяет
    a.refresh_knowledge_base(force=True)
    idx = DocIndex(settings.knowledge_base.index_path, readonly=True)
    assert len(idx.list_documents()) == 2
    idx.close()
    w = DocIndex(settings.knowledge_base.index_path)
    kb = settings.knowledge_base
    assert w.rebuild_if_changed(kb.root, kb.exclude) is None            # без изменений — не трогает
    assert w.rebuild_if_changed(kb.root, []) is not None                # поменяли исключения — пересборка
    w.close()


def test_old_dm_settings_are_ignored(tmp_path, repo_root):
    """В settings.yaml на сервере остались ключи личных сообщений — бот запускается и их не использует."""
    text = (repo_root / "config" / "settings.example.yaml").read_text(encoding="utf-8").replace(
        "connect:", 'connect:\n  channel: "colleague"\n  receive: "history"\n  history_colleagues: ["a"]\n'
        "  history_interval_seconds: 75\n  history_hours: 6\n  agent_login_env: X", 1)
    path = tmp_path / "settings.yaml"
    path.write_text(text, encoding="utf-8")
    cs = Settings.load(path).connect
    assert not hasattr(cs, "history_colleagues") and not hasattr(cs, "channel")
