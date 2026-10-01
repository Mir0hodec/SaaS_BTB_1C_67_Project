"""Заглушка Claude Code CLI для проверки моста без подписки. Не часть продукта.

Повторяет протокол `claude -p ... --output-format stream-json`: событие
system/init со списком инструментов, затем result. Вопрос читает из stdin.
Управляющие метки в вопросе (для тестов):
  [mock:file]       — положить файл в OUTBOX_DIR сервера files
  [mock:limit]      — ответить ошибкой лимита подписки
  [mock:extra-tool] — «подмешать» в сессию запрещённый инструмент Bash
  [mock:error]      — завершиться ошибкой
"""
import json
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.stdin.reconfigure(encoding="utf-8")
args = sys.argv[1:]


def arg(flag):
    return args[args.index(flag) + 1] if flag in args else None


question = sys.stdin.read()
session_id = arg("--session-id") or arg("--resume") or ""
mcp = json.load(open(arg("--mcp-config"), encoding="utf-8"))["mcpServers"] if arg("--mcp-config") else {}
tools = [t for t in (arg("--allowedTools") or "").split(",") if t]
if "[mock:extra-tool]" in question:
    tools.append("Bash")


def emit(event):
    print(json.dumps(event, ensure_ascii=False), flush=True)


emit({"type": "system", "subtype": "init", "session_id": session_id, "model": arg("--model"),
      "permissionMode": arg("--permission-mode"), "tools": tools,
      "mcp_servers": [{"name": n, "status": "connected"} for n in mcp]})

if "[mock:limit]" in question:
    emit({"type": "result", "subtype": "success", "is_error": True, "session_id": session_id,
          "result": "Claude usage limit reached. Your limit will reset at 6:20pm."})
    sys.exit(1)
if "[mock:error]" in question:
    emit({"type": "result", "subtype": "error_during_execution", "is_error": True,
          "session_id": session_id, "result": ""})
    sys.exit(1)

if "[mock:file]" in question and "files" in mcp:
    from pathlib import Path
    out = Path(mcp["files"]["env"]["OUTBOX_DIR"])
    out.mkdir(parents=True, exist_ok=True)
    (out / "отчет.txt").write_text("демо-файл", encoding="utf-8")

emit({"type": "result", "subtype": "success", "is_error": False, "session_id": session_id,
      "total_cost_usd": 0.05 if "--session-id" in args else 0.12,
      "result": (f"**[mock]** Получил:\n{question}\n\nСерверы: {sorted(mcp)}. "
                 f"Сессия: {session_id}. Путь C:\\secret\\file.txt")})
