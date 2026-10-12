"""--mcp-config для одного вопроса: только нужные серверы, одна база 1С.

Процесс Claude и его MCP-серверы стартуют в пустой рабочей папке сессии,
поэтому все пути — абсолютные. Секретов в конфиге нет.
"""
from __future__ import annotations

import json
from pathlib import Path

from bridge.access import Base
from bridge.config import REPO_ROOT, Settings
from bridge.onec_toolkit import READ_TOOLS as TOOLKIT_READ_TOOLS, WRITE_TOOLS as TOOLKIT_WRITE_TOOLS
from bridge.toolkit_secrets import env_name

ONEC_TOOLS = ["get_metadata", "run_query", "export_query_to_excel", "search_code"]
KB_TOOLS = ["search_knowledge_base", "read_knowledge_base_document", "list_knowledge_base_documents"]
FILES_TOOLS = ["create_excel", "create_word", "list_attachments", "view_attachment"]
COUNTERPARTY_TOOLS = ["check_counterparty"]
ONEC_CODE_TOOLS = ["search_1c", "get_module", "find_object"]
DEV_TOOLS = ["list_guides", "read_guide", "run_skill", "write_file", "read_file", "list_files", "build"]

# Встроенные инструменты Claude Code, которые убираются из сессии целиком:
# терминал, файлы, сеть, субагенты. Проверяется ещё раз по событию init
# (bridge/claude_runner.py), на случай новых инструментов в новых версиях.
BUILTIN_TOOLS_TO_REMOVE = [
    "Bash", "PowerShell", "BashOutput", "KillShell", "Monitor",
    "Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep", "LS",
    "WebFetch", "WebSearch", "Agent", "Task", "Skill", "SlashCommand",
    # агенты, расписания, удалённые запуски, уведомления, рабочие копии — боту не нужны
    "SendMessage", "ListAgents", "RemoteTrigger", "PushNotification", "ScheduleWakeup",
    "CronCreate", "CronDelete", "CronList", "EnterWorktree", "ExitWorktree", "DesignSync",
    "ReportFindings", "ListMcpResourcesTool", "ReadMcpResourceTool", "ReadMcpResourceDirTool",
]


def _server(settings: Settings, script: str, env: dict[str, str]) -> dict:
    return {
        "command": settings.python,
        "args": [str(REPO_ROOT / script)],
        "env": {"PYTHONUTF8": "1", **env},
    }


def _onec_code_python(settings: Settings) -> str:
    """onec_rag/install.ps1 ставит свой venv (в нём только пакет mcp)."""
    if settings.onec_code.python:
        return settings.onec_code.python
    for venv_python in (REPO_ROOT / "onec_rag" / ".venv" / "Scripts" / "python.exe",
                        REPO_ROOT / "onec_rag" / ".venv" / "bin" / "python"):
        if venv_python.is_file():
            return str(venv_python)
    return settings.python


def toolkit_server(base: Base) -> dict:
    """База на ПК сотрудника через MCP Toolkit. Токен — ссылкой на переменную окружения:
    Claude Code подставляет её сам, в файле конфигурации значения нет."""
    return {"type": "http", "url": base.toolkit_url,
            "headers": {"Authorization": "Bearer ${" + env_name(base.id) + "}"}}


def build_mcp_config(settings: Settings, *, base_id: str | None, outbox: Path,
                     attachments: Path, target: Path, base: Base | None = None) -> Path:
    servers = {
        "knowledge_base": _server(settings, "mcp_knowledge_base/server.py",
                                  {"KB_INDEX_PATH": str(settings.knowledge_base.index_path)}),
        "files": _server(settings, "mcp_files/server.py",
                         {"OUTBOX_DIR": str(outbox), "ATTACHMENTS_DIR": str(attachments)}),
    }
    if base is not None and base.via_toolkit:
        servers["onec"] = toolkit_server(base)
    elif base_id:
        servers["onec"] = _server(settings, "mcp_1c/server.py", {
            "ONEC_BASE_ID": base_id,
            "BASES_CONFIG_PATH": str(settings.bases_path),
            "OUTBOX_DIR": str(outbox),
        })
    if settings.dev.enabled:
        # Рабочая папка разработки — своя у каждого сотрудника и живёт между вопросами.
        servers["dev"] = _server(settings, "mcp_dev/server.py", {
            "DEV_WORKSPACE": str(outbox.parent.parent / "dev"), "OUTBOX_DIR": str(outbox),
            "SKILLS_DIR": str(settings.dev.skills_dir), "EXTRA_GUIDES_DIR": str(settings.dev.guides_dir),
            "V8_PATH": settings.dev.platform(), "SKILLS_PYTHON": settings.python})
    if settings.counterparty_check_enabled:
        # Логин/пароль сервер получит из унаследованного окружения — не из файла.
        servers["counterparty"] = _server(settings, "mcp_counterparty/server.py", {})
    if settings.onec_code.enabled:
        # Код конфигурации не зависит от выбранной базы: индекс один, только чтение.
        servers["onec_code"] = {
            "command": _onec_code_python(settings),
            "args": [str(REPO_ROOT / "onec_rag" / "onec_mcp.py")],
            "env": {"PYTHONUTF8": "1", "ONEC_RAG_CONFIG": str(settings.onec_code.config_path)},
        }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"mcpServers": servers}, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def allowed_tools(base_id: str | None, counterparty: bool = False, onec_code: bool = False,
                  base: Base | None = None, dev: bool = False) -> list[str]:
    tools = [f"mcp__knowledge_base__{t}" for t in KB_TOOLS] + [f"mcp__files__{t}" for t in FILES_TOOLS]
    if base is not None and base.via_toolkit:
        names = TOOLKIT_READ_TOOLS + (TOOLKIT_WRITE_TOOLS if base.write else [])
        tools += [f"mcp__onec__{t}" for t in names]
    elif base_id:
        tools += [f"mcp__onec__{t}" for t in ONEC_TOOLS]
    if counterparty:
        tools += [f"mcp__counterparty__{t}" for t in COUNTERPARTY_TOOLS]
    if dev:
        tools += [f"mcp__dev__{t}" for t in DEV_TOOLS]
    if onec_code:
        tools += [f"mcp__onec_code__{t}" for t in ONEC_CODE_TOOLS]
    return tools
