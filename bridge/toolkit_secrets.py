"""Токены MCP Toolkit баз 1С — не в YAML, а в отдельном файле .secrets/toolkit.json.

Папку .secrets закрывает scripts/install_service.ps1 (доступ — система и
администраторы). В процесс Claude токен попадает только переменной окружения
ONEC_TOOLKIT_TOKEN_<ID>; в MCP-конфиге на диске — ссылка ${…}, не значение.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from bridge.config import REPO_ROOT

DEFAULT_PATH = REPO_ROOT / ".secrets" / "toolkit.json"
_lock = threading.Lock()


def env_name(base_id: str) -> str:
    return "ONEC_TOOLKIT_TOKEN_" + base_id.upper()


def _read(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def get_token(base_id: str, path: Path | None = None) -> str:
    with _lock:
        return _read(path or DEFAULT_PATH).get(base_id, "")


def set_token(base_id: str, token: str | None, path: Path | None = None) -> None:
    path = path or DEFAULT_PATH
    with _lock:
        data = _read(path)
        if token:
            data[base_id] = token
        else:
            data.pop(base_id, None)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)


def known(path: Path | None = None) -> set[str]:
    with _lock:
        return set(_read(path or DEFAULT_PATH))
