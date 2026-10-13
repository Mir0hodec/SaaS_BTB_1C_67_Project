"""Опубликованные базы с расширением MCP_HTTPService (HTTP-сервис /hs/mcp-1c).

База опубликована на веб-сервере, в ней установлено расширение — помощник читает
метаданные, данные запросами, журнал регистрации и формы. Только чтение: записи в
расширении нет. Вход — всегда «Администратор» / «1» (common/onec_login.py).
Проверка до запуска Claude и по кнопке «Проверить» в панели — здесь.
"""
from __future__ import annotations

from bridge.onec_toolkit import ProbeResult
from common.onec_login import ADMIN_PASSWORD, ADMIN_USER
from mcp_1c.onec_client import OneCError, OneCExtensionClient


def probe(url: str, timeout: float = 30.0) -> ProbeResult:
    client = OneCExtensionClient(url, auth=(ADMIN_USER, ADMIN_PASSWORD), timeout=timeout)
    try:
        version = client.version()
        cfg = client.configuration()
    except OneCError as exc:
        return ProbeResult(ok=False, message=str(exc))
    name = str(cfg.get("name") or "")
    detail = f"расширение {version}, платформа {cfg.get('platform_version') or '?'}"
    return ProbeResult(ok=True, user=ADMIN_USER, password_ok=True, config=name, config_title=name,
                       version=str(cfg.get("version") or ""), detail=detail)
