"""Принудительный UTF-8 для stdio на Windows.

MCP-протокол ходит по stdio как JSON. По умолчанию Python на Windows
(особенно при выводе в пайп, а не в консоль, и особенно в русской
локали) кодирует stdout/stderr в кодовую страницу ОС, а не в UTF-8 —
кириллица в данных 1С/базы знаний будет незаметно портиться. Вызывать
этой функцией первой строкой в каждом входном скрипте.
"""
from __future__ import annotations

import sys


def ensure_utf8_stdio() -> None:
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, stream_name)
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
