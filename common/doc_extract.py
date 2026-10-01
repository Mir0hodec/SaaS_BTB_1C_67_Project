"""Извлечение текста из файлов базы знаний: docx, pdf, xlsx/xlsm, txt/md.

Только чтение исходников. Картинки (скриншоты в инструкциях) не
распознаются — индексируется только текстовый слой. Для xlsm читаются
только значения ячеек, макросы не исполняются.
"""
from __future__ import annotations

import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

SUPPORTED_EXTENSIONS = {".docx", ".pdf", ".xlsx", ".xlsm", ".txt", ".md"}

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def extract_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".docx":
        return _docx(path)
    if ext == ".pdf":
        return _pdf(path)
    if ext in (".xlsx", ".xlsm"):
        return _xlsx(path)
    if ext in (".txt", ".md"):
        return path.read_text(encoding="utf-8", errors="replace")
    raise ValueError(f"Неподдерживаемый формат: {ext}")


def _docx(path: Path) -> str:
    with zipfile.ZipFile(path) as z, z.open("word/document.xml") as f:
        root = ET.parse(f).getroot()
    # Runs одного абзаца склеиваются без разделителя: Word дробит фразу на
    # несколько <w:t> при смене форматирования внутри неё.
    paragraphs = ("".join(t.text for t in p.iter(f"{_W}t") if t.text) for p in root.iter(f"{_W}p"))
    return "\n".join(p for p in paragraphs if p.strip())


def _pdf(path: Path) -> str:
    from pypdf import PdfReader

    pages = []
    for page in PdfReader(str(path)).pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 — битая страница не должна ронять весь документ
            continue
    return "\n".join(pages)


def _xlsx(path: Path) -> str:
    import openpyxl

    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    try:
        lines = []
        for ws in wb.worksheets:
            lines.append(f"# Лист: {ws.title}")
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None and str(c).strip()]
                if cells:
                    lines.append(" | ".join(cells))
        return "\n".join(lines)
    finally:
        wb.close()
