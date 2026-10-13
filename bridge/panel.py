"""Панель управления ИИ-помощником: http://127.0.0.1:<port>/panel

Только для самого сервера: мост слушает 127.0.0.1, а панель дополнительно
отказывает запросам с чужим Host (защита от DNS rebinding), через прокси или
туннель (заголовки X-Forwarded-For, CF-*) и не с локального адреса. Каждый
запрос к API панели несёт токен из страницы (X-Panel-Token) — чужая
страница в браузере сервера не сможет ничего нажать.

Изменения настроек пишутся в config/settings.yaml и config/bases.yaml одной
строкой (комментарии сохраняются) и применяются сразу, без перезапуска.
"""
from __future__ import annotations

import getpass
import hmac
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from bridge import docs, onec_extension, onec_toolkit, toolkit_secrets
from bridge.access import load_bases
from bridge.config import REPO_ROOT, Settings
from bridge.connect_directory import Directory, DirectoryError
from bridge.store import Store
from common.yaml_edit import YamlEditError, remove_section, set_value

log = logging.getLogger("bridge.panel")

UI_DIR = Path(__file__).resolve().parent / "panel_ui"
MODELS = {
    "claude-opus-5-5": "Opus 5.5",
    "claude-sonnet-5-5": "Sonnet 5.5",
    "claude-haiku-5-5": "Haiku 5.5",
}
EFFORTS = ["low", "medium", "high", "max"]
BASE_ID_RE = re.compile(r"^[a-z0-9_]{2,40}$")
TOOLKIT_URL_RE = re.compile(r"^https?://[A-Za-z0-9.\-]+(:\d{1,5})?/mcp/?$")
PUBLICATION_URL_RE = re.compile(r"^https?://[A-Za-z0-9.\-]+(:\d{1,5})?(/[A-Za-z0-9_.\-%]+)*/?$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-real-ip", "forwarded", "cf-connecting-ip", "cf-ray")
STATUS_GROUPS = {"ok": ["ok"], "error": ["error", "send_error"], "limit": ["limit"],
                 "other": ["denied", "unknown_user"]}
LOG_FILES = {"bridge": "bridge.log", "start": "start.log"}


def _git(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    # safe.directory: под системной задачей git иначе отказывается работать с чужой папкой.
    return subprocess.run(["git", "-c", "safe.directory=*", "-C", str(REPO_ROOT), *args],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _version() -> dict:
    try:
        out = _git("log", "-1", "--format=%h|%cI|%s", timeout=10).stdout.strip()
        commit, date, subject = out.split("|", 2)
        return {"commit": commit, "date": date, "subject": subject}
    except Exception:  # noqa: BLE001 — нет git или не репозиторий
        return {"commit": "", "date": "", "subject": ""}


def claude_account(settings: Settings) -> dict:
    """Под каким аккаунтом Claude работает бот — без токенов, только почта, имя и тариф."""
    if settings.claude.config_dir:
        cfg = Path(settings.claude.config_dir)
        state_file = cfg / ".claude.json"
    else:
        cfg = Path.home() / ".claude"
        state_file = Path.home() / ".claude.json"
    info = {"config_dir": str(cfg), "logged_in": False, "email": None, "name": None, "org": None, "plan": None}
    try:
        acc = json.loads(state_file.read_text(encoding="utf-8")).get("oauthAccount") or {}
        info.update(email=acc.get("emailAddress"), name=acc.get("displayName"), org=acc.get("organizationName"))
    except (OSError, ValueError, AttributeError):
        pass
    try:
        cred = json.loads((cfg / ".credentials.json").read_text(encoding="utf-8")).get("claudeAiOauth") or {}
        info["logged_in"] = bool(cred.get("refreshToken") or cred.get("accessToken"))
        info["plan"] = cred.get("subscriptionType")
    except (OSError, ValueError, AttributeError):
        pass
    return info


def windows_user() -> dict:
    name = getpass.getuser()
    system = name.upper() in ("SYSTEM", "СИСТЕМА") or name.endswith("$")
    return {"name": name, "system": system}


def _tail(path: Path, lines: int) -> list[str]:
    if not path.is_file():
        return []
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - 400_000))
        data = f.read().decode("utf-8", errors="replace")
    return data.splitlines()[-lines:]


