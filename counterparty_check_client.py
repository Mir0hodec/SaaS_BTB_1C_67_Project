"""Проверка контрагента через HTTP-сервис 1С (CounterpartyCheck).

    python counterparty_check_client.py 750710450345
    python counterparty_check_client.py 750710450345 --at 2026-09-28T00:00:00

Настройки — только из переменных окружения:
    COUNTERPARTY_CHECK_URL, COUNTERPARTY_CHECK_LOGIN, COUNTERPARTY_CHECK_PASSWORD,
    COUNTERPARTY_CHECK_TIMEOUT_SECONDS (по умолчанию 15).
Пароль и заголовок Authorization не попадают ни в лог, ни в результат.

Статус результата:
    ok            — долга нет и неподписанных ЭАВР нет: обычный сценарий;
    needs_manager — есть долг или неподписанные ЭАВР: передать клиент-менеджеру;
    error         — проверить не удалось (см. поле error). Это НЕ «чистый»
                    контрагент: вызывающий код решает сам, что делать.
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common.dotenv import load_dotenv as _load_dotenv  # noqa: E402

log = logging.getLogger("counterparty_check")

ENV_URL = "COUNTERPARTY_CHECK_URL"
ENV_LOGIN = "COUNTERPARTY_CHECK_LOGIN"
ENV_PASSWORD = "COUNTERPARTY_CHECK_PASSWORD"
ENV_TIMEOUT = "COUNTERPARTY_CHECK_TIMEOUT_SECONDS"
DEFAULT_TIMEOUT_SECONDS = 15.0

STATUS_OK = "ok"
STATUS_NEEDS_MANAGER = "needs_manager"
STATUS_ERROR = "error"

_FIELD_NAMES = {
    "counterparty_name": ("counterpartyName", "counterparty_name"),
    "bin": ("bin", "BIN"),
    "debt_amount": ("debtAmount", "debt_amount"),
    "unsigned_eavr_count": ("unsignedEavrCount", "unsigned_eavr_count"),
    "unsigned_eavr_amount": ("unsignedEavrAmount", "unsigned_eavr_amount"),
}
_HTTP_ERRORS = {400: "bad_request", 401: "unauthorized", 403: "forbidden"}
_BIN_RE = re.compile(r"^\d{12}$")


@dataclass
class CounterpartyCheckResult:
    counterparty_name: str | None
    bin: str
    debt_amount: float | None
    unsigned_eavr_count: int | None
    unsigned_eavr_amount: float | None
    raw_response: object
    status: str
    error: str | None

    @property
    def needs_manager(self) -> bool:
        """Долг > 0 или есть неподписанные ЭАВР — клиента ведёт клиент-менеджер."""
        return self.status == STATUS_NEEDS_MANAGER

    def to_dict(self) -> dict:
        return asdict(self)


def _error(bin_value: str, code: str, detail: str, raw: object = None) -> CounterpartyCheckResult:
    return CounterpartyCheckResult(None, bin_value, None, None, None, raw, STATUS_ERROR, f"{code}: {detail}")


def _redact(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


def _safe_url(url: str) -> str:
    """URL без логина/пароля, если их вписали прямо в адрес."""
    parts = urlsplit(url)
    if parts.username or parts.password:
        netloc = parts.hostname or ""
        if parts.port:
            netloc += f":{parts.port}"
        parts = parts._replace(netloc=netloc)
    return urlunsplit(parts)


def _timeout() -> float:
    raw = os.environ.get(ENV_TIMEOUT, "").strip()
    try:
        value = float(raw) if raw else DEFAULT_TIMEOUT_SECONDS
    except ValueError:
        log.warning("%s=%r — не число, беру %g с", ENV_TIMEOUT, raw, DEFAULT_TIMEOUT_SECONDS)
        return DEFAULT_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_TIMEOUT_SECONDS


def _to_float(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace(" ", "").replace(" ", "").replace(",", ".")
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _to_int(value) -> int | None:
    number = _to_float(value)
    return int(number) if number is not None and number == int(number) else None


def _pick(data: dict, field: str):
    for name in _FIELD_NAMES[field]:
        if name in data and data[name] is not None:
            return data[name]
    return None


def _payload_body(payload: object) -> dict | None:
    """Поля могут лежать на верхнем уровне или внутри result/data."""
    if isinstance(payload, list) and len(payload) == 1:
        payload = payload[0]
    if not isinstance(payload, dict):
        return None
    known = {n for names in _FIELD_NAMES.values() for n in names}
    for candidate in (payload, payload.get("result"), payload.get("data")):
        if isinstance(candidate, dict) and known & candidate.keys():
            return candidate
    return payload


def _normalize(bin_value: str, payload: object) -> CounterpartyCheckResult:
    body = _payload_body(payload)
    if body is None:
        return _error(bin_value, "invalid_response", "ответ сервиса — не JSON-объект", payload)

    name = _pick(body, "counterparty_name")
    debt = _to_float(_pick(body, "debt_amount"))
    count = _to_int(_pick(body, "unsigned_eavr_count"))
    amount = _to_float(_pick(body, "unsigned_eavr_amount"))
    result = CounterpartyCheckResult(
        counterparty_name=str(name) if name is not None else None,
        bin=str(_pick(body, "bin") or bin_value),
        debt_amount=debt, unsigned_eavr_count=count, unsigned_eavr_amount=amount,
        raw_response=payload, status=STATUS_OK, error=None,
    )
    if debt is None or count is None:
        # Без долга и числа ЭАВР решение принять нельзя — не считаем клиента «чистым».
        server_message = body.get("error") or body.get("message")
        result.status = STATUS_ERROR
        result.error = "invalid_response: в ответе нет debtAmount/unsignedEavrCount" + (
            f" ({server_message})" if server_message else "")
        return result
    if debt > 0 or count > 0:
        result.status = STATUS_NEEDS_MANAGER
    return result


def check_counterparty(bin_value: str, calculation_datetime: str | None = None) -> CounterpartyCheckResult:
    """Проверить контрагента по БИН/ИИН. Исключений не бросает — ошибки в result.error."""
    bin_clean = re.sub(r"\s+", "", str(bin_value or ""))
    if not _BIN_RE.match(bin_clean):
        return _error(bin_clean, "invalid_bin", "БИН/ИИН — 12 цифр")

    url = os.environ.get(ENV_URL, "").strip()
    login = os.environ.get(ENV_LOGIN, "")
    password = os.environ.get(ENV_PASSWORD, "")
    missing = [n for n, v in ((ENV_URL, url), (ENV_LOGIN, login), (ENV_PASSWORD, password)) if not v]
    if missing:
        return _error(bin_clean, "config_error", "не заданы переменные окружения: " + ", ".join(missing))

    token = base64.b64encode(f"{login}:{password}".encode("utf-8")).decode("ascii")
    secrets = [password, token]
    timeout = _timeout()
    body = {"bin": bin_clean}
    if calculation_datetime:
        body["calculationDateTime"] = calculation_datetime

    log.info("проверка контрагента bin=%s url=%s", bin_clean, _safe_url(url))
    started = time.monotonic()
    try:
        response = requests.post(url, json=body, auth=(login, password), timeout=timeout,
                                 headers={"Accept": "application/json"})
    except requests.Timeout:
        result = _error(bin_clean, "timeout", f"сервис не ответил за {timeout:g} с")
    except requests.RequestException as exc:
        result = _error(bin_clean, "network_error", _redact(f"{type(exc).__name__}: {exc}", secrets))
    else:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        log.info("ответ сервиса проверки: HTTP %s за %d мс", response.status_code, elapsed_ms)
        result = _handle_response(bin_clean, response, secrets)

    if result.status == STATUS_ERROR:
        log.warning("проверка контрагента bin=%s не выполнена: %s", bin_clean, result.error)
    else:
        log.info("проверка контрагента bin=%s: %s (долг %s, неподписанных ЭАВР %s)",
                 bin_clean, result.status, result.debt_amount, result.unsigned_eavr_count)
    return result


def _handle_response(bin_value: str, response: requests.Response, secrets: list[str]) -> CounterpartyCheckResult:
    code = response.status_code
    if not 200 <= code < 300:
        detail = _redact(f"HTTP {code}: {response.text[:300]}", secrets)
        kind = _HTTP_ERRORS.get(code, "server_error" if code >= 500 else "http_error")
        return _error(bin_value, kind, detail)
    try:
        payload = response.json()
    except ValueError:
        return _error(bin_value, "invalid_response", "ответ сервиса — не JSON",
                      _redact(response.text[:1000], secrets))
    return _normalize(bin_value, payload)


def summary(result: CounterpartyCheckResult) -> str:
    """Решение по бизнес-правилу одной строкой — для консоли и для бота."""
    if result.status == STATUS_OK:
        return "Долга и неподписанных ЭАВР нет — обычный сценарий."
    if result.status == STATUS_NEEDS_MANAGER:
        return "Есть долг или неподписанные ЭАВР — передать клиент-менеджеру."
    return f"Проверка не выполнена: {result.error}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверка контрагента через 1С")
    parser.add_argument("bin", help="БИН/ИИН, 12 цифр")
    parser.add_argument("--at", dest="calculation_datetime", help="дата расчёта, например 2026-09-28T00:00:00")
    parser.add_argument("-v", "--verbose", action="store_true", help="подробный лог в stderr")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    _load_dotenv(Path(__file__).with_name(".env"))

    result = check_counterparty(args.bin, args.calculation_datetime)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str))
    print(("Решение: " if result.status != STATUS_ERROR else "") + summary(result))
    return {STATUS_OK: 0, STATUS_NEEDS_MANAGER: 2}.get(result.status, 1)


if __name__ == "__main__":
    sys.exit(main())
