"""Чистка ответа модели перед отправкой в чат и разбиение на сообщения."""
from __future__ import annotations

import re

_WIN_PATH_RE = re.compile(r"(?:[A-Za-z]:\\|\\\\)[^\s\"'<>|]*")
_UNIX_PATH_RE = re.compile(r"(?<![\w/])/(?:home|tmp|var|usr|etc|Users|opt|root|mnt)/[^\s\"'<>|]*")
_TOOL_RE = re.compile(r"`?mcp__\w+`?")
_HEADER_RE = re.compile(r"^\s{0,3}#{1,6}\s+", re.M)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__", re.S)
_CODE_FENCE_RE = re.compile(r"^```[\w-]*\s*$", re.M)
_SYSTEM_TAG_RE = re.compile(r"</?(system-reminder|thinking|antml:[\w-]+)[^>]*>", re.I)


def clean_answer(text: str) -> str:
    text = _SYSTEM_TAG_RE.sub("", text)
    text = _WIN_PATH_RE.sub("[путь скрыт]", text)
    text = _UNIX_PATH_RE.sub("[путь скрыт]", text)
    text = _TOOL_RE.sub("", text)
    # 1С-Коннект не рендерит Markdown — убираем разметку, текст оставляем.
    text = _CODE_FENCE_RE.sub("", text)
    text = _HEADER_RE.sub("", text)
    text = _BOLD_RE.sub(lambda m: m.group(1) or m.group(2), text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_message(text: str, limit: int) -> list[str]:
    """Режем по абзацам, затем по строкам; слово не рвём, если можно."""
    if len(text) <= limit:
        return [text] if text else []
    parts: list[str] = []
    current = ""
    for block in re.split(r"(\n\n)", text):
        if len(current) + len(block) <= limit:
            current += block
            continue
        if current.strip():
            parts.append(current.strip())
        current = ""
        while len(block) > limit:
            cut = block.rfind("\n", 0, limit)
            if cut <= 0:
                cut = block.rfind(" ", 0, limit)
            if cut <= 0:
                cut = limit
            parts.append(block[:cut].strip())
            block = block[cut:]
        current = block
    if current.strip():
        parts.append(current.strip())
    return parts
