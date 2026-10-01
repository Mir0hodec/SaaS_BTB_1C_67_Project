"""MCP-сервер поиска по индексу документов (база знаний, регламенты).

Индекс открывается только на чтение и лениво: сервер стартует даже без
построенного индекса, а инструменты в этом случае возвращают понятную
ошибку с командой сборки, а не роняют сессию Claude Code.
"""
from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from common.doc_index import MAX_READ_CHUNKS, DocIndex, IndexNotBuiltError

REPO_ROOT = Path(__file__).resolve().parent.parent


def build_doc_server(*, name: str, noun: str, index_env: str, default_index: str, instructions: str) -> MCPServer:
    raw_path = Path(os.environ.get(index_env, default_index))
    index_path = raw_path if raw_path.is_absolute() else REPO_ROOT / raw_path
    mcp = MCPServer(name=name, instructions=instructions)
    state: dict[str, DocIndex] = {}

    def index() -> DocIndex:
        if "idx" not in state:
            try:
                state["idx"] = DocIndex(index_path, readonly=True)
            except IndexNotBuiltError as exc:
                raise ToolError(str(exc)) from None
        return state["idx"]

    def search(query: str, product: str | None = None, limit: int = 8) -> dict:
        hits = index().search(query, limit=max(1, min(limit, 20)), product=product)
        return {
            "hits": [asdict(h) for h in hits],
            "hint": (
                "matched_terms/query_terms — сколько значимых слов запроса нашлось во фрагменте. "
                "Поиск по словам, не по смыслу: если совпадений мало, повтори запрос с синонимами "
                "(«завести» → «добавить», «обменник» → «обменный пункт»). "
                "Чтобы прочитать фрагмент целиком или соседние, вызови read-инструмент с path и chunk_no. "
                "В ответе сотруднику указывай title документа как источник."
            ),
        }

    def read(path: str, start_chunk: int = 0, max_chunks: int = 3) -> dict:
        try:
            return index().read_document(path, start_chunk=start_chunk, max_chunks=max_chunks)
        except KeyError:
            raise ToolError(f"Документ '{path}' не найден в индексе — возьми path из результатов поиска.") from None

    def list_docs(product: str | None = None) -> list[dict]:
        return index().list_documents(product=product)

    mcp.add_tool(
        search, name=f"search_{noun}",
        description=(
            "Полнотекстовый поиск по фрагментам документов (учитывает словоформы). "
            "product — необязательный фильтр: 'ОСИ/ЖКХ', 'Обменный пункт', 'Общее'."
        ),
    )
    mcp.add_tool(
        read, name=f"read_{noun}_document",
        description=f"Прочитать документ по path начиная с фрагмента start_chunk (не более {MAX_READ_CHUNKS} фрагментов за вызов).",
    )
    mcp.add_tool(
        list_docs, name=f"list_{noun}_documents",
        description="Список документов индекса (path, title, product, число фрагментов).",
    )
    return mcp
