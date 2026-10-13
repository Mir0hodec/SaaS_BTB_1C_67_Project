"""Белый список сотрудников и выбор базы 1С по хэштегу."""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

_BASE_ID_RE = re.compile(r"^[a-z0-9_]+$")
_HASHTAG_RE = re.compile(r"(?<![\w#])#([\wё-]+)", re.I)


@dataclass(frozen=True)
class User:
    key: str
    name: str
    bases: tuple[str, ...] | str   # "*" или список id
    owner: bool = False

    def can_use(self, base_id: str) -> bool:
        return self.bases == "*" or base_id in self.bases


@dataclass(frozen=True)
class Base:
    id: str
    title: str
    tags: tuple[str, ...]
    toolkit_url: str = ""            # MCP Toolkit на ПК сотрудника: http://<IP>:6003/mcp
    owners: tuple[str, ...] = ()     # кто может выбирать базу; пусто — все, кому открыта по users.yaml
    write: bool = False              # можно ли боту писать в базу (execute_code)
    mode: str = ""                   # mock | http | extension (опубликованная база с расширением MCP_HTTPService)
    url: str = ""                    # адрес публикации базы — для http и extension

    @property
    def via_toolkit(self) -> bool:
        return bool(self.toolkit_url)

    @property
    def via_extension(self) -> bool:
        return self.mode == "extension" and not self.toolkit_url

    def allowed_for(self, user_key: str) -> bool:
        return not self.owners or user_key.lower() in self.owners


def _norm(tag: str) -> str:
    return tag.lower().replace("ё", "е").strip("-_")


def _bases(value) -> tuple[str, ...] | str:
    return "*" if value == "*" else tuple(value or [])


def load_users(path: Path) -> dict[str, User]:
    raw = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("users", {}) or {}
    users = {}
    for key, u in raw.items():
        u = u or {}
        users[str(key)] = User(key=str(key), name=u.get("name", str(key)),
                               bases=_bases(u.get("bases", [])), owner=bool(u.get("owner", False)))
    return users


def resolve_user(path: Path, user_id: str, *, use_deny: bool = True) -> User | None:
    """Сотрудник по user_id 1С-Коннект. При allow_all — любой, кто пишет в линию.
    deny — только для личных сообщений (там сотруднику может отвечать старый бот);
    на своей линии «ИИ-помощник» пересечения нет — use_deny=False."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if use_deny and user_id in {str(x) for x in raw.get("deny") or []}:
        return None   # например, сотрудник, которому пока отвечает другой бот
    known = load_users(path).get(user_id)
    if known:
        return known
    if raw.get("allow_all", False):
        return User(key=user_id, name="Сотрудник", bases=_bases(raw.get("default_bases", "*")))
    return None


def load_bases(path: Path) -> dict[str, Base]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    bases = {}
    for base_id, b in raw.items():
        if not _BASE_ID_RE.match(base_id):
            raise ValueError(
                f"id базы «{base_id}» — только строчная латиница, цифры и _: "
                "Claude Code вычищает кириллицу из имён, инструменты пропадут"
            )
        tags = tuple(_norm(t) for t in [base_id, *b.get("tags", [])])
        bases[base_id] = Base(id=base_id, title=b.get("title", base_id), tags=tags,
                              toolkit_url=str(b.get("toolkit_url") or "").strip(),
                              owners=tuple(str(o).lower() for o in b.get("owners") or []),
                              write=bool(b.get("write", False)), mode=str(b.get("mode") or "").strip(),
                              url=str(b.get("url") or "").strip())
    return bases


@dataclass
class BaseChoice:
    base: Base | None     # найденная база
    tag: str | None       # хэштег из сообщения
    text: str             # сообщение без хэштега базы


def pick_base(text: str, bases: dict[str, Base]) -> BaseChoice:
    """Первый хэштег, похожий на базу. Опечатки прощаются: «#рознца» → «розница»."""
    all_tags = {t: b for b in bases.values() for t in b.tags}
    for m in _HASHTAG_RE.finditer(text):
        tag = _norm(m.group(1))
        base = all_tags.get(tag)
        if base is None:
            close = difflib.get_close_matches(tag, all_tags, n=1, cutoff=0.75)
            base = all_tags[close[0]] if close else None
        rest = re.sub(r"[ \t]{2,}", " ", text[:m.start()] + text[m.end():]).strip()
        if base is not None:
            return BaseChoice(base=base, tag=m.group(1), text=rest)
        return BaseChoice(base=None, tag=m.group(1), text=rest)
    return BaseChoice(base=None, tag=None, text=text.strip())
