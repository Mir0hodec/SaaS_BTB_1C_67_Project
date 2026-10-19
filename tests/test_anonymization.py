"""Обезличивание опубликованных баз (MCP_HTTPService 0.5+): привязка моста, правила, расшифровка ответа и файлов.

Вместо 1С — HTTP-сервер с контрактом расширения 0.5: /anonymization, /detokenize и /query с метками.
"""
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import unquote

import pytest
from openpyxl import Workbook, load_workbook

from bridge import anonymization, toolkit_secrets
from bridge.access import User
from bridge.assistant import Assistant, Job
from bridge.claude_runner import RunResult
from bridge.config import Settings
from bridge.store import Store

REPO = Path(__file__).resolve().parent.parent
GOOD_AUTH = "Basic " + base64.b64encode("Администратор:1".encode()).decode()
VALUES = {"[ОРГ-0BCA66]": "ТОО Ромашка", "[ИИН-F50265]": "090140001122"}


class Fake1C05(BaseHTTPRequestHandler):
    state: dict = {}

    def log_message(self, *a):
        pass

    def _send(self, code, body):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _route(self, method):
        if self.headers.get("Authorization") != GOOD_AUTH:
            return self._send(401, {"error": "auth"})
        path = unquote(self.path)[len("/Base1/hs/mcp-1c"):]
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}") \
            if method == "POST" else {}
        st = Fake1C05.state
        status = lambda: {"version": "0.5.0", "enabled": True, "paired": bool(st.get("key")),  # noqa: E731
                          "rules_revision": st.get("revision", ""), "categories": ["ОРГ", "ИИН"]}
        if path == "/version":
            return self._send(200, {"version": "0.5.0"})
        if path == "/configuration":
            return self._send(200, {"name": "БухгалтерияДляКазахстана", "version": "3.0.40", "platform_version": "8.3.24",
                                    "anonymization": True})
        if path == "/anonymization" and method == "GET":
            return self._send(200, status())
        if path == "/anonymization":
            if st.get("key") and body.get("key") != st["key"]:
                return self._send(403, {"error": "wrong bridge key"})
            st.setdefault("key", body["key"])
            if "rules" in body:
                st["rules"], st["revision"] = body["rules"], body.get("revision", "")
                st["pushes"] = st.get("pushes", 0) + 1
            return self._send(200, status())
        if path == "/detokenize":
            if body.get("key") != st.get("key"):
                return self._send(403, {"error": "wrong bridge key"})
            out = []
            for text in body["texts"]:
                for token, value in VALUES.items():
                    text = text.replace(token, value)
                out.append(text)
            return self._send(200, {"texts": out})
        if path == "/query":
            return self._send(200, {"columns": ["Наименование", "БИН"], "rows": [list(VALUES)], "total": 1,
                                    "truncated": False, "anonymized": True})
        return self._send(404, {"error": "unknown"})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")


@pytest.fixture
def base_url(tmp_path, monkeypatch):
    monkeypatch.setattr(toolkit_secrets, "DEFAULT_PATH", tmp_path / "secrets.json")
    monkeypatch.setattr(anonymization, "CONFIG_RULES", tmp_path / "anonymization.yaml")
    Fake1C05.state = {}
    server = HTTPServer(("127.0.0.1", 0), Fake1C05)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/Base1"
    server.shutdown()


def test_rules_defaults_and_editing(tmp_path, monkeypatch):
    monkeypatch.setattr(anonymization, "CONFIG_RULES", tmp_path / "anonymization.yaml")
    rules = anonymization.load_rules()
    assert {"ОРГ", "ИИН", "СЧЕТ", "АДР", "ДОК"} <= set(rules["categories"]) and rules["enabled"]
    assert ["=СИК", "СЧЕТ"] in rules["fields"] and ["КонтактнаяИнформация", "АДР"] in rules["tables_all"]
    rules["categories"] = [c for c in rules["categories"] if c != "ДОК"] + ["НЕТ ТАКОЙ"]
    rules["keep"].append("НомерВходящегоДокумента")
    anonymization.save_rules(rules)
    again = anonymization.load_rules()
    assert "ДОК" not in again["categories"] and "НЕТ ТАКОЙ" not in again["categories"]
    assert "НомерВходящегоДокумента" in again["keep"]
    assert anonymization.revision(again) != anonymization.revision(anonymization.load_rules(anonymization.DEFAULT_RULES))


