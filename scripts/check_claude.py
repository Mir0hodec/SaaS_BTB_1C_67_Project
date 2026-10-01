"""Проверка настоящего Claude Code CLI перед запуском бота.

    python scripts/check_claude.py

Запускает один вопрос с теми же флагами, что и бот, и показывает: какая
версия CLI, какая модель, какие инструменты реально доступны агенту
(должны быть только mcp__knowledge_base__* и mcp__files__*), подключились
ли MCP-серверы и дошёл ли ответ. Тратит один небольшой запрос подписки.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.win_utf8 import ensure_utf8_stdio  # noqa: E402

ensure_utf8_stdio()

from bridge.claude_runner import ClaudeRunner, _base_command  # noqa: E402
from bridge.config import Settings  # noqa: E402
from bridge.mcp_config_builder import allowed_tools, build_mcp_config  # noqa: E402


def main() -> int:
    settings = Settings.load()
    runner = ClaudeRunner(settings)
    version = subprocess.run(_base_command(settings.claude.binary, ["--version"]), capture_output=True,
                             text=True, encoding="utf-8", errors="replace", env=runner._env())
    print(f"CLI: {(version.stdout or version.stderr).strip() or 'не запускается'}")
    if version.returncode != 0:
        return 1

    work = Path(tempfile.mkdtemp(prefix="claude-check-"))
    config = build_mcp_config(settings, base_id=None, outbox=work / "out", attachments=work / "in",
                              target=work / "mcp.json")
    result = runner.run(question="Какие инструменты тебе доступны? Перечисли их имена одной строкой.",
                        session_id=str(uuid.uuid4()), is_new=True, cwd=work, mcp_config=config,
                        allowed=allowed_tools(None, settings.counterparty_check_enabled))
    print("Инструменты в сессии:", ", ".join(result.tools) or "—")
    if result.is_error:
        print("ОШИБКА:", result.error)
        return 1
    print("Ответ модели:", result.text[:500])
    print("Проверка пройдена: лишних инструментов нет, ответ получен.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
