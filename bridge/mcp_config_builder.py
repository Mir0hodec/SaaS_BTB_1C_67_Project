"""--mcp-config для одного вопроса: только нужные серверы, одна база 1С.

Процесс Claude и его MCP-серверы стартуют в пустой рабочей папке сессии,
поэтому все пути — абсолютные. Секретов в конфиге нет.
"""
from __future__ import annotations

import json
from pathlib import Path

from bridge.config import REPO_ROOT, Settings

ONEC_TOOLS = ["get_metadata", "run_query", "export_query_to_excel", "search_code"]
KB_TOOLS = ["search_knowledge_base", "read_knowledge_base_document", "list_knowledge_base_documents"]
FILES_TOOLS = ["create_excel", "create_word", "list_attachments", "view_attachment"]
COUNTERPARTY_TOOLS = ["check_counterparty"]

# Встроенные инструменты Claude Code, которые убираются из сессии целиком:
# терминал, файлы, сеть, субагенты. Проверяется ещё раз по событию init
# (bridge/claude_runner.py), на случай новых инструментов в новых версиях.
BUILTIN_TOOLS_TO_REMOVE = [
    "Bash", "PowerShell", "BashOutput", "KillShell", "Monitor",
    "Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep", "LS",
    "WebFetch", "WebSearch", "Agent", "Task", "Skill", "SlashCommand",
]


def _server(settings: Settings, script: str, env: dict[str, str]) -> dict:
    return {
        "command": settings.python,
        "args": [str(REPO_ROOT / script)],
        "env": {"PYTHONUTF8": "1", **env},
    }


def build_mcp_config(settings: Settings, *, base_id: str | None, outbox: Path,
                     attachments: Path, target: Path) -> Path:
    servers = {
        "knowledge_base": _server(settings, "mcp_knowledge_base/server.py",
                                  {"KB_INDEX_PATH": str(settings.knowledge_base.index_path)}),
        "files": _server(settings, "mcp_files/server.py",
                         {"OUTBOX_DIR": str(outbox), "ATTACHMENTS_DIR": str(attachments)}),
    }
    if base_id:
        servers["onec"] = _server(settings, "mcp_1c/server.py", {
            "ONEC_BASE_ID": base_id,
            "BASES_CONFIG_PATH": str(settings.bases_path),
            "OUTBOX_DIR": str(outbox),
        })
    if settings.counterparty_check_enabled:
        # Логин/пароль сервер получит из унаследованного окружения — не из файла.
        servers["counterparty"] = _server(settings, "mcp_counterparty/server.py", {})
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"mcpServers": servers}, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def allowed_tools(base_id: str | None, counterparty: bool = False) -> list[str]:
    tools = [f"mcp__knowledge_base__{t}" for t in KB_TOOLS] + [f"mcp__files__{t}" for t in FILES_TOOLS]
    if base_id:
        tools += [f"mcp__onec__{t}" for t in ONEC_TOOLS]
    if counterparty:
        tools += [f"mcp__counterparty__{t}" for t in COUNTERPARTY_TOOLS]
    return tools
