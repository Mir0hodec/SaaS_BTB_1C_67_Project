"""Инструкции (docs/*.md → HTML), команды /инструкция, /toolkit, /расширение и «Справка» в панели."""
import re
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bridge import docs, onec_toolkit, toolkit_secrets
from bridge.access import User
from bridge.app import create_app
from bridge.assistant import Assistant, Job
from bridge.config import Settings
from bridge.store import Store

REPO = Path(__file__).resolve().parent.parent


def test_markdown_subset():
    html = docs.render_markdown(
        "# Заголовок\n\nТекст с **жирным**, `кодом <b>` и [ссылкой](https://example.com).\n\n"
        "1. Первый\n2. Второй\n   - вложенный\n   - ещё\n3. Третий\n\n"
        "| Колонка | Значение |\n|---|---|\n| a | `x` |\n\n"
        "> Внимание: <script>alert(1)</script>\n\n```\nкод <i>\n```\n\n5. Пятый\n")
    assert "<h1>Заголовок</h1>" in html
    assert "<b>жирным</b>" in html and "<code>кодом &lt;b&gt;</code>" in html
    assert '<a href="https://example.com" target="_blank" rel="noopener">ссылкой</a>' in html
    assert "<ol><li>Первый</li><li>Второй<ul><li>вложенный</li><li>ещё</li></ul></li><li>Третий</li></ol>" in html
    assert "<table><thead><tr><th>Колонка</th>" in html and "<td><code>x</code></td>" in html
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<pre><code>код &lt;i&gt;</code></pre>" in html
    assert '<ol start="5">' in html


@pytest.mark.parametrize("doc_id", list(docs.DOCS))
def test_real_docs_render_cleanly(doc_id):
    title, path = docs.DOCS[doc_id]
    html = docs.render_markdown(path.read_text(encoding="utf-8"))
    assert "<h1>" in html and "<table>" in html and "<ol" in html
    for leftover in ("**", "```", "|---", "](http"):
        assert leftover not in html, leftover
    name, page = docs.standalone_html(doc_id)
    assert name == f"{title}.html" and page.startswith("<!doctype html>") and "<style>" in page


def test_instruction_file_follows_markdown(tmp_path, monkeypatch):
    md = tmp_path / "emp.md"
    md.write_text("# Версия 1\n", encoding="utf-8")
    monkeypatch.setitem(docs.DOCS, "employees", ("Инструкция для сотрудников", md))
    f = docs.instruction_file(tmp_path / "out")
    assert "Версия 1" in f.read_text(encoding="utf-8")
    md.write_text("# Версия 2\n", encoding="utf-8")
    import os
    os.utime(md, (f.stat().st_mtime + 5, f.stat().st_mtime + 5))
    assert "Версия 2" in docs.instruction_file(tmp_path / "out").read_text(encoding="utf-8")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(toolkit_secrets, "DEFAULT_PATH", tmp_path / "secrets.json")
    ext = tmp_path / "extensions"
    ext.mkdir()
    (ext / docs.EXTENSIONS_README).write_text("readme", encoding="utf-8")
    monkeypatch.setattr(docs, "EXTENSIONS_DIR", ext)
    kit = tmp_path / "MCP_Toolkit.epf"
    kit.write_bytes(b"epf")
    monkeypatch.setattr(docs, "toolkit_files", lambda: [kit])
    s = Settings.load(REPO / "config" / "settings.example.yaml")
    s.bases_path = tmp_path / "bases.yaml"
    s.bases_path.write_text("", encoding="utf-8")
    s.users_path = tmp_path / "users.yaml"
    s.users_path.write_text("allow_all: true\n", encoding="utf-8")
    s.data_dir = tmp_path / "data"
    s.knowledge_base.root, s.knowledge_base.index_path = tmp_path / "kb", tmp_path / "kb.sqlite3"
    return s, ext


def _cmd(settings, text):
    a = Assistant(settings, Store(settings.data_dir / "s.sqlite3"), runner=None)
    return a.handle(Job(channel="connect", user=User(key="u1", name="Иван", bases="*"), text=text))


def test_commands_send_instruction_and_extension(env):
    settings, ext = env
    r = _cmd(settings, "/инструкция")
    assert r.status == "command" and [f.name for f in r.files] == ["Инструкция для сотрудников.html"]
    assert "<h1>" in r.files[0].read_text(encoding="utf-8")

    assert "пока нет" in _cmd(settings, "/расширение").text
    r = _cmd(settings, "/toolkit")
    assert [f.name for f in r.files] == ["MCP_Toolkit.epf", "Инструкция для сотрудников.html"]
    assert "расширениями" not in r.text

    (ext / "ЭкспертДоработки.cfe").write_bytes(b"cfe")
    r = _cmd(settings, "/расширение")
    assert [f.name for f in r.files] == ["ЭкспертДоработки.cfe"] and "Добавить из файла" in r.text
    r = _cmd(settings, "/toolkit")
    assert r.files[-1].name == "ЭкспертДоработки.cfe" and "Добавить из файла" in r.text


class FakeConnect:
    def send_colleague_message(self, **kw):
        pass


def test_panel_help_and_kit(env, tmp_path):
    settings, ext = env
    (ext / "доработка.cfe").write_bytes(b"cfe")
    settings_file = tmp_path / "settings.yaml"
    shutil.copy(REPO / "config" / "settings.example.yaml", settings_file)
    settings.source = settings_file
    app = create_app(settings, connect=FakeConnect(), colleague_source=lambda on, stop: None)
    client = TestClient(app, base_url=f"http://127.0.0.1:{settings.port}", client=("127.0.0.1", 50000))
    with client:
        token = re.search(r'name="panel-token" content="([^"]+)"', client.get("/panel").text).group(1)
        auth = {"X-Panel-Token": token}
        assert client.get("/panel/api/docs").status_code == 403                       # без токена панели
        ids = [d["id"] for d in client.get("/panel/api/docs", headers=auth).json()["docs"]]
        assert ids == ["employees", "admin"]
        d = client.get("/panel/api/docs/admin", headers=auth).json()
        assert d["file_name"] == "Инструкция администратора.html" and "<table>" in d["html"]
        assert client.get("/panel/api/docs/../../config", headers=auth).status_code == 404
        assert client.get("/panel/api/docs/secret", headers=auth).status_code == 404
        b = client.get("/panel/api/bases", headers=auth).json()
        assert [f["name"] for f in b["kit"]] == ["MCP_Toolkit.epf"]
        assert [f["name"] for f in b["extensions"]] == ["доработка.cfe"]


def test_wrong_token_is_reported_before_claude(monkeypatch):
    class Resp:
        status_code = 401
    monkeypatch.setattr(onec_toolkit, "health", lambda url, timeout=3.0: (True, ""))
    monkeypatch.setattr(onec_toolkit.requests, "post", lambda *a, **kw: Resp())
    called = []
    monkeypatch.setattr(onec_toolkit, "call", lambda *a, **kw: called.append(1))
    res = onec_toolkit.probe("http://10.0.0.5:6003/mcp", "old-token")
    assert not res.ok and res.message == onec_toolkit.WRONG_TOKEN and not called
