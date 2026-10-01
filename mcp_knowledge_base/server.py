"""MCP-сервер: поиск по базе знаний, инструкциям и FAQ компании. Только чтение.

Индекс строится отдельно: python scripts/build_kb_index.py
Путь к индексу передаёт мост через KB_INDEX_PATH (см. bridge/mcp_config_builder.py).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.win_utf8 import ensure_utf8_stdio  # noqa: E402

ensure_utf8_stdio()

from common.doc_mcp import build_doc_server  # noqa: E402

mcp = build_doc_server(
    name="knowledge-base",
    noun="knowledge_base",
    index_env="KB_INDEX_PATH",
    default_index=".index/kb.sqlite3",
    instructions=(
        "База знаний ГК «Эксперт»: инструкции и ответы на частые вопросы по продуктам "
        "(1С:Бухгалтерия ОСИ/ЖКХ для Казахстана, конфигурация «Обменный пункт» и др.). "
        "Только чтение."
    ),
)

if __name__ == "__main__":
    mcp.run(transport="stdio")
