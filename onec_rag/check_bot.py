"""Проверка «бот находит код через MCP»: один настоящий вопрос тем же запуском
Claude Code CLI, что у бота (те же флаги, инструменты и системный промпт).

    python onec_rag/check_bot.py "где считается пеня?"

Тратит один запрос подписки. Нужен выполненный вход в Claude Code (claude → /login).
"""
from __future__ import annotations

import sys
import tempfile
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from common.win_utf8 import ensure_utf8_stdio  # noqa: E402

ensure_utf8_stdio()

from bridge.answer import clean_answer  # noqa: E402
from bridge.claude_runner import ClaudeRunner  # noqa: E402
from bridge.config import Settings  # noqa: E402
from bridge.mcp_config_builder import allowed_tools, build_mcp_config  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    own = REPO_ROOT / "config" / "settings.yaml"
    settings = Settings.load() if own.exists() else Settings.load(REPO_ROOT / "config" / "settings.example.yaml")
    if not settings.onec_code.enabled:
        print(f"Поиск по коду не настроен: нет файла {settings.onec_code.config_path}")
        return 1
    work = Path(tempfile.mkdtemp(prefix="onec-check-"))
    config = build_mcp_config(settings, base_id=None, outbox=work / "out", attachments=work / "in",
                              target=work / "mcp.json")
    question = ("Сотрудник: Проверка\nБаза 1С не выбрана (только база знаний).\n\nВопрос:\n"
                + " ".join(sys.argv[1:]))
    result = ClaudeRunner(settings).run(
        question=question, session_id=str(uuid.uuid4()), is_new=True, cwd=work, mcp_config=config,
        allowed=allowed_tools(None, settings.counterparty_check_enabled, True))
    code_tools = [t for t in result.tools if t.startswith("mcp__onec_code__")]
    print("Инструменты поиска по коду в сессии:", ", ".join(code_tools) or "НЕТ")
    if result.is_error:
        print("ОШИБКА:", result.error)
        return 1
    print("Ответ бота:\n" + clean_answer(result.text))
    return 0 if code_tools else 1


if __name__ == "__main__":
    sys.exit(main())
