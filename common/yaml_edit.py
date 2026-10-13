"""Точечная правка YAML-конфига: меняется одна строка, комментарии и порядок
остаются. Нужна панели управления: settings.yaml и bases.yaml пишет человек,
и перезапись через yaml.dump стёрла бы все пояснения.

Поддерживается ключ верхнего уровня (``allow_all``) и ключ внутри секции
(``connect.ack_text``). Значение пишется в одну строку в виде JSON —
это валидный YAML. Перед записью новый текст разбирается заново и сверяется:
если правка дала не то значение, файл не трогается.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Sequence

import yaml


class YamlEditError(ValueError):
    pass


def _split_comment(rest: str) -> tuple[str, str]:
    """«значение   # комментарий» → («значение», «   # комментарий»)."""
    quote = ""
    depth = 0
    i = 0
    while i < len(rest):
        ch = rest[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 2
                continue
            if ch == quote:
                if quote == "'" and rest[i + 1:i + 2] == "'":
                    i += 2
                    continue
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "#" and (i == 0 or rest[i - 1] in " \t"):
            value = rest[:i]
            stripped = value.rstrip()
            return stripped, rest[len(stripped):]
        i += 1
    return rest.rstrip(), ""


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_content(line: str) -> bool:
    s = line.strip()
    return bool(s) and not s.startswith("#")


def _find_section(lines: list[str], section: str) -> int | None:
    pat = re.compile(rf"^{re.escape(section)}:\s*(#.*)?$")
    for i, line in enumerate(lines):
        if pat.match(line.rstrip("\r\n")):
            return i
    return None


def _section_end(lines: list[str], start: int) -> int:
    """Индекс первой строки после блока секции (следующий ключ верхнего уровня)."""
    i = start + 1
    while i < len(lines):
        if _is_content(lines[i]) and _indent(lines[i]) == 0:
            break
        i += 1
    return i


def _child_indent(lines: list[str], start: int, end: int) -> int:
    for line in lines[start + 1:end]:
        if _is_content(line):
            return _indent(line)
    return 2


def _locate(lines: list[str], keys: Sequence[str]) -> tuple[int | None, int, int, int]:
    """(строка ключа или None, отступ ключа, начало области, конец области)."""
    if len(keys) == 1:
        lo, hi, indent = 0, len(lines), 0
    elif len(keys) == 2:
        sec = _find_section(lines, keys[0])
        if sec is None:
            return None, 2, -1, -1
        hi = _section_end(lines, sec)
        lo, indent = sec + 1, _child_indent(lines, sec, hi)
    else:
        raise YamlEditError("поддерживаются только ключи «ключ» и «секция.ключ»")
    pat = re.compile(rf"^{' ' * indent}{re.escape(keys[-1])}:(\s.*|)$")
    for i in range(lo, hi):
        if _indent(lines[i]) == indent and pat.match(lines[i].rstrip("\r\n")):
            return i, indent, lo, hi
    return None, indent, lo, hi


def render(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def set_value(path: Path, keys: Sequence[str], value: Any) -> None:
    """Записать value по ключу keys в YAML-файл path, сохранив остальной текст."""
    text = path.read_text(encoding="utf-8-sig")
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += newline
    at, indent, lo, hi = _locate(lines, keys)
    key = keys[-1]
    if at is not None:
        line = lines[at].rstrip("\r\n")
        rest = line[indent + len(key) + 1:]
        value_text, comment = _split_comment(rest.strip())
        # Блочное значение (список «- …» на следующих строках) заменяется одной строкой.
        end = at + 1
        if not value_text:
            while end < len(lines):
                ln = lines[end]
                if not _is_content(ln) or _indent(ln) > indent \
                        or (_indent(ln) == indent and ln.lstrip().startswith("- ")):
                    end += 1
                    continue
                break
            while end > at + 1 and not _is_content(lines[end - 1]):
                end -= 1   # пустые строки и комментарии после блока остаются
        if comment and not comment.startswith((" ", "\t")):
            comment = "   " + comment
        lines[at:end] = [f"{' ' * indent}{key}: {render(value)}{comment}{newline}"]
    elif len(keys) == 2 and lo == -1:
        lines += [newline, f"{keys[0]}:{newline}", f"  {key}: {render(value)}{newline}"]
    elif len(keys) == 2:
        last = max((i for i in range(lo, hi) if _is_content(lines[i])), default=lo - 1)
        lines.insert(last + 1, f"{' ' * indent}{key}: {render(value)}{newline}")
    else:
        lines.append(f"{key}: {render(value)}{newline}")

    new_text = "".join(lines)
    try:
        data = yaml.safe_load(new_text) or {}
        got = data
        for k in keys:
            got = got[k]
    except Exception as exc:  # noqa: BLE001
        raise YamlEditError(f"{path.name}: после правки файл не читается ({exc}) — файл не изменён") from None
    if got != value:
        raise YamlEditError(f"{path.name}: {'.'.join(keys)} не удалось записать — файл не изменён")

    shutil.copy2(path, path.with_name(path.name + ".bak"))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, path)


def remove_section(path: Path, section: str) -> bool:
    """Удалить ключ верхнего уровня вместе с его блоком. Комментарии над ним остаются."""
    text = path.read_text(encoding="utf-8-sig")
    lines = text.splitlines(keepends=True)
    start = _find_section(lines, section)
    if start is None:
        return False
    end = _section_end(lines, start)
    while end > start + 1 and not _is_content(lines[end - 1]):
        end -= 1
    new_text = "".join(lines[:start] + lines[end:])
    data = yaml.safe_load(new_text) or {}
    if section in data:
        raise YamlEditError(f"{path.name}: «{section}» не удалось удалить — файл не изменён")
    shutil.copy2(path, path.with_name(path.name + ".bak"))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, path)
    return True
