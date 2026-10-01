import platform
import sys

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "scripts"))
import start  # noqa: E402

from bridge.config import Settings  # noqa: E402


@pytest.mark.skipif(platform.system() != "Windows", reason="фейковый cloudflared — .cmd")
def test_tunnel_url_is_caught_and_hook_is_set(tmp_path, monkeypatch, repo_root):
    fake = tmp_path / "cloudflared.cmd"
    fake.write_text("@echo off\r\necho INF Requesting new quick Tunnel\r\n"
                    "echo INF ^|  https://quiet-river-1234.trycloudflare.com  ^|\r\n"
                    "ping -n 30 127.0.0.1 >nul\r\n", encoding="ascii")
    monkeypatch.setattr(start, "find_cloudflared", lambda: str(fake))
    monkeypatch.setattr(start, "LOG_DIR", tmp_path)
    proc, url = start.start_tunnel()
    start.subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    assert url == "https://quiet-river-1234.trycloudflare.com"

    hooks = []

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def set_hook(self, **kw):
            hooks.append(kw)

    monkeypatch.setattr(start, "ConnectClient", FakeClient)
    for k, v in {"CONNECT_API_LOGIN": "l", "CONNECT_API_PASSWORD": "p", "CONNECT_LINE_ID": "line",
                 "CONNECT_WEBHOOK_TOKEN": "tok"}.items():
        monkeypatch.setenv(k, v)
    start.set_hook(Settings.load(repo_root / "config" / "settings.example.yaml"), url)
    assert hooks == [{"line_id": "line", "url": url + "/connect/hook", "token": "tok"}]


def test_hook_skipped_without_credentials(monkeypatch, repo_root, tmp_path, capsys):
    monkeypatch.setattr(start, "LOG_DIR", tmp_path)
    for k in ("CONNECT_API_LOGIN", "CONNECT_API_PASSWORD", "CONNECT_LINE_ID"):
        monkeypatch.delenv(k, raising=False)
    start.set_hook(Settings.load(repo_root / "config" / "settings.example.yaml"), "https://x.trycloudflare.com")
    assert "вебхук НЕ прописан" in capsys.readouterr().out


def test_webhook_token_generated_once(monkeypatch, tmp_path):
    monkeypatch.setattr(start, "ROOT", tmp_path)
    monkeypatch.setattr(start, "LOG_DIR", tmp_path)
    monkeypatch.delenv("CONNECT_WEBHOOK_TOKEN", raising=False)
    start.ensure_webhook_token("CONNECT_WEBHOOK_TOKEN")
    token = (tmp_path / ".env").read_text(encoding="utf-8").strip().split("=", 1)[1]
    assert len(token) >= 40
    start.ensure_webhook_token("CONNECT_WEBHOOK_TOKEN")      # уже есть — второй раз не пишет
    assert (tmp_path / ".env").read_text(encoding="utf-8").count("CONNECT_WEBHOOK_TOKEN") == 1
