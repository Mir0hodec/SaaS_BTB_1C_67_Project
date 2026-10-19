"""Обезличивание данных опубликованных баз (расширение MCP_HTTPService 0.5+), сторона моста.

Расширение отдаёт конфиденциальные значения метками [КАТ-XXXXXX] — к Claude (и в Anthropic)
они не попадают в открытом виде. Мост:
  * привязывается к базе своим ключом (первый POST /anonymization запоминает хеш ключа в базе;
    ключ — в .secrets/toolkit.json под «<база>#anon», в окружение Claude не попадает);
  * отправляет в базу правила (bridge/anonymization_default.yaml или config/anonymization.yaml);
  * перед отправкой ответа в 1С-Коннект подставляет настоящие значения (POST /detokenize)
    в текст и в файлы (xlsx, docx, txt/csv/md). В журнал пишется текст с метками.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

import yaml

from bridge import toolkit_secrets
from bridge.config import REPO_ROOT
from common.onec_login import ADMIN_PASSWORD, ADMIN_USER
from mcp_1c.onec_client import OneCError, OneCExtensionClient

log = logging.getLogger("bridge.anonymization")

DEFAULT_RULES = REPO_ROOT / "bridge" / "anonymization_default.yaml"
CONFIG_RULES = REPO_ROOT / "config" / "anonymization.yaml"
TOKEN_RE = re.compile(r"\[[A-Za-zА-Яа-яЁё]{2,8}-[0-9A-F]{6,64}\]")
TEXT_SUFFIXES = {".txt", ".csv", ".md", ".json", ".xml", ".html"}
CATEGORIES = {
    "ФЛ": "Имена людей", "ОРГ": "Организации и контрагенты", "ИИН": "ИИН, БИН, РНН", "ТЕЛ": "Телефоны",
    "EMAIL": "Электронная почта", "АДР": "Адреса", "СЧЕТ": "Банковские счета", "КАРТА": "Номера карт",
    "ДОК": "Номера документов", "ДОГ": "Договоры", "УДЛ": "Паспорта и удостоверения", "КОД": "Прочие коды и номера",
}
LIST_KEYS = ("tables", "tables_all", "fields")


def load_rules(path: Path | None = None) -> dict:
    """Правила: свои из config/anonymization.yaml, иначе по умолчанию."""
    for p in ([path] if path else [CONFIG_RULES, DEFAULT_RULES]):
        if p.is_file():
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            return normalize(data)
    return normalize({})


def normalize(data: dict) -> dict:
    base = yaml.safe_load(DEFAULT_RULES.read_text(encoding="utf-8")) if DEFAULT_RULES.is_file() else {}
    rules = {**base, **(data or {})}
    rules["enabled"] = bool(rules.get("enabled", True))
    rules["categories"] = [str(c) for c in rules.get("categories") or [] if str(c) in CATEGORIES]
    for key in LIST_KEYS:
        rules[key] = [[str(p[0]), str(p[1])] for p in rules.get(key) or []
                      if isinstance(p, (list, tuple)) and len(p) == 2 and str(p[0]).strip()]
    rules["keep"] = [str(x) for x in rules.get("keep") or [] if str(x).strip()]
    rules["patterns"] = [str(x) for x in rules.get("patterns") or [] if str(x) in CATEGORIES]
    return rules


def save_rules(rules: dict, path: Path | None = None) -> dict:
    rules = normalize(rules)
    path = path or CONFIG_RULES
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Правила обезличивания — пишет панель (Базы 1С → Обезличивание). Формат — "
                    "bridge/anonymization_default.yaml\n"
                    + yaml.safe_dump(rules, allow_unicode=True, sort_keys=False, default_flow_style=None),
                    encoding="utf-8")
    return rules


def revision(rules: dict) -> str:
    return hashlib.sha256(json.dumps(rules, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _key_name(base_id: str) -> str:
    return f"{base_id}#anon"


@dataclass
class State:
    supported: bool          # расширение умеет обезличивание (0.5+)
    enabled: bool            # обезличивание в базе включено
    key: str = ""            # ключ моста для /detokenize; пусто — расшифровать нельзя
    detail: str = ""


def _client(url: str, timeout: float = 30) -> OneCExtensionClient:
    return OneCExtensionClient(url, auth=(ADMIN_USER, ADMIN_PASSWORD), timeout=timeout)


def ensure(base_id: str, url: str, rules: dict | None = None) -> State:
    """Привязать мост к базе и отправить правила, если они в базе другие."""
    client = _client(url)
    try:
        status = client.anonymization()
    except OneCError as exc:
        return State(False, False, detail=f"без обезличивания: расширение старше 0.5 ({exc})")
    rules = rules or load_rules()
    rev = revision(rules)
    key = toolkit_secrets.get_token(_key_name(base_id))
    try:
        if not status.get("paired"):
            key = secrets.token_urlsafe(32)
            status = client.configure_anonymization(key, rules, rev)
            toolkit_secrets.set_token(_key_name(base_id), key)
            log.info("обезличивание: мост привязан к базе %s", base_id)
        elif key and status.get("rules_revision") != rev:
            status = client.configure_anonymization(key, rules, rev)
            log.info("обезличивание: правила обновлены в базе %s", base_id)
        elif key:
            status = client.configure_anonymization(key)       # только проверка ключа
    except OneCError as exc:
        if "wrong bridge key" in str(exc):
            toolkit_secrets.set_token(_key_name(base_id), None)
            key = ""
        else:
            return State(True, bool(status.get("enabled")), key, f"обезличивание: {exc}")
    enabled = bool(status.get("enabled"))
    if not key:
        return State(True, enabled, "", "обезличивание включено, но ключ моста не тот — расшифровки не будет: "
                                         "сбросьте привязку в базе (см. инструкцию администратора)")
    return State(True, enabled, key, "обезличивание включено" if enabled else "обезличивание выключено")


def has_tokens(text: str) -> bool:
    return bool(text and TOKEN_RE.search(text))


def detokenize_texts(url: str, key: str, texts: list[str]) -> list[str]:
    """Метки → настоящие значения. Тексты без меток в базу не отправляются."""
    idx = [i for i, t in enumerate(texts) if has_tokens(t)]
    if not idx:
        return list(texts)
    restored = _client(url, timeout=60).detokenize(key, [texts[i] for i in idx])
    out = list(texts)
    for i, value in zip(idx, restored):
        out[i] = value
    return out


def detokenize_file(url: str, key: str, path: Path) -> bool:
    """Расшифровать файл на месте. True — что-то заменено."""
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        from openpyxl import load_workbook
        wb = load_workbook(path)
        cells = [c for ws in wb.worksheets for row in ws.iter_rows() for c in row
                 if isinstance(c.value, str) and has_tokens(c.value)]
        if not cells:
            return False
        for c, value in zip(cells, detokenize_texts(url, key, [c.value for c in cells])):
            c.value = value
        wb.save(path)
        return True
    if suffix == ".docx":
        from docx import Document
        doc = Document(str(path))
        runs = [r for p in _docx_paragraphs(doc) for r in p.runs if has_tokens(r.text)]
        if not runs:
            return False
        for r, value in zip(runs, detokenize_texts(url, key, [r.text for r in runs])):
            r.text = value
        doc.save(str(path))
        return True
    if suffix in TEXT_SUFFIXES:
        text = path.read_text(encoding="utf-8", errors="replace")
        if not has_tokens(text):
            return False
        path.write_text(detokenize_texts(url, key, [text])[0], encoding="utf-8")
        return True
    return False


def _docx_paragraphs(doc):
    yield from doc.paragraphs
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                yield from cell.paragraphs
