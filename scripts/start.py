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

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
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
        cwd=ROOT, stdout=out, stderr=subprocess.STDOUT,
        env={**os.environ, "PYTHONUTF8": "1", "BRIDGE_SUPERVISED": "1"},   # панель может перезапустить
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


def set_hook(settings: Settings, base_url: str) -> bool:
    cs = settings.connect
    if cs.line_id and cs.line_id == cs.foreign_line_id:
        say("вебхук НЕ прописан: connect.line_id — линия старого бота (CONNECT_LINE_ID), её не трогаем")
        return False
    missing = [n for n, v in (("CONNECT_API_LOGIN", cs.login), ("CONNECT_API_PASSWORD", cs.password),
                              ("connect.line_id в settings.yaml", cs.line_id)) if not v]
    if missing:
        say("вебхук НЕ прописан: не заданы " + ", ".join(missing) + " (см. «Доступы ИИ-помощника.docx»)")
        return False
    url = base_url.rstrip("/") + "/connect/hook"
    for attempt in range(3):
        try:
            ConnectClient(cs.api_base_url, cs.login, cs.password).set_hook(
                line_id=cs.line_id, url=url, token=cs.webhook_token, kind=cs.line_hook)
            say(f"вебхук 1С-Коннект ({cs.line_hook}) → {url}")
            return True
        except ConnectError as exc:
            hint = (" — бот-подписка на учётку API одна (у старого бота): connect.line_hook: line"
                    if "403" in str(exc) and cs.line_hook == "bot" else "")
            say(f"SetHook не удался ({exc}){hint}")
            if attempt < 2:
                time.sleep(10)
    return False


class LineLink:
    """Своя линия: туннель и SetHook. Пока 1С-Коннект не примет вебхук (например, в карточке
    линии ещё не включён внешний сервер обработки), повтор раз в 5 минут — без перезапуска бота.
    Сбой туннеля не роняет бота: личные сообщения работают дальше."""
    RETRY_SECONDS = 300

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.tunnel: subprocess.Popen | None = None
        self.url = ""
        self.hooked = False
        self.next_try = 0.0
        self.line_id = self.kind = ""

    def tick(self) -> None:
        if self.tunnel is not None and self.tunnel.poll() is not None:
            say("туннель остановился — перезапуск")
            self.tunnel, self.url, self.hooked = None, "", False
            self.save_status("туннель остановился — переподключаюсь")
        if self.hooked or time.monotonic() < self.next_try:
            return
        self.next_try = time.monotonic() + self.RETRY_SECONDS
        settings = Settings.load()              # линию могли вписать в settings.yaml после запуска
        if not settings.connect.line_enabled:   # своей линии нет — вебхук и SetHook не нужны
            return
        self.line_id, self.kind = settings.connect.line_id, settings.connect.line_hook
        if self.mode == "url" and settings.connect.public_url:
            self.url = settings.connect.public_url
        elif self.mode == "tunnel" and self.tunnel is None:
            try:
                self.tunnel, self.url = start_tunnel()
            except (RuntimeError, OSError) as exc:
                say(f"линия: туннель не запущен ({exc}) — личные сообщения работают как раньше")
                self.save_status(f"туннель не запущен: {exc}")
                return
            say(f"туннель: {self.url}")
        if self.url:
            self.hooked = set_hook(settings, self.url)
            self.save_status("" if self.hooked else "1С-Коннект не принял подписку — повтор через 5 минут")

    def save_status(self, error: str = "") -> None:
        """Состояние линии для панели управления (logs/line.json)."""
        data = {"line_id": self.line_id, "kind": self.kind, "url": self.url,
                "hooked": self.hooked, "tunnel": self.tunnel is not None and self.tunnel.poll() is None,
                "error": error, "at": datetime.now().astimezone().isoformat(timespec="seconds")}
        try:
            LOG_DIR.mkdir(exist_ok=True)
            tmp = LOG_DIR / "line.json.tmp"
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, LOG_DIR / "line.json")
        except OSError:
            pass


def already_running() -> bool:
    try:
        return requests.get(f"http://127.0.0.1:{port()}/health", timeout=2).ok
    except requests.RequestException:
        return False


def main() -> int:
    if already_running():
        say(f"бот уже запущен — панель управления: http://127.0.0.1:{port()}/panel")
        return 0
    settings = Settings.load()
    colleague = settings.connect.channel == "colleague"
    # Токен нужен и в режиме личных сообщений: им закрыт HTTP-приёмник
    # /connect/colleague/message (проверка, будущий push от 1С-Коннект).
    ensure_webhook_token(settings.connect.webhook_token_env)
    settings = Settings.load()
    mode = settings.connect.webhook_mode

    bridge = start_bridge()
    if colleague:
        say("личные сообщения: опрос истории 1С-Коннекта (см. logs/bridge.log)")
    line = LineLink(mode)
    while True:
        line.tick()
        time.sleep(5)
        if bridge.poll() is not None:
            say("мост остановился — перезапуск")
            bridge = start_bridge()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as exc:  # noqa: BLE001
        say(f"ОШИБКА: {exc}")
        sys.exit(1)
