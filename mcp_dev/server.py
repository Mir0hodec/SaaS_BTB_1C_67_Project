"""MCP-сервер разработки: внешние обработки и отчёты 1С на сервере бота.

Модель пишет решение, этот сервер собирает из него .epf/.erf скиллами
cc-1c-skills (github.com/Nikolay-Shirokov/cc-1c-skills) и кладёт файл в
OUTBOX_DIR — мост отправит его сотруднику в чат 1С-Коннекта.

Терминала у модели нет. Вместо него — узкие инструменты:
  * list_guides / read_guide — инструкции скиллов (форматы XML, команды);
  * run_skill — python-скрипт скилла из белого списка, аргументы проверяются:
    все пути — внутри рабочей папки, параметры баз и платформы запрещены;
  * write_file / read_file / list_files — только рабочая папка сотрудника;
  * build — сборка платформой сервера во временной базе (своей базы не нужно).
Скиллы, которые меняют базы и публикации (db-*, cf-*, cfe-*, web-*), сюда не входят.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.win_utf8 import ensure_utf8_stdio  # noqa: E402

ensure_utf8_stdio()

from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

WORKSPACE = Path(os.environ.get("DEV_WORKSPACE", "")).resolve() if os.environ.get("DEV_WORKSPACE") else None
OUTBOX = Path(os.environ.get("OUTBOX_DIR", "")) if os.environ.get("OUTBOX_DIR") else None
SKILLS_DIR = Path(os.environ.get("SKILLS_DIR", ""))
EXTRA_GUIDES_DIR = Path(os.environ.get("EXTRA_GUIDES_DIR", ""))
V8_PATH = os.environ.get("V8_PATH", "")
PYTHON = os.environ.get("SKILLS_PYTHON", sys.executable)

ALLOWED_SKILLS = [
    "epf-init", "epf-validate", "epf-bsp-init", "epf-bsp-add-command",
    "erf-init", "erf-validate",
    "form-add", "form-compile", "form-decompile", "form-edit", "form-info", "form-remove", "form-validate",
    "form-patterns",
    "skd-compile", "skd-decompile", "skd-edit", "skd-info", "skd-validate",
    "mxl-compile", "mxl-decompile", "mxl-info", "mxl-validate",
    "template-add", "template-remove", "help-add",
]
EXTRA_GUIDES = ["composing-1c-queries"]          # из 1c-mcp-toolkit: запросы только по реальным метаданным
FORBIDDEN_FLAGS = {"-v8path", "-infobasepath", "-infobaseserver", "-infobaseref", "-username", "-password",
                   "-additionalv8arguments", "-additionalibcmdarguments", "-configsrc"}
PATH_HINT_RE = re.compile(r"[\\/]|\.(xml|bsl|epf|erf|json|txt|html|md|mxl|png|jpg)$", re.I)
MAX_FILE_CHARS = 200_000
MAX_OUTPUT_CHARS = 8_000

mcp = MCPServer(name="dev", instructions=(
    "Разработка внешних обработок и отчётов 1С. Порядок: read_guide нужного скилла → run_skill для "
    "генерации XML → write_file для модулей → build. Пути — относительно рабочей папки."))


def _workspace() -> Path:
    if WORKSPACE is None:
        raise ToolError("Рабочая папка не задана")
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    return WORKSPACE


def _inside(path: str) -> Path:
    """Путь внутри рабочей папки; выход наружу запрещён."""
    ws = _workspace()
    p = (ws / path).resolve()
    if p != ws and ws not in p.parents:
        raise ToolError(f"Путь «{path}» вне рабочей папки")
    return p


def _skill_dir(name: str) -> Path:
    if name in ALLOWED_SKILLS:
        d = SKILLS_DIR / name
    elif name in EXTRA_GUIDES:
        d = EXTRA_GUIDES_DIR / name
    else:
        raise ToolError(f"Скилл «{name}» недоступен. Доступны: {', '.join(ALLOWED_SKILLS + EXTRA_GUIDES)}")
    if not (d / "SKILL.md").is_file():
        raise ToolError(f"Скилл «{name}» не установлен на сервере (scripts/install_1c_skills.ps1)")
    return d


def _description(skill_md: str) -> str:
    m = re.search(r"^description:\s*(.+)$", skill_md, re.M)
    return m.group(1).strip() if m else ""


def _tail(text: str) -> str:
    return text if len(text) <= MAX_OUTPUT_CHARS else "…\n" + text[-MAX_OUTPUT_CHARS:]


@mcp.tool()
def list_guides() -> list[dict]:
    """Скиллы 1С, доступные для разработки обработок и отчётов: имя и назначение."""
    out = []
    for name in ALLOWED_SKILLS + EXTRA_GUIDES:
        try:
            md = (_skill_dir(name) / "SKILL.md").read_text(encoding="utf-8")
        except ToolError:
            continue
        out.append({"name": name, "description": _description(md)})
    return out


@mcp.tool()
def read_guide(name: str) -> str:
    """Инструкция скилла: параметры, форматы XML, примеры. Команды вида
    `powershell … script.ps1 -Param значение` выполняй через run_skill(name, ["-Param", "значение"])."""
    return (_skill_dir(name) / "SKILL.md").read_text(encoding="utf-8")


def _script(skill: Path) -> Path:
    scripts = sorted(p for p in (skill / "scripts").glob("*.py") if not p.stem.startswith("stub"))
    if not scripts:
        raise ToolError(f"У скилла {skill.name} нет python-скрипта")
    return scripts[0]


def _check_args(args: list[str]) -> list[str]:
    checked = []
    for a in args:
        a = str(a)
        if a.startswith("-") and a.lower().split("=")[0] in FORBIDDEN_FLAGS:
            raise ToolError(f"Параметр {a} запрещён: базы и платформу задаёт сервер")
        if not a.startswith("-") and PATH_HINT_RE.search(a):
            _inside(a)     # путь к файлу — только внутри рабочей папки
        checked.append(a)
    return checked


@mcp.tool()
def run_skill(name: str, args: list[str]) -> str:
    """Запустить скрипт скилла (python-версию) в рабочей папке. args — как в инструкции
    скилла, например ["-Name", "ЗагрузкаExcel", "-Synonym", "Загрузка из Excel"]."""
    if name not in ALLOWED_SKILLS:
        raise ToolError(f"run_skill: скилл «{name}» недоступен")
    script = _script(_skill_dir(name))
    cmd = [PYTHON, str(script), *_check_args(args)]
    try:
        r = subprocess.run(cmd, cwd=_workspace(), capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=300, env={**os.environ, "PYTHONUTF8": "1"},
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise ToolError(f"{name}: не уложился в 5 минут") from None
    out = _tail((r.stdout + ("\n" + r.stderr if r.stderr else "")).strip())
    if r.returncode != 0:
        raise ToolError(f"{name} завершился с кодом {r.returncode}:\n{out}")
    return out or "Готово."


@mcp.tool()
def write_file(path: str, content: str) -> str:
    """Записать файл в рабочей папке (модуль BSL, правка XML). Кодировка UTF-8 с BOM для .bsl и .xml."""
    if len(content) > MAX_FILE_CHARS:
        raise ToolError("Файл слишком большой")
    p = _inside(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    enc = "utf-8-sig" if p.suffix.lower() in (".bsl", ".xml") else "utf-8"
    p.write_text(content, encoding=enc)
    return f"Записано: {p.relative_to(_workspace())} ({len(content)} символов)"


@mcp.tool()
def read_file(path: str, offset: int = 0) -> str:
    """Прочитать файл из рабочей папки (по 60 000 символов, offset — с какого символа)."""
    p = _inside(path)
    if not p.is_file():
        raise ToolError(f"Нет файла {path}")
    text = p.read_text(encoding="utf-8-sig", errors="replace")
    chunk = text[offset:offset + 60_000]
    more = f"\n… ещё {len(text) - offset - len(chunk)} символов, offset={offset + len(chunk)}" \
        if offset + len(chunk) < len(text) else ""
    return chunk + more


@mcp.tool()
def list_files(path: str = "") -> list[str]:
    """Файлы рабочей папки (или её подпапки)."""
    root = _inside(path) if path else _workspace()
    if not root.exists():
        return []
    ws = _workspace()
    return sorted(str(p.relative_to(ws)).replace("\\", "/") for p in root.rglob("*") if p.is_file())[:500]


@mcp.tool()
def build(source_xml: str, file_name: str = "") -> str:
    """Собрать внешнюю обработку (.epf) или отчёт (.erf) из корневого XML исходников платформой
    сервера во временной базе. Перед сборкой платформа проверяет модули; ошибки вернутся с
    номером строки. Готовый файл уйдёт сотруднику в чат."""
    src = _inside(source_xml)
    if not src.is_file():
        raise ToolError(f"Нет файла {source_xml}")
    if OUTBOX is None:
        raise ToolError("Каталог для файлов не задан")
    head = src.read_text(encoding="utf-8-sig", errors="replace")[:4000]
    ext = ".erf" if "<ExternalReport" in head else ".epf"
    name = re.sub(r'[\\/:*?"<>|]+', "_", (file_name or src.stem).strip()) or "Обработка"
    if not name.lower().endswith(ext):
        name += ext
    OUTBOX.mkdir(parents=True, exist_ok=True)
    out = OUTBOX / name
    if not (SKILLS_DIR / "epf-build" / "scripts").is_dir():
        raise ToolError("Скилл сборки epf-build не установлен на сервере (scripts/install_1c_skills.ps1)")
    cmd = [PYTHON, str(_script(SKILLS_DIR / "epf-build")), "-SourceFile", str(src), "-OutputFile", str(out)]
    if V8_PATH:
        cmd += ["-V8Path", V8_PATH]
    try:
        r = subprocess.run(cmd, cwd=_workspace(), capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=900, env={**os.environ, "PYTHONUTF8": "1"},
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise ToolError("Сборка не уложилась в 15 минут") from None
    log = _tail((r.stdout + ("\n" + r.stderr if r.stderr else "")).strip())
    if r.returncode != 0 or not out.is_file():
        raise ToolError(f"Сборка не удалась (код {r.returncode}):\n{log}")
    return f"Собрано: {name} ({out.stat().st_size // 1024} КБ) — файл будет отправлен сотруднику.\n{log[-1500:]}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
