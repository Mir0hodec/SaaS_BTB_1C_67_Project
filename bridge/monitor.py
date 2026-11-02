"""Самопроверка бота раз в час — обычной программой, без запросов к Claude.

Проверки (как на вкладке «Проверки» панели, но без траты подписки):
  * Claude Code — CLI запускается (--version), вход в подписку есть, и последние ответы бота
    не все с ошибкой (так видно «доступ отключён», «лимит», сломанный вход);
  * API 1С-Коннекта — логин и пароль API принимаются;
  * линия «ИИ-помощник» — вебхук подключён (logs/line.json пишет start.py);
  * база знаний — индекс собран и в нём есть документы.
Если что-то не так — личное сообщение 1С-Коннекта тем, кто в monitor.notify (по умолчанию —
Данил Конжуровский): что сломалось и что зайти на сервер. Пока не починено — напоминание раз
в repeat_hours; когда всё снова в порядке — «бот снова работает».
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from bridge.config import REPO_ROOT, Settings
from bridge.store import Store
from common.doc_index import DocIndex, IndexNotBuiltError

log = logging.getLogger("bridge.monitor")
STATE_KEY = "monitor"


@dataclass
class Check:
    name: str
    ok: bool
    text: str


def check_claude(settings: Settings, store: Store) -> Check:
    from bridge.claude_runner import ClaudeRunner, _base_command
    try:
        ver = subprocess.run(_base_command(settings.claude.binary, ["--version"]), capture_output=True, text=True,
                             encoding="utf-8", errors="replace", env=ClaudeRunner(settings)._env(), timeout=60,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        return Check("Claude Code", False, f"CLI не запускается: {exc}")
    if ver.returncode != 0:
        return Check("Claude Code", False, f"CLI не запускается: {(ver.stderr or ver.stdout).strip()[:200]}")
    if settings.claude.config_dir:
        cred = Path(settings.claude.config_dir) / ".credentials.json"
        try:
            oauth = json.loads(cred.read_text(encoding="utf-8")).get("claudeAiOauth") or {}
        except (OSError, ValueError):
            oauth = {}
        if not (oauth.get("refreshToken") or oauth.get("accessToken")):
            return Check("Claude Code", False, "нет входа в аккаунт Claude — ярлык «Сменить аккаунт Claude», /login")
    limit = store.get_state("limit_until")
    if limit and datetime.fromisoformat(limit) > datetime.now():
        return Check("Claude Code", False, f"исчерпан лимит подписки, обновится в {datetime.fromisoformat(limit):%H:%M}")
    since = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    rows = store._exec("SELECT status, error FROM journal WHERE created_at >= ? AND status IN ('ok', 'error', 'limit') "
                       "ORDER BY id DESC LIMIT 3", (since,))
    if len(rows) == 3 and all(status != "ok" for status, _ in rows):
        return Check("Claude Code", False, f"три последних ответа — ошибка: {(rows[0][1] or '')[:300]}")
    return Check("Claude Code", True, (ver.stdout or "").strip()[:60])


def check_connect(settings: Settings) -> Check:
    cs = settings.connect
    if not (cs.login and cs.password):
        return Check("API 1С-Коннекта", False, "не заданы логин и пароль API (CONNECT_API_LOGIN/PASSWORD)")
    try:
        r = requests.get(f"{cs.api_base_url}/v1/line/specialists/", auth=(cs.login, cs.password),
                         headers={"Accept": "application/json"}, timeout=30)
    except requests.RequestException as exc:
        return Check("API 1С-Коннекта", False, f"нет связи: {type(exc).__name__}")
    if r.status_code != 200:
        return Check("API 1С-Коннекта", False, f"HTTP {r.status_code}: {r.text[:200]}")
    return Check("API 1С-Коннекта", True, "отвечает")


def check_line(settings: Settings, line_file: Path | None = None) -> Check:
    if not settings.connect.line_enabled:
        return Check("Линия «ИИ-помощник»", False, "линия не настроена (connect.line_id)")
    path = line_file or REPO_ROOT / "logs" / "line.json"
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Check("Линия «ИИ-помощник»", False, "нет сведений о вебхуке — запущен ли бот через start.py?")
    if not info.get("hooked"):
        return Check("Линия «ИИ-помощник»", False, f"вебхук не подключён: {info.get('error') or 'туннель не поднят'}")
    return Check("Линия «ИИ-помощник»", True, "подключена")


def check_kb(settings: Settings) -> Check:
    kb = settings.knowledge_base
    if not kb.root.is_dir():
        return Check("База знаний", False, f"нет папки {kb.root}")
    try:
        idx = DocIndex(kb.index_path, readonly=True)
    except IndexNotBuiltError:
        return Check("База знаний", False, "индекс не собран — «Проверки → База знаний → Переиндексировать»")
    try:
        count = len(idx.list_documents())
    except Exception as exc:  # noqa: BLE001
        return Check("База знаний", False, f"индекс не читается: {exc}")
    finally:
        idx.close()
    return Check("База знаний", count > 0, f"документов: {count}" if count else "индекс пустой")


def run_checks(settings: Settings, store: Store) -> list[Check]:
    checks = []
    for fn in (lambda: check_claude(settings, store), lambda: check_connect(settings),
               lambda: check_line(settings), lambda: check_kb(settings)):
        try:
            checks.append(fn())
        except Exception as exc:  # noqa: BLE001 — сама проверка упала: это тоже поломка
            log.exception("самопроверка")
            checks.append(Check("Самопроверка", False, f"{type(exc).__name__}: {exc}"))
    return checks


def alert_text(bad: list[Check]) -> str:
    lines = "\n".join(f"• {c.name}: {c.text}" for c in bad)
    return (f"ИИ-помощник: обнаружена проблема — бот может не отвечать сотрудникам.\n{lines}\n\n"
            "Нужно зайти на сервер и починить: панель управления http://127.0.0.1:8010/panel → «Проверки» и «Логи».")


class Monitor:
    def __init__(self, settings: Settings, store: Store, send, recipients):
        """send(user_id, text) — личное сообщение; recipients() — UUID получателей."""
        self.settings, self.store, self.send, self.recipients = settings, store, send, recipients
        self._stop = threading.Event()

    def state(self) -> dict:
        try:
            return json.loads(self.store.get_state(STATE_KEY) or "{}")
        except ValueError:
            return {}

    def tick(self) -> list[Check]:
        checks = run_checks(self.settings, self.store)
        bad = [c for c in checks if not c.ok]
        prev = self.state()
        now = time.time()
        repeat = self.settings.monitor.repeat_hours * 3600
        text = None
        if bad and (not prev.get("bad") or now - prev.get("alerted_at", 0) >= repeat
                    or prev.get("problems") != [c.name for c in bad]):
            text = alert_text(bad)
        elif not bad and prev.get("bad"):
            text = "ИИ-помощник снова работает: все проверки прошли."
        state = {"at": datetime.now(timezone.utc).isoformat(), "bad": bool(bad),
                 "problems": [c.name for c in bad], "alerted_at": prev.get("alerted_at", 0),
                 "checks": [c.__dict__ for c in checks]}
        if text:
            sent = 0
            for uid in self.recipients():
                try:
                    self.send(uid, text)
                    sent += 1
                except Exception:  # noqa: BLE001
                    log.exception("самопроверка: не удалось написать %s", uid)
            if sent:
                state["alerted_at"] = now
            log.warning("самопроверка: %s (сообщений отправлено: %d)", "; ".join(f"{c.name}: {c.text}" for c in bad)
                        or "всё снова в порядке", sent)
        else:
            log.info("самопроверка: %s", "всё в порядке" if not bad else "проблема прежняя, напоминание позже")
        self.store.set_state(STATE_KEY, json.dumps(state, ensure_ascii=False))
        return checks

    def loop(self) -> None:
        self._stop.wait(self.settings.monitor.first_delay_seconds)
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("самопроверка упала")
            self._stop.wait(self.settings.monitor.interval_minutes * 60)

    def start(self) -> None:
        if self.settings.monitor.interval_minutes > 0:
            threading.Thread(target=self.loop, name="monitor", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
