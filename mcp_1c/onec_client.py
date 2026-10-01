"""Доступ к базе 1С только на чтение.

Контракт read-only HTTP-сервиса, публикуемого в базе (см. README):

  GET  /hs/ai_readonly/metadata                 -> {"objects": {"Документы": [...], ...}}
  GET  /hs/ai_readonly/metadata?object=<Имя>    -> {"name": ..., "attributes": [...], "tabular_sections": [...]}
  POST /hs/ai_readonly/query
       {"query": "<текст>", "params": {"Имя": значение}, "validate_only": false}
       -> {"columns": [...], "rows": [[...]]}
       validate_only=true — только синтаксическая проверка без выполнения:
       -> {"ok": true} или {"ok": false, "error": "..."}
  Даты в params и результатах — ISO 8601 ("2026-09-01T00:00:00").
  Сервис сам обязан отклонять всё, кроме выборки, и работать под учёткой
  только на чтение: MCP-сервер не может проверить текст запроса на 100%.
"""
from __future__ import annotations

import json
import os
import random
import re
from datetime import datetime
from pathlib import Path
from typing import Protocol

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class OneCError(Exception):
    pass


class OneCClient(Protocol):
    def get_metadata(self, object_name: str | None = None) -> dict: ...
    def run_query(self, query_text: str, params: dict | None = None) -> dict: ...
    def validate_query(self, query_text: str) -> dict: ...


class OneCHttpClient:
    def __init__(self, base_url: str, auth: tuple[str, str] | None, timeout: float = 120):
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.timeout = timeout

    def _call(self, method: str, path: str, **kwargs) -> dict:
        r = requests.request(method, f"{self.base_url}/hs/ai_readonly{path}", auth=self.auth,
                             timeout=self.timeout, **kwargs)
        if r.status_code >= 400:
            raise OneCError(f"HTTP-сервис 1С ответил {r.status_code}: {r.text[:500]}")
        return r.json()

    def get_metadata(self, object_name: str | None = None) -> dict:
        return self._call("GET", "/metadata", params={"object": object_name} if object_name else None)

    def run_query(self, query_text: str, params: dict | None = None) -> dict:
        return self._call("POST", "/query", json={"query": query_text, "params": params or {}, "validate_only": False})

    def validate_query(self, query_text: str) -> dict:
        return self._call("POST", "/query", json={"query": query_text, "params": {}, "validate_only": True})


class MockOneCClient:
    """Демо-база из mcp_1c/fixtures — проверка всей цепочки без 1С."""

    def __init__(self, fixtures_dir: Path):
        self.fixtures_dir = fixtures_dir

    def _load(self, name: str) -> dict:
        return json.loads((self.fixtures_dir / name).read_text(encoding="utf-8"))

    def get_metadata(self, object_name: str | None = None) -> dict:
        meta = self._load("metadata.json")
        if not object_name:
            return {"objects": meta["objects"]}
        details = meta["details"].get(object_name)
        if details is None:
            raise OneCError(f"Объект «{object_name}» не найден в метаданных")
        return details

    def validate_query(self, query_text: str) -> dict:
        if "ВЫБРАТЬ" not in query_text.upper():
            return {"ok": False, "error": "Ожидается запрос на выборку (ВЫБРАТЬ …)"}
        return {"ok": True}

    def run_query(self, query_text: str, params: dict | None = None) -> dict:
        check = self.validate_query(query_text)
        if not check["ok"]:
            raise OneCError(check["error"])
        params = params or {}
        if "НачалоПериода" in params:
            # Реестр: детерминированные строки внутри месяца.
            start = datetime.fromisoformat(params["НачалоПериода"])
            rnd = random.Random(start.toordinal())
            rows = [[(start.replace(day=rnd.randint(1, 28), hour=rnd.randint(9, 18))).isoformat(),
                     f"РН-{start:%y%m}{n:04d}", rnd.choice(["ТОО Альфа", "ИП Бета", "ТОО Гамма"]),
                     round(rnd.uniform(1_000, 250_000), 2)] for n in range(1, 31)]
            return {"columns": ["Дата", "Номер", "Контрагент", "Сумма"], "rows": rows}
        data = self._load("sample_data.json")
        return {"columns": data["columns"], "rows": data["rows"]}


def _expand_env(value: str) -> str:
    def repl(m: re.Match) -> str:
        if m.group(1) not in os.environ:
            raise OneCError(f"Переменная окружения {m.group(1)} не задана для MCP-сервера 1С")
        return os.environ[m.group(1)]
    return _ENV_REF.sub(repl, value)


def _repo_path(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else REPO_ROOT / p


def build_client(base: dict) -> OneCClient:
    mode = base.get("mode", "mock")
    if mode == "http":
        auth = None
        if base.get("auth_user"):
            auth = (_expand_env(base["auth_user"]), _expand_env(base.get("auth_password", "")))
        return OneCHttpClient(_expand_env(base["url"]), auth=auth)
    if mode == "mock":
        return MockOneCClient(_repo_path(base["fixtures_dir"]))
    raise OneCError(f"Неизвестный режим базы: {mode}")


def code_dump_dir(base: dict) -> Path | None:
    return _repo_path(base["code_dump_dir"]) if base.get("code_dump_dir") else None
