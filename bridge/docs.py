"""Инструкции и комплект файлов для сотрудников.

Тексты инструкций — Markdown в docs/. Панель показывает их в разделе «Справка»,
бот присылает инструкцию для сотрудников файлом (HTML: открывается в браузере,
печатается). Комплект /toolkit — MCP Toolkit, скрипт настройки ПК, инструкция и
всё, что администратор положил в папку extensions/ (например, расширение .cfe).
"""
from __future__ import annotations

import html
import re
from pathlib import Path

from bridge.config import REPO_ROOT

DOCS_DIR = REPO_ROOT / "docs"
DOCS = {
    "employees": ("Инструкция для сотрудников", DOCS_DIR / "Инструкция для сотрудников.md"),
    "admin": ("Инструкция администратора", DOCS_DIR / "Инструкция администратора.md"),
}
EXTENSIONS_DIR = REPO_ROOT / "extensions"
EXTENSIONS_README = "README.txt"

_LIST_RE = re.compile(r"^(\s*)([-*]|\d+[.)])\s+(.*)$")
_HEADING_RE = re.compile(r"^(#{1,4})\s+(.*)$")


# --- Markdown → HTML (подмножество, которым написаны docs/) ---

def _inline(text: str) -> str:
    codes: list[str] = []

    def keep(m: re.Match) -> str:
        codes.append(m.group(1))
        return f"\x00{len(codes) - 1}\x00"

    text = re.sub(r"`([^`]+)`", keep, text)
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s\"<>]+)\)", r'<a href="\2" target="_blank" rel="noopener">\1</a>', text)
    return re.sub(r"\x00(\d+)\x00", lambda m: "<code>" + html.escape(codes[int(m.group(1))]) + "</code>", text)


def _list(block: list[str]) -> str:
    items: list[list] = []                     # [отступ, нумерованный, номер, текст]
    for line in block:
        m = _LIST_RE.match(line)
        if m:
            items.append([len(m.group(1).expandtabs(4)), m.group(2)[0].isdigit(), m.group(2), m.group(3)])
        elif items:
            items[-1][3] += " " + line.strip()

    def build(pos: int) -> tuple[str, int]:
        indent, ordered, number = items[pos][0], items[pos][1], items[pos][2]
        parts: list[str] = []
        while pos < len(items) and items[pos][0] >= indent:
            if items[pos][0] > indent and parts:
                sub, pos = build(pos)
                parts[-1] = parts[-1][:-len("</li>")] + sub + "</li>"
                continue
            parts.append("<li>" + _inline(items[pos][3]) + "</li>")
            pos += 1
        start = int(number.rstrip(".)")) if ordered else 1
        tag = "ol" if ordered else "ul"
        attr = f' start="{start}"' if ordered and start != 1 else ""
        return f"<{tag}{attr}>" + "".join(parts) + f"</{tag}>", pos

    out, pos = [], 0
    while pos < len(items):
        part, pos = build(pos)
        out.append(part)
    return "".join(out)


def _table(rows: list[str]) -> str:
    cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
    body = [r for r in cells if not all(re.fullmatch(r":?-{2,}:?", c) for c in r if c)]
    if not body:
        return ""
    head, *rest = body
    return ("<table><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head) + "</tr></thead><tbody>"
            + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in rest)
            + "</tbody></table>")


def _starts_block(line: str) -> bool:
    return bool(_HEADING_RE.match(line) or _LIST_RE.match(line) or line.startswith(("```", ">", "|")))


def render_markdown(text: str) -> str:
    lines = text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
        elif line.startswith("```"):
            j = i + 1
            while j < len(lines) and not lines[j].startswith("```"):
                j += 1
            out.append("<pre><code>" + html.escape("\n".join(lines[i + 1:j]), quote=False) + "</code></pre>")
            i = j + 1
        elif m := _HEADING_RE.match(line):
            level = len(m.group(1))
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            i += 1
        elif line.lstrip().startswith("|"):
            j = i
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                j += 1
            out.append(_table(lines[i:j]))
            i = j
        elif line.startswith(">"):
            j = i
            while j < len(lines) and lines[j].startswith(">"):
                j += 1
            out.append("<blockquote>" + render_markdown("\n".join(x[1:].lstrip() for x in lines[i:j])) + "</blockquote>")
            i = j
        elif _LIST_RE.match(line):
            j = i
            while j < len(lines) and (_LIST_RE.match(lines[j]) or (lines[j][:1] in (" ", "\t") and lines[j].strip())):
                j += 1
            out.append(_list(lines[i:j]))
            i = j
        else:
            j = i
            while j < len(lines) and lines[j].strip() and (j == i or not _starts_block(lines[j])):
                j += 1
            out.append("<p>" + _inline(" ".join(x.strip() for x in lines[i:j])) + "</p>")
            i = j
    return "\n".join(out)


STANDALONE_CSS = """
body{margin:0;background:#f6f7fb;color:#1d2433;font:15px/1.6 "Segoe UI",system-ui,sans-serif}
main{max-width:860px;margin:32px auto;background:#fff;border-radius:14px;padding:36px 44px;
box-shadow:0 1px 3px rgba(20,30,60,.08)}
h1{font-size:26px;margin:0 0 18px}h2{font-size:20px;margin:30px 0 10px;padding-top:14px;border-top:1px solid #e6e8ef}
h3{font-size:16px;margin:20px 0 8px}p{margin:8px 0}li{margin:3px 0}
code{background:#f0f1f6;border-radius:5px;padding:1px 5px;font:13px Consolas,monospace}
pre{background:#f0f1f6;border-radius:8px;padding:12px 14px;overflow:auto}pre code{padding:0;background:none}
table{border-collapse:collapse;width:100%;margin:10px 0;font-size:14px}
th,td{border:1px solid #e1e4ec;padding:7px 10px;text-align:left;vertical-align:top}th{background:#f6f7fb}
blockquote{margin:10px 0;padding:8px 14px;background:#fff8e6;border-left:4px solid #f0b429;border-radius:6px}
a{color:#4b3fd6}
@media print{body{background:#fff}main{box-shadow:none;margin:0;padding:0;max-width:none}}
"""


def standalone_html(doc_id: str) -> tuple[str, str]:
    """Отдельная HTML-страница инструкции — (имя файла, текст)."""
    title, path = DOCS[doc_id]
    body = render_markdown(path.read_text(encoding="utf-8"))
    page = (f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>{html.escape(title)}</title>'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><style>{STANDALONE_CSS}</style>'
            f"</head><body><main>{body}</main></body></html>")
    return f"{title}.html", page


def instruction_file(target_dir: Path, doc_id: str = "employees") -> Path:
    """Файл инструкции для отправки в чат; пересобирается, когда меняется Markdown."""
    name, page = standalone_html(doc_id)
    target = target_dir / name
    source = DOCS[doc_id][1]
    if not target.exists() or target.stat().st_mtime < source.stat().st_mtime:
        target_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(page, encoding="utf-8")
    return target


# --- комплект для сотрудника ---

def extension_files(directory: Path | None = None) -> list[Path]:
    """Файлы, которые администратор положил в extensions/ (README не отправляется)."""
    directory = directory or EXTENSIONS_DIR
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir()
                  if p.is_file() and p.name != EXTENSIONS_README and not p.name.startswith("."))


def toolkit_files() -> list[Path]:
    return [p for p in (REPO_ROOT / "tools" / "MCP_Toolkit.epf", REPO_ROOT / "tools" / "MCP_Toolkit_x86.epf",
                        REPO_ROOT / "scripts" / "employee_toolkit.ps1") if p.is_file()]