def panel_router(*, settings: Settings, store: Store, assistant, worker, directory: Directory, started_at: float,
                 local_clients: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})) -> APIRouter:
    r = APIRouter()
    token = secrets.token_urlsafe(24)
    hosts = {f"127.0.0.1:{settings.port}", f"localhost:{settings.port}"}
    origins = {f"http://{h}" for h in hosts}
    checks: dict[str, dict] = {}
    jobs: dict[str, dict] = {}
    version = _version()

    def guard(request: Request, *, api: bool) -> None:
        client = request.client.host if request.client else ""
        if client not in local_clients or request.headers.get("host", "") not in hosts \
                or any(h in request.headers for h in PROXY_HEADERS):
            raise HTTPException(403, "панель доступна только на самом сервере")
        origin = request.headers.get("origin")
        if origin and origin not in origins:
            raise HTTPException(403, "чужой источник запроса")
        if api and not hmac.compare_digest(request.headers.get("x-panel-token", ""), token):
            raise HTTPException(403, "устаревшая страница — обновите её (F5)")

    def name_of(user_key: str, fallback: str | None = None) -> str:
        return directory.name(user_key) or (fallback if fallback and fallback != "Сотрудник" else None) \
            or (fallback or user_key)

    def run_job(kind: str, fn) -> dict:
        """Долгое действие в фоне; страница опрашивает /jobs/<id>."""
        job_id = uuid.uuid4().hex[:10]
        jobs[job_id] = job = {"id": job_id, "kind": kind, "state": "running", "started": time.time(),
                              "result": None, "error": None}

        def target() -> None:
            try:
                job["result"] = fn()
                job["state"] = "done"
            except Exception as exc:  # noqa: BLE001 — показать ошибку в панели
                log.exception("панель: действие %s не выполнено", kind)
                job["error"], job["state"] = str(exc) or type(exc).__name__, "error"
            job["finished"] = time.time()
        threading.Thread(target=target, daemon=True, name=f"panel-{kind}").start()
        return job

    # --- страница ---

    @r.get("/panel", response_class=HTMLResponse)
    def page(request: Request) -> HTMLResponse:
        guard(request, api=False)
        html = (UI_DIR / "index.html").read_text(encoding="utf-8").replace("{{TOKEN}}", token)
        return HTMLResponse(html, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                           "Content-Security-Policy": "default-src 'self'; style-src 'self' "
                                           "'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'"})

    @r.get("/panel/static/{name}")
    def static(name: str, request: Request) -> FileResponse:
        guard(request, api=False)
        if name not in ("panel.css", "panel.js", "favicon.svg"):
            raise HTTPException(404)
        return FileResponse(UI_DIR / name, headers={"Cache-Control": "no-cache"})

    # --- обзор ---

    def stats(days: int = 14) -> dict:
        tz = datetime.now().astimezone().tzinfo
        today = datetime.now(tz).date()
        since = datetime.combine(today - timedelta(days=days - 1), datetime.min.time(), tz)
        rows = store.journal_since(since)
        series = {today - timedelta(days=i): {"ok": 0, "error": 0, "limit": 0, "cost": 0.0}
                  for i in range(days)}
        for row in rows:
            day = datetime.fromisoformat(row["created_at"]).astimezone(tz).date()
            cell = series.get(day)
            if cell is None:
                continue
            group = "ok" if row["status"] == "ok" else "limit" if row["status"] == "limit" \
                else "error" if row["status"] in ("error", "send_error") else None
            if group:
                cell[group] += 1
            cell["cost"] += row["cost_usd"] or 0.0

        def summary(rows_: list[dict]) -> dict:
            answered = [x for x in rows_ if x["status"] in ("ok", "error", "send_error")]
            ok = [x for x in answered if x["status"] == "ok"]
            durations = sorted(x["duration_ms"] / 1000 for x in ok if x["duration_ms"])
            return {"questions": len(answered), "ok": len(ok),
                    "ok_rate": round(100 * len(ok) / len(answered)) if answered else None,
                    "median_seconds": durations[len(durations) // 2] if durations else None,
                    "cost": round(sum(x["cost_usd"] or 0 for x in rows_), 4),
                    "users": len({x["user_key"] for x in answered}),
                    "limits": sum(1 for x in rows_ if x["status"] == "limit")}

        today_start = datetime.combine(today, datetime.min.time(), tz)
        week_start = today_start - timedelta(days=6)
        return {
            "today": summary([x for x in rows if datetime.fromisoformat(x["created_at"]) >= today_start]),
            "week": summary([x for x in rows if datetime.fromisoformat(x["created_at"]) >= week_start]),
            "series": [{"date": d.isoformat(), **v, "cost": round(v["cost"], 4)} for d, v in sorted(series.items())],
        }

    def line_view() -> dict:
        """Своя линия «ИИ-помощник»: состояние туннеля и вебхука пишет супервизор (logs/line.json)."""
        cs = settings.connect
        info: dict = {"line_id": cs.line_id, "kind": cs.line_hook, "hooked": False, "tunnel": False}
        try:
            info.update(json.loads((REPO_ROOT / "logs" / "line.json").read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        info["enabled"] = cs.line_enabled
        return info

    def channels_today() -> dict:
        tz = datetime.now().astimezone().tzinfo
        start = datetime.combine(datetime.now(tz).date(), datetime.min.time(), tz)
        counts: dict[str, int] = {}
        for row in store.journal_since(start):
            counts[row["channel"]] = counts.get(row["channel"], 0) + 1
        return counts

    def bases_summary() -> dict:
        try:
            bases = list(load_bases(settings.bases_path).values())
        except (FileNotFoundError, ValueError):
            bases = []
        return {"total": len(bases), "toolkit": sum(b.via_toolkit for b in bases),
                "extension": sum(b.via_extension for b in bases)}

    @r.get("/panel/api/overview")
    def overview(request: Request) -> dict:
        guard(request, api=True)
        limit_until = store.get_state("limit_until")
        if limit_until and datetime.fromisoformat(limit_until) <= datetime.now():
            limit_until = None
        current = dict(worker.current) if worker.current else None
        if current:
            current["user_name"] = name_of(current["user_key"], current.get("user_name"))
            current["seconds"] = round(time.time() - current["started"])
        recent = store.journal_page(limit=6)
        for row in recent:
            row["user_name"] = name_of(row["user_key"], row["user_name"])
        return {
            "now": datetime.now(timezone.utc).isoformat(),
            "uptime": round(time.time() - started_at),
            "paused": store.get_state("paused"),
            "limit_until": limit_until,
            "queue": worker.queue.qsize(),
            "current": current,
            "stats": stats(),
            "recent": recent,
            "model": settings.claude.model, "model_name": MODELS.get(settings.claude.model, settings.claude.model),
            "effort": settings.claude.effort,
            "counterparty": settings.counterparty_check_enabled,
            "onec_code": settings.onec_code.enabled,
            "kb": assistant.knowledge_base_info(),
            "version": version,
            "supervised": os.environ.get("BRIDGE_SUPERVISED") == "1",
            "checks": checks,
            "account": claude_account(settings),
            "windows_user": windows_user(),
            "line": line_view(),
            "channels_today": channels_today(),
            "bases": bases_summary(),
        }

    @r.post("/panel/api/pause")
    async def pause(request: Request) -> dict:
        guard(request, api=True)
        body = await request.json()
        if body.get("paused"):
            store.set_state("paused", datetime.now(timezone.utc).isoformat())
        else:
            store.set_state("paused", None)
            store.set_state("limit_until", None)
        log.info("панель: бот %s", "на паузе" if body.get("paused") else "включён")
        return {"paused": store.get_state("paused")}

    # --- сотрудники ---

    @r.get("/panel/api/directory")
    def directory_search(request: Request, q: str = "", refresh: int = 0) -> dict:
        guard(request, api=True)
        if refresh or not directory.loaded_at:
            try:
                directory.refresh()
            except (DirectoryError, requests.RequestException) as exc:
                if refresh:
                    raise HTTPException(502, f"справочник 1С-Коннект не загрузился: {exc}") from None
        words = [w for w in q.lower().replace("ё", "е").split() if w]
        found = []
        for p in directory.all():
            hay = f"{p.full_name} {p.post} {p.email}".lower().replace("ё", "е")
            if p.deleted or (words and not all(w in hay for w in words)) or p.id == settings.connect.bot_specialist_id:
                continue
            found.append({"id": p.id, "name": p.full_name, "post": p.post, "email": p.email})
        return {"people": found[:40], "total": len(found), "loaded_at": directory.loaded_at or None}

    # --- журнал ---

    @r.get("/panel/api/journal")
    def journal(request: Request, status: str = "", user: str = "", q: str = "", before: int = 0) -> dict:
        guard(request, api=True)
        rows = store.journal_page(limit=50, before_id=before or None, statuses=STATUS_GROUPS.get(status),
                                  user_key=user or None, query=q.strip() or None)
        for row in rows:
            row["user_name"] = name_of(row["user_key"], row["user_name"])
        users = {}
        for row in store.journal_page(limit=500):
            users.setdefault(row["user_key"], name_of(row["user_key"], row["user_name"]))
        return {"rows": rows, "more": len(rows) == 50,
                "users": [{"id": k, "name": v} for k, v in sorted(users.items(), key=lambda x: x[1].lower())]}

    @r.get("/panel/api/journal/{entry_id}")
    def journal_entry(entry_id: int, request: Request) -> dict:
        guard(request, api=True)
        row = store.journal_entry(entry_id)
        if not row:
            raise HTTPException(404, "запись не найдена")
        row["user_name"] = name_of(row["user_key"], row["user_name"])
        return row

    # --- настройки ---

    def settings_view() -> dict:
        c, cs = settings.claude, settings.connect
        return {"model": c.model, "effort": c.effort,
                "ack_text": cs.ack_text, "models": [{"id": k, "name": v} for k, v in MODELS.items()],
                "efforts": EFFORTS, "version": version,
                "supervised": os.environ.get("BRIDGE_SUPERVISED") == "1", "windows_user": windows_user()}

    @r.get("/panel/api/settings")
    def get_settings(request: Request) -> dict:
        guard(request, api=True)
        return settings_view()

    @r.post("/panel/api/settings")
    async def save_settings(request: Request) -> dict:
        guard(request, api=True)
        body = await request.json()
        path = settings.source
        changes: list[tuple[tuple[str, str], object, callable]] = []
        if "model" in body:
            if body["model"] not in MODELS and body["model"] != settings.claude.model:
                raise HTTPException(400, "неизвестная модель")
            changes.append((("claude", "model"), body["model"], lambda v: setattr(settings.claude, "model", v)))
        if "effort" in body:
            if body["effort"] not in EFFORTS:
                raise HTTPException(400, "неизвестная глубина рассуждений")
            changes.append((("claude", "effort"), body["effort"], lambda v: setattr(settings.claude, "effort", v)))
        if "ack_text" in body:
            text = str(body["ack_text"] or "").strip()
            if len(text) > 300 or "\n" in text:
                raise HTTPException(400, "сообщение-подтверждение — одна строка до 300 символов")
            changes.append((("connect", "ack_text"), text, lambda v: setattr(settings.connect, "ack_text", v)))
        try:
            for keys, value, apply in changes:
                set_value(path, keys, value)
                apply(value)
        except YamlEditError as exc:
            raise HTTPException(500, str(exc)) from None
        log.info("панель: настройки изменены: %s", ", ".join(".".join(k) for k, _, _ in changes))
        return settings_view()

    # --- проверки и обслуживание ---

    def check_claude() -> dict:
        from bridge.claude_runner import ClaudeRunner, _base_command
        from bridge.mcp_config_builder import build_mcp_config, session_tools
        runner = ClaudeRunner(settings)
        ver = subprocess.run(_base_command(settings.claude.binary, ["--version"]), capture_output=True, text=True,
                             encoding="utf-8", errors="replace", env=runner._env(), timeout=60,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        version_text = (ver.stdout or ver.stderr).strip()
        if ver.returncode != 0:
            return {"ok": False, "version": version_text, "error": "CLI не запускается"}
        work = Path(tempfile.mkdtemp(prefix="claude-check-"))
        config = build_mcp_config(settings, base_id=None, outbox=work / "out", attachments=work / "in",
                                  target=work / "mcp.json")
        result = runner.run(question="Ответь одним предложением: ты на связи?", session_id=str(uuid.uuid4()),
                            is_new=True, cwd=work, mcp_config=config,
                            allowed=session_tools(settings))
        return {"ok": not result.is_error, "version": version_text, "tools": result.tools,
                "answer": result.text[:500], "error": result.error, "limit": result.limit_hit,
                "account": claude_account(settings)}

    def check_connect() -> dict:
        cs = settings.connect
        if not (cs.login and cs.password):
            return {"ok": False, "error": "не заданы логин и пароль API (CONNECT_API_LOGIN/PASSWORD)"}
        resp = requests.get(f"{cs.api_base_url}/v1/line/specialists/", auth=(cs.login, cs.password),
                            headers={"Accept": "application/json"}, timeout=30)
        res = {"ok": resp.status_code == 200, "http": resp.status_code}
        if resp.ok:
            res["specialists"] = len(resp.json())
        else:
            res["error"] = resp.text[:300]
        try:
            res["directory"] = directory.refresh()
        except (DirectoryError, requests.RequestException) as exc:
            res["directory_error"] = str(exc)
        return res

    def check_counterparty(bin_value: str) -> dict:
        sys.path.insert(0, str(REPO_ROOT))
        import counterparty_check_client as cc
        result = cc.check_counterparty(bin_value)
        return {"ok": result.status != "error", "status": result.status, "summary": cc.summary(result),
                "name": result.counterparty_name, "debt": result.debt_amount,
                "unsigned_count": result.unsigned_eavr_count, "error": result.error}

    def check_dev() -> dict:
        """Пробная сборка пустой обработки: есть ли скиллы, платформа и лицензия 1С."""
        dev = settings.dev
        if not dev.enabled:
            return {"ok": False, "error": "скиллы 1С не установлены — scripts/install_1c_skills.ps1"}
        platform = dev.platform()
        if not platform:
            return {"ok": False, "error": "не найдена платформа 1С (C:/Program Files/1cv8/8.3.*/bin)"}

        def script(name: str) -> str:
            found = sorted(p for p in (dev.skills_dir / name / "scripts").glob("*.py") if not p.stem.startswith("stub"))
            return str(found[0])
        work = Path(tempfile.mkdtemp(prefix="epf-check-"))
        out = work / "ПроверкаСборки.epf"
        flags = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace", "cwd": work,
                 "env": {**os.environ, "PYTHONUTF8": "1"}, "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
        started = time.time()
        init = subprocess.run([settings.python, script("epf-init"), "-Name", "ПроверкаСборки"], timeout=120, **flags)
        if init.returncode != 0:
            return {"ok": False, "platform": platform, "error": (init.stdout + init.stderr)[-1500:]}
        build = subprocess.run([settings.python, script("epf-build"), "-SourceFile", str(work / "src" / "ПроверкаСборки.xml"),
                                "-OutputFile", str(out), "-V8Path", platform], timeout=900, **flags)
        log_text = (build.stdout + build.stderr).strip()
        ok = build.returncode == 0 and out.is_file()
        return {"ok": ok, "platform": platform, "seconds": round(time.time() - started),
                "size_kb": out.stat().st_size // 1024 if out.is_file() else 0, "error": "" if ok else log_text[-2000:]}

    @r.post("/panel/api/check/{kind}")
    async def check(kind: str, request: Request) -> dict:
        guard(request, api=True)
        body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
        if kind == "claude":
            fn = check_claude
        elif kind == "connect":
            fn = check_connect
        elif kind == "counterparty":
            bin_value = re.sub(r"\D", "", str(body.get("bin", "")))
            if len(bin_value) != 12:
                raise HTTPException(400, "БИН/ИИН — 12 цифр")
            fn = lambda: check_counterparty(bin_value)  # noqa: E731
        elif kind == "kb":
            fn = assistant.rebuild_knowledge_base
        elif kind == "dev":
            fn = check_dev
        else:
            raise HTTPException(404)

        def recorded():
            result = fn()
            checks[kind] = {"at": datetime.now(timezone.utc).isoformat(), "result": result}
            return result
        return run_job(kind, recorded)

    # --- базы 1С (MCP Toolkit на ПК сотрудников) ---

    def check_view(check: onec_toolkit.ProbeResult) -> dict:
        return {"ok": check.ok, "message": check.message, "user": check.user,
                "config": check.config_title or check.config, "version": check.version, "tools": len(check.tools),
                "detail": check.detail, "at": datetime.fromtimestamp(check.checked_at, timezone.utc).isoformat()}

    def bases_view() -> list[dict]:
        try:
            bases = load_bases(settings.bases_path)
        except FileNotFoundError:
            bases = {}
        tokens = toolkit_secrets.known()
        out = []
        for b in bases.values():
            check = assistant.toolkit_checks.get(b.id)
            kind = "toolkit" if b.via_toolkit else "extension" if b.via_extension else "demo"
            out.append({
                "id": b.id, "title": b.title, "toolkit_url": b.toolkit_url, "link": b.link,
                "kind": kind, "url": b.url if b.via_extension else "",
                "write": b.write, "token_set": b.id in tokens,
                "owners": [{"id": o, "name": name_of(o)} for o in b.owners],
                "check": None if check is None else check_view(check),
            })
        return out

    @r.get("/panel/api/bases")
    def bases(request: Request) -> dict:
        guard(request, api=True)
        files = lambda paths: [{"name": p.name, "size": p.stat().st_size} for p in paths]  # noqa: E731
        return {"bases": bases_view(), "admin_user": onec_toolkit.ADMIN_USER,
                "kit": files(docs.toolkit_files()), "extensions": files(docs.extension_files()),
                "extensions_dir": str(docs.EXTENSIONS_DIR)}

    @r.get("/panel/api/docs")
    def docs_list(request: Request) -> dict:
        guard(request, api=True)
        return {"docs": [{"id": k, "title": title} for k, (title, path) in docs.DOCS.items() if path.is_file()]}

    @r.get("/panel/api/docs/{doc_id}")
    def doc(doc_id: str, request: Request) -> dict:
        guard(request, api=True)
        if doc_id not in docs.DOCS or not docs.DOCS[doc_id][1].is_file():
            raise HTTPException(404, "нет такой инструкции")
        title, path = docs.DOCS[doc_id]
        file_name, standalone = docs.standalone_html(doc_id)
        return {"id": doc_id, "title": title, "html": docs.render_markdown(path.read_text(encoding="utf-8")),
                "file_name": file_name, "standalone": standalone}

    @r.post("/panel/api/bases")
    async def save_base(request: Request) -> dict:
        guard(request, api=True)
        body = await request.json()
        base_id = str(body.get("id") or "").strip().lower()
        if not BASE_ID_RE.match(base_id):
            raise HTTPException(400, "код базы — латиница, цифры и _, от 2 до 40 символов (например gke_ivan)")
        title = str(body.get("title") or "").strip()
        if not title:
            raise HTTPException(400, "укажите название базы")
        owners = [str(o).lower() for o in body.get("owners") or [] if UUID_RE.match(str(o).lower())]
        if body.get("kind") == "extension":
            # Опубликованная база с расширением MCP_HTTPService: адрес публикации, без токена, только чтение.
            url = re.sub(r"/hs/mcp-1c(/.*)?$", "", str(body.get("url") or "").strip()).rstrip("/")
            if not PUBLICATION_URL_RE.match(url):
                raise HTTPException(400, "адрес публикации базы вида http://сервер/ИмяБазы")
            fields = (("title", title), ("mode", "extension"), ("url", url), ("owners", owners))
        else:
            url = str(body.get("toolkit_url") or "").strip()
            if not TOOLKIT_URL_RE.match(url):
                raise HTTPException(400, "адрес MCP Toolkit вида http://192.168.0.51:6003/mcp")
            fields = (("title", title), ("toolkit_url", url), ("owners", owners),
                      ("write", bool(body.get("write"))))
        try:
            for key, value in fields:
                set_value(settings.bases_path, (base_id, key), value)
        except YamlEditError as exc:
            raise HTTPException(500, str(exc)) from None
        token = str(body.get("token") or "").strip()
        if token:
            toolkit_secrets.set_token(base_id, token)
        assistant.toolkit_checks.pop(base_id, None)
        log.info("панель: база %s сохранена (%s)", base_id, url)
        return {"bases": bases_view()}

    @r.delete("/panel/api/bases/{base_id}")
    def delete_base(base_id: str, request: Request) -> dict:
        guard(request, api=True)
        try:
            if not remove_section(settings.bases_path, base_id):
                raise HTTPException(404, "базы нет")
        except YamlEditError as exc:
            raise HTTPException(500, str(exc)) from None
        toolkit_secrets.set_token(base_id, None)
        assistant.toolkit_checks.pop(base_id, None)
        log.info("панель: база %s удалена", base_id)
        return {"bases": bases_view()}

    @r.post("/panel/api/bases/{base_id}/check")
    def check_base(base_id: str, request: Request) -> dict:
        guard(request, api=True)
        base = load_bases(settings.bases_path).get(base_id)
        if base is None or not (base.via_toolkit or base.via_extension):
            raise HTTPException(404, "нет такой базы с MCP Toolkit или расширением")
        token = toolkit_secrets.get_token(base_id) if base.via_toolkit else ""
        if base.via_toolkit and not token:
            raise HTTPException(400, "сначала задайте токен MCP Toolkit")

        def do() -> dict:
            res = onec_toolkit.probe(base.toolkit_url, token) if base.via_toolkit else onec_extension.probe(base.url)
            assistant.toolkit_checks[base_id] = res
            return check_view(res)
        return run_job("base", do)

    @r.get("/panel/api/jobs/{job_id}")
    def job(job_id: str, request: Request) -> dict:
        guard(request, api=True)
        if job_id not in jobs:
            raise HTTPException(404)
        return jobs[job_id]

    @r.get("/panel/api/logs")
    def logs(request: Request, file: str = "bridge", lines: int = 300, health: int = 0) -> dict:
        guard(request, api=True)
        if file not in LOG_FILES:
            raise HTTPException(404)
        out = _tail(REPO_ROOT / "logs" / LOG_FILES[file], max(20, min(lines, 3000)) * (1 if health else 3))
        if not health:
            out = [x for x in out if "GET /health" not in x and "/panel/api/" not in x]
        return {"lines": out[-max(20, min(lines, 3000)):]}

    def exit_soon() -> None:
        def bye():
            time.sleep(1.0)
            log.info("панель: перезапуск моста")
            os._exit(3)
        threading.Thread(target=bye, daemon=True).start()

    @r.post("/panel/api/restart")
    def restart(request: Request) -> dict:
        guard(request, api=True)
        if os.environ.get("BRIDGE_SUPERVISED") != "1":
            raise HTTPException(409, "бот запущен не через scripts/start.py — перезапустить его некому")
        exit_soon()
        return {"restarting": True}

    @r.post("/panel/api/update")
    def update(request: Request) -> dict:
        guard(request, api=True)

        def do_update() -> dict:
            before = _git("rev-parse", "HEAD").stdout.strip()
            req_before = (REPO_ROOT / "requirements.txt").read_bytes()
            pull = _git("pull", "--ff-only", timeout=180)
            out = (pull.stdout + pull.stderr).strip()
            if pull.returncode != 0:
                raise RuntimeError(out or "git pull не выполнен")
            after = _git("rev-parse", "HEAD").stdout.strip()
            changed = before != after
            if changed and (REPO_ROOT / "requirements.txt").read_bytes() != req_before:
                pip = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check",
                                      "-r", str(REPO_ROOT / "requirements.txt")], capture_output=True, text=True,
                                     encoding="utf-8", errors="replace", timeout=900,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                out += "\n" + (pip.stdout + pip.stderr).strip()
                if pip.returncode != 0:
                    raise RuntimeError(out)
            log_text = _git("log", "--format=%h %s", f"{before}..{after}").stdout.strip() if changed else ""
            restarting = changed and os.environ.get("BRIDGE_SUPERVISED") == "1"
            if restarting:
                exit_soon()
            return {"changed": changed, "output": out, "commits": log_text.splitlines(),
                    "restarting": restarting}
        return run_job("update", do_update)

    return r
