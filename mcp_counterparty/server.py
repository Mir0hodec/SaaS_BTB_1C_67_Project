"""MCP-сервер: проверка контрагента через HTTP-сервис 1С CounterpartyCheck.

Логин и пароль сервиса берутся из окружения (процесс наследует его от
claude) или из .env в корне проекта — в MCP-конфиг на диске они не пишутся.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from common.win_utf8 import ensure_utf8_stdio  # noqa: E402

ensure_utf8_stdio()

from mcp.server.mcpserver import MCPServer  # noqa: E402

import counterparty_check_client as cc  # noqa: E402
from common.dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

mcp = MCPServer(name="counterparty", instructions=(
    "Проверка контрагента по БИН/ИИН: долг и неподписанные ЭАВР. Только чтение."
))


@mcp.tool()
def check_counterparty(bin: str, calculation_datetime: str | None = None) -> dict:
    """Проверить контрагента по БИН/ИИН (12 цифр): наименование, долг, число и
    сумма неподписанных ЭАВР. calculation_datetime — дата расчёта ISO, если нужна
    не на сегодня. Поле decision — решение по правилу компании: долг > 0 или есть
    неподписанные ЭАВР — клиента ведёт клиент-менеджер."""
    result = cc.check_counterparty(bin, calculation_datetime)
    data = result.to_dict()
    data.pop("raw_response", None)
    data["decision"] = cc.summary(result)
    return data


if __name__ == "__main__":
    mcp.run(transport="stdio")
