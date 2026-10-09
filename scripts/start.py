"""Запуск помощника одной командой: мост + HTTPS-туннель + подписка вебхука.

    python scripts/start.py

1. Если нет токена вебхука — генерирует и дописывает в .env.
2. Поднимает мост (uvicorn на 127.0.0.1, порт — port в settings.yaml, наружу порт не открывается).
3. webhook_mode: tunnel — запускает Cloudflare quick tunnel: он даёт адрес
   https://…trycloudflare.com без домена, сертификата и открытых портов
   (соединение исходящее, как long-polling у Telegram-бота). Адрес
   прописывается в 1С-Коннект (SetHook). Туннель перезапустился — новый адрес
   прописывается снова.
   webhook_mode: url — прописывает connect.public_url один раз.
4. Если мост или туннель упали — поднимает заново.
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common.win_utf8 import ensure_utf8_stdio  # noqa: E402

ensure_utf8_stdio()

from bridge.config import Settings  # noqa: E402
from bridge.connect_client import ConnectClient, ConnectError  # noqa: E402



def port() -> int:
    try:
        return Settings.load().port
    except FileNotFoundError:
        return 8010
TUNNEL_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
LOG_DIR = ROOT / "logs"
# Без окон консоли: клик по окну консоли Windows ставит процесс на паузу.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def say(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    LOG_DIR.mkdir(exist_ok=True)
    with open(LOG_DIR / "start.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def ensure_webhook_token(env_name: str) -> None:
    if os.environ.get(env_name):
        return
    token = secrets.token_urlsafe(32)
    with open(ROOT / ".env", "a", encoding="utf-8") as f:
        f.write(f"\n{env_name}={token}\n")
    os.environ[env_name] = token
    say(f"сгенерирован {env_name} и записан в .env")


def start_bridge() -> subprocess.Popen:
    LOG_DIR.mkdir(exist_ok=True)
    out = open(LOG_DIR / "bridge.log", "a", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "bridge.app:app", "--host", "127.0.0.1", "--port", str(port())],
        cwd=ROOT, stdout=out, stderr=subprocess.STDOUT, env={**os.environ, "PYTHONUTF8": "1"},
        creationflags=NO_WINDOW,
    )
    for _ in range(60):
        try:
            if requests.get(f"http://127.0.0.1:{port()}/health", timeout=2).ok:
                say("мост запущен")
                return proc
        except requests.RequestException:
            pass
        if proc.poll() is not None:
            break
        time.sleep(1)
    raise RuntimeError("мост не запустился — см. logs/bridge.log")


def find_cloudflared() -> str:
    local = ROOT / "tools" / "cloudflared.exe"
    found = str(local) if local.exists() else shutil.which("cloudflared")
    if not found:
        raise RuntimeError("не найден cloudflared: запустите setup.ps1 (он его скачает)")
    return found


def start_tunnel() -> tuple[subprocess.Popen, str]:
    proc = subprocess.Popen(
        [find_cloudflared(), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port()}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        creationflags=NO_WINDOW,
    )
    deadline = time.monotonic() + 90
    log = open(LOG_DIR / "tunnel.log", "a", encoding="utf-8")
    for line in proc.stdout:
        log.write(line)
        log.flush()
        m = TUNNEL_URL_RE.search(line)
        if m:
            # Дальше лог туннеля просто дописываем в файл, чтобы не забить буфер.
            threading.Thread(target=lambda: [log.write(ln) or log.flush() for ln in proc.stdout],
                             daemon=True).start()
            return proc, m.group(0)
        if time.monotonic() > deadline:
            break
    proc.kill()
    raise RuntimeError("туннель не выдал адрес — см. logs/tunnel.log")


def set_hook(settings: Settings, base_url: str) -> None:
    cs = settings.connect
    missing = [n for n, v in (("CONNECT_API_LOGIN", cs.login), ("CONNECT_API_PASSWORD", cs.password),
                              ("CONNECT_LINE_ID", cs.line_id)) if not v]
    if missing:
        say("вебхук НЕ прописан: не заданы " + ", ".join(missing) + " (см. «Доступы ИИ-помощника.docx»)")
        return
    url = base_url.rstrip("/") + "/connect/hook"
    for attempt in range(5):
        try:
            ConnectClient(cs.api_base_url, cs.login, cs.password).set_hook(
                line_id=cs.line_id, url=url, token=cs.webhook_token)
            say(f"вебхук 1С-Коннект → {url}")
            return
        except ConnectError as exc:
            say(f"SetHook не удался ({exc}), повтор через 10 с")
            time.sleep(10)


def main() -> int:
    settings = Settings.load()
    colleague = settings.connect.channel == "colleague"
    # Токен нужен и в режиме личных сообщений: им закрыт HTTP-приёмник
    # /connect/colleague/message (проверка, будущий push от 1С-Коннект).
    ensure_webhook_token(settings.connect.webhook_token_env)
    settings = Settings.load()
    mode = settings.connect.webhook_mode

    bridge = start_bridge()
    tunnel = None
    if colleague:
        # Личные сообщения: приём через десктопное приложение 1С-Коннект на
        # этой машине, ответы через REST. Вебхук и туннель не нужны, SetHook
        # не вызывается — действующие интеграции Коннекта не трогаем.
        say("режим личных сообщений: жду 1С-Коннект под учёткой бота (см. logs/bridge.log)")
    elif mode == "url" and settings.connect.public_url:
        set_hook(settings, settings.connect.public_url)
    elif mode == "tunnel":
        tunnel, url = start_tunnel()
        say(f"туннель: {url}")
        set_hook(settings, url)

    while True:
        time.sleep(5)
        if bridge.poll() is not None:
            say("мост остановился — перезапуск")
            bridge = start_bridge()
        if tunnel is not None and tunnel.poll() is not None:
            say("туннель остановился — перезапуск")
            tunnel, url = start_tunnel()
            say(f"туннель: {url}")
            set_hook(settings, url)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as exc:  # noqa: BLE001
        say(f"ОШИБКА: {exc}")
        sys.exit(1)
