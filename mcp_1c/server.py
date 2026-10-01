"""MCP-сервер 1С: одна база на вопрос, только чтение.

База задаётся мостом через ONEC_BASE_ID — модель видит только её и не
путает цифры разных баз. Файлы выгрузок кладутся в OUTBOX_DIR, откуда мост
отправляет их сотруднику.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.win_utf8 import ensure_utf8_stdio  # noqa: E402

ensure_utf8_stdio()

import yaml  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

from common.xlsx import safe_file_name, write_workbook  # noqa: E402
from mcp_1c.onec_client import OneCError, build_client, code_dump_dir  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
MAX_QUERY_ROWS = 200
MAX_EXPORT_MONTHS = 60

BASE_ID = os.environ.get("ONEC_BASE_ID", "")
_bases = yaml.safe_load(Path(os.environ.get("BASES_CONFIG_PATH", REPO_ROOT / "config" / "bases.yaml"))
                        .read_text(encoding="utf-8")) or {}
BASE = _bases.get(BASE_ID)
OUTBOX = Path(os.environ["OUTBOX_DIR"]) if os.environ.get("OUTBOX_DIR") else None

mcp = MCPServer(name="onec", instructions=(
    f"База 1С «{(BASE or {}).get('title', BASE_ID)}». Только чтение: метаданные, запросы, "
    "выгрузка в Excel, поиск по коду конфигурации."
))


def _client():
    if BASE is None:
        raise ToolError(f"База «{BASE_ID}» не настроена в bases.yaml")
    return build_client(BASE)


def _guard(fn, *args):
    try:
        return fn(*args)
    except OneCError as exc:
        raise ToolError(str(exc)) from None


@mcp.tool()
def get_metadata(object_name: str | None = None) -> dict:
    """Состав базы. Без аргумента — списки объектов по видам. С object_name
    (например «Документ.РасходнаяНакладная») — реквизиты и табличные части."""
    return _guard(_client().get_metadata, object_name)


@mcp.tool()
def run_query(query_text: str, params: dict | None = None) -> dict:
    """Выполнить запрос на языке запросов 1С (только ВЫБРАТЬ). params — значения
    параметров &Имя, даты строкой ISO. Возвращает не больше 200 строк; для
    больших выгрузок — export_query_to_excel."""
    result = _guard(_client().run_query, query_text, params)
    rows = result.get("rows", [])
    if len(rows) > MAX_QUERY_ROWS:
        return {"columns": result.get("columns", []), "rows": rows[:MAX_QUERY_ROWS],
                "total_rows": len(rows), "truncated": True,
                "note": "Показаны первые 200 строк. Полный результат — через export_query_to_excel."}
    return {**result, "total_rows": len(rows), "truncated": False}


def _months(start: date, end: date) -> list[tuple[datetime, datetime]]:
    periods, cur = [], date(start.year, start.month, 1)
    while cur <= end:
        nxt = date(cur.year + cur.month // 12, cur.month % 12 + 1, 1)
        a = datetime.combine(max(cur, start), datetime.min.time())
        b = datetime.combine(min(nxt - timedelta(days=1), end), datetime.max.time()).replace(microsecond=0)
        periods.append((a, b))
        cur = nxt
    return periods


@mcp.tool()
def export_query_to_excel(query_text: str, date_from: str, date_to: str, file_name: str,
                          sheet_name: str = "Выгрузка") -> dict:
    """Большая выгрузка в Excel. Запрос обязан использовать параметры
    &НачалоПериода и &КонецПериода. Мост проверяет запрос без выполнения,
    выполняет его помесячно за период [date_from, date_to] (ISO-даты) и
    собирает один xlsx с фильтрами и закреплённой шапкой. Файл уйдёт
    сотруднику вместе с ответом; строки в ответ модели не возвращаются."""
    if OUTBOX is None:
        raise ToolError("Каталог для файлов не задан (OUTBOX_DIR)")
    if "&НачалоПериода" not in query_text or "&КонецПериода" not in query_text:
        raise ToolError("В запросе нужны параметры &НачалоПериода и &КонецПериода — по ним мост режет период")
    try:
        start, end = date.fromisoformat(date_from[:10]), date.fromisoformat(date_to[:10])
    except ValueError:
        raise ToolError("date_from/date_to — даты в формате ГГГГ-ММ-ДД") from None
    if end < start:
        raise ToolError("date_to раньше date_from")
    periods = _months(start, end)
    if len(periods) > MAX_EXPORT_MONTHS:
        raise ToolError(f"Слишком длинный период: больше {MAX_EXPORT_MONTHS} месяцев")

    client = _client()
    check = _guard(client.validate_query, query_text)
    if not check.get("ok", False):
        raise ToolError(f"Запрос не прошёл проверку в 1С: {check.get('error', 'ошибка')}")

    columns: list = []
    rows: list = []
    for a, b in periods:
        part = _guard(client.run_query, query_text,
                      {"НачалоПериода": a.isoformat(), "КонецПериода": b.isoformat()})
        columns = columns or part.get("columns", [])
        rows.extend(part.get("rows", []))

    name = safe_file_name(file_name, ".xlsx")
    written = write_workbook(OUTBOX / name, [{"name": sheet_name, "columns": columns, "rows": rows}])
    return {"file": name, "rows": written, "months": len(periods), "columns": columns,
            "preview": rows[:5]}


@mcp.tool()
def search_code(text: str, limit: int = 50) -> dict:
    """Поиск подстроки по выгрузке конфигурации в файлы (модули .bsl):
    «где это считается и почему так». Регистр не учитывается."""
    root = code_dump_dir(BASE or {})
    if root is None or not root.is_dir():
        raise ToolError("Для этой базы не указана выгрузка конфигурации (code_dump_dir в bases.yaml)")
    needle = text.lower().strip()
    if len(needle) < 3:
        raise ToolError("Строка поиска — от 3 символов")
    matches, total = [], 0
    for path in sorted(root.rglob("*.bsl")):
        for no, line in enumerate(path.read_text(encoding="utf-8-sig", errors="replace").splitlines(), 1):
            if needle in line.lower():
                total += 1
                if len(matches) < max(1, min(limit, 200)):
                    matches.append({"module": path.relative_to(root).as_posix(), "line": no,
                                    "text": line.strip()[:300]})
    return {"matches": matches, "total": total}


if __name__ == "__main__":
    mcp.run(transport="stdio")
