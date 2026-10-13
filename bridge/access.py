"""Белый список сотрудников и список баз 1С (bases.yaml)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

_BASE_ID_RE = re.compile(r"^[a-z0-9_]+$")


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

    @property
    def link(self) -> str:
        """Ссылка, которой сотрудник выбирает базу в разговоре."""
        return self.toolkit_url or self.url

    def allowed_for(self, user_key: str) -> bool:
        return not self.owners or user_key.lower() in self.owners


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


def resolve_user(path: Path, user_id: str) -> User | None:
    """Сотрудник по user_id 1С-Коннект. При allow_all — любой, кто пишет в линию."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
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
        bases[base_id] = Base(id=base_id, title=b.get("title", base_id),
                              toolkit_url=str(b.get("toolkit_url") or "").strip(),
                              owners=tuple(str(o).lower() for o in b.get("owners") or []),
                              write=bool(b.get("write", False)), mode=str(b.get("mode") or "").strip(),
                              url=str(b.get("url") or "").strip())
    return bases