def test_bridge_pairs_once_and_pushes_rules_on_change(base_url):
    first = anonymization.ensure("base1", base_url)
    assert first.supported and first.enabled and first.key
    assert toolkit_secrets.get_token("base1#anon") == first.key == Fake1C05.state["key"]
    assert Fake1C05.state["pushes"] == 1 and Fake1C05.state["rules"]["categories"]
    assert anonymization.ensure("base1", base_url).key == first.key and Fake1C05.state["pushes"] == 1
    rules = anonymization.load_rules()
    rules["categories"].remove("ДОК")
    anonymization.save_rules(rules)
    anonymization.ensure("base1", base_url)
    assert Fake1C05.state["pushes"] == 2 and "ДОК" not in Fake1C05.state["rules"]["categories"]
    toolkit_secrets.set_token("base1#anon", "чужой-ключ-" + "x" * 30)     # база привязана к другому мосту
    other = anonymization.ensure("base1", base_url)
    assert other.enabled and not other.key and "ключ моста не тот" in other.detail


def test_old_extension_is_reported_not_broken(tmp_path, monkeypatch):
    monkeypatch.setattr(toolkit_secrets, "DEFAULT_PATH", tmp_path / "secrets.json")
    state = anonymization.ensure("old", "http://127.0.0.1:9/nobase")
    assert not state.supported and not state.key and "без обезличивания" in state.detail


class Runner:
    """Claude отвечает метками и кладёт xlsx с метками в OUTBOX — как export_query_to_excel."""
    def __init__(self):
        self.calls = []

    def run(self, **kw):
        self.calls.append(kw)
        outbox = Path(json.loads(Path(kw["mcp_config"]).read_text(encoding="utf-8"))
                      ["mcpServers"]["files"]["env"]["OUTBOX_DIR"])
        outbox.mkdir(parents=True, exist_ok=True)
        wb = Workbook()
        wb.active.append(["Контрагент", "БИН", "Сумма"])
        wb.active.append(["[ОРГ-0BCA66]", "[ИИН-F50265]", 1075000])
        wb.save(outbox / "контрагенты.xlsx")
        return RunResult(text="Долг у [ОРГ-0BCA66] (БИН [ИИН-F50265]) — 1 075 000 тг.", is_error=False,
                         session_total_cost=0.01, has_result=True)


def test_answer_and_files_are_decrypted_journal_keeps_tokens(base_url, tmp_path):
    s = Settings.load(REPO / "config" / "settings.example.yaml")
    s.bases_path = tmp_path / "bases.yaml"
    s.bases_path.write_text(f'pub:\n  title: "Бухгалтерия"\n  mode: extension\n  url: "{base_url}"\n', encoding="utf-8")
    s.users_path = tmp_path / "users.yaml"
    s.users_path.write_text("allow_all: true\n", encoding="utf-8")
    s.data_dir = tmp_path / "data"
    s.knowledge_base.root, s.knowledge_base.index_path = tmp_path / "kb", tmp_path / "kb.sqlite3"
    runner = Runner()
    store = Store(s.data_dir / "s.sqlite3")
    a = Assistant(s, store, runner=runner)
    reply = a.handle(Job(channel="connect", user=User(key="u1", name="Иван", bases="*"),
                         text=f"{base_url} у кого долг?"))
    assert reply.status == "ok"
    assert reply.text == "Долг у ТОО Ромашка (БИН 090140001122) — 1 075 000 тг."
    assert "Данные обезличены" in runner.calls[0]["question"]
    rows = list(load_workbook(reply.files[0]).active.iter_rows(values_only=True))
    assert rows[1] == ("ТОО Ромашка", "090140001122", 1075000)
    journal = store.journal_entry(store.journal_page(limit=1)[0]["id"])
    assert "[ОРГ-0BCA66]" in journal["answer"] and "Ромашка" not in journal["answer"]
    assert "anon" not in json.dumps(runner.calls[0]["extra_env"] or {})          # ключа моста у Claude нет


def test_without_key_answer_keeps_tokens_with_note(base_url, tmp_path):
    a = Assistant.__new__(Assistant)
    a.anon_states = {"pub": anonymization.State(True, True, "", "")}
    from bridge.access import Base
    base = Base(id="pub", title="Б", mode="extension", url=base_url)
    text = a._detokenize(base, "Контрагент [ОРГ-0BCA66]", [])
    assert "[ОРГ-0BCA66]" in text and "нет ключа" in text
