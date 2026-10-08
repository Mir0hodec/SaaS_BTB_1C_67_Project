"""Индексатор кода 1С: выгрузка конфигурации в файлы → один файл SQLite (FTS5, trigram).

    python onec_rag/onec_index.py                 # инкрементально, по config.json
    python onec_rag/onec_index.py --full          # перестроить с нуля
    python onec_rag/onec_index.py --no-pull       # без git pull
    python onec_rag/onec_index.py --stats         # только показать состояние индекса

Только stdlib. Постоянных процессов нет: скрипт запускается Планировщиком
задач Windows, делает `git pull` в репозиториях выгрузки и доиндексирует
изменившиеся файлы (сравнение по mtime+size). Пишет в WAL-режиме, поэтому
MCP-сервер (onec_mcp.py, только чтение) во время переиндексации продолжает
отвечать по прежнему состоянию.

Форматы выгрузки: Конфигуратор (…/Ext/ObjectModule.bsl, Documents/Имя.xml)
и EDT (src/…/ObjectModule.bsl, Имя.mdo) — оба разбираются по одним правилам.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import onec_embed as emb  # noqa: E402  (сам модуль — только stdlib; тяжёлые пакеты грузит по требованию)

DEFAULT_CONFIG = HERE / "config.json"
CONFIG_ENV = "ONEC_RAG_CONFIG"
SCHEMA_VERSION = "3"
MAX_XML_BYTES = 5 * 1024 * 1024
COMMIT_EVERY_FILES = 500

log = logging.getLogger("onec_index")


# --- конфиг ---

@dataclass
class Repo:
    name: str
    path: Path
    git_pull: bool = True


@dataclass
class Config:
    index_path: Path
    repos: list[Repo]
    log_path: Path | None
    search: dict
    semantic: dict

    def repo(self, name: str) -> Repo | None:
        return next((r for r in self.repos if r.name == name), None)


SEARCH_DEFAULTS = {
    "default_limit": 8,
    "max_limit": 20,
    "max_output_chars": 12000,
    "candidates": 400,
    "weights": {"path": 3.0, "name": 10.0, "doc": 4.0, "body": 1.0},
    "synonyms": {},
}


def config_path(explicit: str | None = None) -> Path:
    return Path(explicit or os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG).resolve()


def load_config(path: Path | None = None) -> Config:
    path = path or config_path()
    # utf-8-sig: Блокнот Windows сохраняет UTF-8 с BOM.
    raw = json.loads(path.read_text(encoding="utf-8-sig"))

    def rel(value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else (path.parent / p).resolve()

    repos = [Repo(name=str(r["name"]), path=rel(r["path"]), git_pull=bool(r.get("git_pull", True)))
             for r in raw.get("repos", [])]
    names = [r.name for r in repos]
    if len(set(names)) != len(names):
        raise ValueError("config: имена репозиториев (repos[].name) должны быть разными")
    for name in names:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise ValueError(f"config: имя репозитория «{name}» — только латиница, цифры, _ . -")
    search = {**SEARCH_DEFAULTS, **(raw.get("search") or {})}
    search["weights"] = {**SEARCH_DEFAULTS["weights"], **(search.get("weights") or {})}
    semantic = {**emb.DEFAULTS, **(raw.get("semantic") or {})}
    semantic["model_dir"] = rel(semantic["model_dir"])
    return Config(
        index_path=rel(raw.get("index_path", "../.index/onec_code.sqlite3")),
        repos=repos,
        log_path=rel(raw["log_path"]) if raw.get("log_path") else None,
        search=search,
        semantic=semantic,
    )


# --- файловая система Windows: длинные пути и кириллица ---

def fs_path(p: str | Path) -> str:
    """Путь для системных вызовов. На Windows — с префиксом \\\\?\\, иначе пути
    длиннее 260 символов (обычное дело для выгрузки 1С) не открываются."""
    s = os.path.abspath(str(p))
    if os.name == "nt" and not s.startswith("\\\\?\\"):
        s = "\\\\?\\UNC\\" + s[2:] if s.startswith("\\\\") else "\\\\?\\" + s
    return s


def read_text(path: str | Path) -> str:
    data = Path(fs_path(path)).read_bytes()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1251", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


# --- метаданные из пути ---

# (папка выгрузки, имя вида в XML/типах, русское имя вида)
_KINDS = [
    ("CommonModules", "CommonModule", "ОбщийМодуль"),
    ("Catalogs", "Catalog", "Справочник"),
    ("Documents", "Document", "Документ"),
    ("DocumentJournals", "DocumentJournal", "ЖурналДокументов"),
    ("Enums", "Enum", "Перечисление"),
    ("Reports", "Report", "Отчет"),
    ("DataProcessors", "DataProcessor", "Обработка"),
    ("InformationRegisters", "InformationRegister", "РегистрСведений"),
    ("AccumulationRegisters", "AccumulationRegister", "РегистрНакопления"),
    ("AccountingRegisters", "AccountingRegister", "РегистрБухгалтерии"),
    ("CalculationRegisters", "CalculationRegister", "РегистрРасчета"),
    ("ChartsOfCharacteristicTypes", "ChartOfCharacteristicTypes", "ПланВидовХарактеристик"),
    ("ChartsOfAccounts", "ChartOfAccounts", "ПланСчетов"),
    ("ChartsOfCalculationTypes", "ChartOfCalculationTypes", "ПланВидовРасчета"),
    ("BusinessProcesses", "BusinessProcess", "БизнесПроцесс"),
    ("Tasks", "Task", "Задача"),
    ("ExchangePlans", "ExchangePlan", "ПланОбмена"),
    ("Constants", "Constant", "Константа"),
    ("CommonForms", "CommonForm", "ОбщаяФорма"),
    ("CommonCommands", "CommonCommand", "ОбщаяКоманда"),
    ("WebServices", "WebService", "WebСервис"),
    ("HTTPServices", "HTTPService", "HTTPСервис"),
    ("SettingsStorages", "SettingsStorage", "ХранилищеНастроек"),
    ("FilterCriteria", "FilterCriterion", "КритерийОтбора"),
    ("Sequences", "Sequence", "Последовательность"),
    ("ScheduledJobs", "ScheduledJob", "РегламентноеЗадание"),
    ("EventSubscriptions", "EventSubscription", "ПодпискаНаСобытие"),
    ("Subsystems", "Subsystem", "Подсистема"),
    ("DefinedTypes", "DefinedType", "ОпределяемыйТип"),
    ("FunctionalOptions", "FunctionalOption", "ФункциональнаяОпция"),
    ("CommonAttributes", "CommonAttribute", "ОбщийРеквизит"),
    ("SessionParameters", "SessionParameter", "ПараметрСеанса"),
    ("ExternalDataSources", "ExternalDataSource", "ВнешнийИсточникДанных"),
    ("Roles", "Role", "Роль"),
    ("CommonTemplates", "CommonTemplate", "ОбщийМакет"),
]
TYPE_FOLDERS = {folder: ru for folder, _, ru in _KINDS}
KIND_RU = {single: ru for _, single, ru in _KINDS}
# Структуру из XML берём только там, где она отвечает на вопросы сотрудников.
STRUCT_FOLDERS = set(TYPE_FOLDERS) - {"Roles", "CommonTemplates", "CommonModules", "CommonForms"}

MODULE_KINDS = {
    "objectmodule": "МодульОбъекта",
    "managermodule": "МодульМенеджера",
    "recordsetmodule": "МодульНабораЗаписей",
    "valuemanagermodule": "МодульМенеджераЗначения",
    "commandmodule": "МодульКоманды",
    "managedapplicationmodule": "МодульУправляемогоПриложения",
    "ordinaryapplicationmodule": "МодульОбычногоПриложения",
    "sessionmodule": "МодульСеанса",
    "externalconnectionmodule": "МодульВнешнегоСоединения",
}


@dataclass
class PathMeta:
    object_type: str      # Документ, Справочник, ОбщийМодуль… («Конфигурация» — модули корня)
    object_name: str
    module_kind: str      # МодульОбъекта, МодульФормы… («Структура» — для XML)
    sub_name: str = ""    # имя формы или команды

    @property
    def full_name(self) -> str:
        return f"{self.object_type}.{self.object_name}" if self.object_name else self.object_type

    @property
    def label(self) -> str:
        return " ".join(x for x in (self.full_name, self.module_kind, self.sub_name) if x)


def path_meta(rel: str) -> PathMeta:
    parts = rel.split("/")
    stem = parts[-1].rsplit(".", 1)[0]
    idx = next((i for i, p in enumerate(parts[:-1]) if p in TYPE_FOLDERS), None)
    if idx is None:
        kind = MODULE_KINDS.get(stem.lower(), "Модуль" if rel.lower().endswith(".bsl") else "Структура")
        return PathMeta("Конфигурация", "", kind)
    folder = parts[idx]
    name = parts[idx + 1]
    if idx + 1 == len(parts) - 1:          # Documents/Имя.xml
        name = stem
    rest = parts[idx + 2:]
    if not rel.lower().endswith(".bsl"):
        return PathMeta(TYPE_FOLDERS[folder], name, "Структура")
    sub = ""
    if "Forms" in rest and rest.index("Forms") + 1 < len(rest) - 1:
        kind, sub = "МодульФормы", rest[rest.index("Forms") + 1]
    elif "Commands" in rest and rest.index("Commands") + 1 < len(rest) - 1:
        kind, sub = "МодульКоманды", rest[rest.index("Commands") + 1]
    elif stem.lower() in MODULE_KINDS:
        kind = MODULE_KINDS[stem.lower()]
    elif folder == "CommonModules":
        kind = "ОбщийМодуль"
    elif folder == "CommonForms":
        kind = "МодульФормы"
    elif folder in ("WebServices", "HTTPServices"):
        kind = "МодульСервиса"
    else:
        kind = "Модуль"
    return PathMeta(TYPE_FOLDERS[folder], name, kind, sub)


def is_object_file(rel: str) -> bool:
    """XML/MDO с описанием объекта конфигурации (не формы, не права, не макеты)."""
    parts = rel.split("/")
    low = parts[-1].lower()
    if low.endswith(".xml"):           # Конфигуратор: Documents/Имя.xml
        return len(parts) >= 2 and parts[-2] in STRUCT_FOLDERS
    if low.endswith(".mdo"):           # EDT: Documents/Имя/Имя.mdo
        return len(parts) >= 3 and parts[-3] in STRUCT_FOLDERS and parts[-1][:-4] == parts[-2]
    return False


# --- чанкинг BSL ---

_IDENT = r"[A-Za-zА-Яа-яЁё_][0-9A-Za-zА-Яа-яЁё_]*"
PROC_START = re.compile(
    rf"^[ \t]*(?:(?:Асинх|Async)[ \t]+)?(Процедура|Функция|Procedure|Function)[ \t]+({_IDENT})[ \t]*\(", re.I)
PROC_END = re.compile(
    r"^[ \t]*(?:КонецПроцедуры|КонецФункции|EndProcedure|EndFunction)(?![0-9A-Za-zА-Яа-яЁё_])", re.I)
_HEADER_END = re.compile(r"\)[ \t]*(Экспорт|Export)?[ \t]*;?[ \t]*(//.*)?$", re.I)
_FUNC_WORDS = ("функция", "function")


@dataclass
class Chunk:
    kind: str             # proc | func | module | object
    name: str
    line_start: int       # с 1, включительно
    line_end: int
    body: str
    is_export: bool = False
    text: str | None = None   # исходный текст (хранится только для структуры объекта)
    doc: str = ""             # описание: комментарий над процедурой без «//»


def _meaningful(line: str) -> bool:
    s = line.strip()
    return bool(s) and not s.startswith(("//", "#"))


def _is_export(lines: list[str], start: int, end: int) -> bool:
    for line in lines[start:min(end + 1, start + 60)]:
        m = _HEADER_END.search(line)
        if m:
            return bool(m.group(1))
    return False


def chunk_bsl(text: str) -> list[Chunk]:
    """Процедура/Функция … КонецПроцедуры/КонецФункции — один чанк вместе с
    комментариями `//` и директивами `&НаСервере` над ней. Код вне процедур
    (переменные модуля, тело модуля) — отдельные чанки kind=module. Модуль
    без процедур — один чанк."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    chunks: list[Chunk] = []
    n = len(lines)

    def gap(a: int, b: int) -> None:      # строки [a, b) вне процедур
        while a < b and not lines[a].strip():
            a += 1
        while b > a and not lines[b - 1].strip():
            b -= 1
        if a < b and any(_meaningful(x) for x in lines[a:b]):
            chunks.append(Chunk("module", "", a + 1, b, "\n".join(lines[a:b])))

    starts = [i for i, line in enumerate(lines) if PROC_START.match(line)]
    if not starts:
        if text.strip():
            chunks.append(Chunk("module", "", 1, max(n, 1), "\n".join(lines)))
        return chunks

    prev_end = 0                           # первая строка после предыдущего чанка
    for pos, i in enumerate(starts):
        if i < prev_end:
            continue                       # «Процедура» внутри незакрытой предыдущей
        limit = starts[pos + 1] if pos + 1 < len(starts) else n
        j = next((k for k in range(i, limit) if PROC_END.match(lines[k])), None)
        if j is None:                      # нет КонецПроцедуры — до следующей процедуры
            j = limit - 1
            while j > i and not lines[j].strip():
                j -= 1
        k = i
        while k > prev_end and lines[k - 1].lstrip().startswith(("//", "&")):
            k -= 1
        gap(prev_end, k)
        m = PROC_START.match(lines[i])
        kind = "func" if m.group(1).lower() in _FUNC_WORDS else "proc"
        doc = " ".join(x.strip().lstrip("/").strip() for x in lines[k:i] if x.lstrip().startswith("//"))
        chunks.append(Chunk(kind, m.group(2), k + 1, j + 1, "\n".join(lines[k:j + 1]),
                            is_export=_is_export(lines, i, j), doc=" ".join(doc.split())))
        prev_end = j + 1
    gap(prev_end, n)
    return chunks


# --- структура объекта из XML (Конфигуратор) и MDO (EDT) ---

_PRIMITIVES = {
    "string": "Строка", "decimal": "Число", "number": "Число", "boolean": "Булево",
    "datetime": "Дата", "date": "Дата", "valuestorage": "ХранилищеЗначения", "uuid": "УникальныйИдентификатор",
}
_GROUPS_CFG = {
    "Attribute": "Реквизиты", "Dimension": "Измерения", "Resource": "Ресурсы",
    "EnumValue": "Значения", "AccountingFlag": "Признаки учета",
    "ExtDimensionAccountingFlag": "Признаки учета субконто",
    "AddressingAttribute": "Реквизиты адресации", "Column": "Графы",
    "Operation": "Операции", "URLTemplate": "Шаблоны URL", "Subsystem": "Подсистемы",
}
_GROUPS_EDT = {
    "attributes": "Реквизиты", "dimensions": "Измерения", "resources": "Ресурсы",
    "enumValues": "Значения", "accountingFlags": "Признаки учета",
    "extDimensionAccountingFlags": "Признаки учета субконто",
    "addressingAttributes": "Реквизиты адресации", "columns": "Графы",
    "operations": "Операции", "urlTemplates": "Шаблоны URL", "subsystems": "Подсистемы",
}
_EXTRA_LABELS = {
    "methodname": "Метод", "handler": "Обработчик", "event": "Событие",
    "registerrecords": "Движения по регистрам", "source": "Источники", "type": "Тип",
}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(el, name: str):
    return next((c for c in el if _local(c.tag) == name), None) if el is not None else None


def _children(el, name: str) -> list:
    c = _child(el, name)
    return list(c) if c is not None else []


def _text(el, name: str) -> str:
    c = _child(el, name)
    return (c.text or "").strip() if c is not None else ""


def ru_type(value: str) -> str:
    """cfg:CatalogRef.Контрагенты → СправочникСсылка.Контрагенты; xs:string → Строка."""
    v = value.strip().split(":", 1)[-1]
    if not v:
        return ""
    head, _, tail = v.partition(".")
    if head.lower() in _PRIMITIVES and not tail:
        return _PRIMITIVES[head.lower()]
    for suffix, ru_suffix in (("Ref", "Ссылка"), ("Object", "Объект")):
        if head.endswith(suffix) and head[:-len(suffix)] in KIND_RU:
            return KIND_RU[head[:-len(suffix)]] + ru_suffix + ("." + tail if tail else "")
    if head in KIND_RU:
        return KIND_RU[head] + ("." + tail if tail else "")
    return v


def _types(el) -> str:
    """Все типы внутри элемента <Type>/<type>."""
    if el is None:
        return ""
    found = [ru_type(t.text or "") for t in el.iter() if _local(t.tag) in ("Type", "TypeSet", "types") and (t.text or "").strip()]
    return ", ".join(dict.fromkeys(x for x in found if x))


def _synonym_cfg(props) -> str:
    syn = _child(props, "Synonym")
    if syn is None:
        return ""
    return next(((c.text or "").strip() for c in syn.iter() if _local(c.tag) == "content" and (c.text or "").strip()), "")


def _synonym_edt(el) -> str:
    for syn in el:
        if _local(syn.tag) == "synonym":
            value = _text(syn, "value")
            if value:
                return value
    return ""


def _new_object(name: str, synonym: str, comment: str) -> dict:
    return {"name": name, "synonym": synonym, "comment": comment, "groups": {}, "tabular": [],
            "forms": [], "commands": [], "templates": [], "extra": {}}


def _extra(o: dict, key: str, value: str) -> None:
    if value:
        label = _EXTRA_LABELS[key.lower()]
        o["extra"][label] = f"{o['extra'][label]}, {value}" if label in o["extra"] else value


def _parse_configurator(root) -> dict | None:
    if not len(root):
        return None
    obj = root[0]
    props = _child(obj, "Properties")
    if props is None or not _text(props, "Name"):
        return None
    o = _new_object(_text(props, "Name"), _synonym_cfg(props), _text(props, "Comment"))
    for key in ("MethodName", "Handler", "Event"):
        _extra(o, key, _text(props, key))
    _extra(o, "Type", _types(_child(props, "Type")))
    for key in ("RegisterRecords", "Source"):
        el = _child(props, key)
        if el is not None:
            _extra(o, key, ", ".join(ru_type(i.text or "") for i in el.iter() if (i.text or "").strip()))

    def attr(el) -> tuple[str, str, str]:
        p = _child(el, "Properties")
        return _text(p, "Name"), _types(_child(p, "Type")), _synonym_cfg(p) if p is not None else ""

    for ch in _children(obj, "ChildObjects"):
        tag = _local(ch.tag)
        if tag == "TabularSection":
            p = _child(ch, "Properties")
            cols = [attr(a) for a in _children(ch, "ChildObjects") if _local(a.tag) == "Attribute"]
            o["tabular"].append((_text(p, "Name"), _synonym_cfg(p) if p is not None else "", cols))
        elif tag in ("Form", "Template", "Command"):
            name = _text(_child(ch, "Properties"), "Name") or (ch.text or "").strip()
            o[{"Form": "forms", "Template": "templates", "Command": "commands"}[tag]].append(name)
        elif tag in _GROUPS_CFG:
            item = attr(ch) if _child(ch, "Properties") is not None else ((ch.text or "").strip(), "", "")
            o["groups"].setdefault(_GROUPS_CFG[tag], []).append(item)
    return o


def _parse_edt(root) -> dict | None:
    name = _text(root, "name")
    if not name:
        return None
    o = _new_object(name, _synonym_edt(root), _text(root, "comment"))

    def attr(el) -> tuple[str, str, str]:
        return _text(el, "name"), _types(_child(el, "type")), _synonym_edt(el)

    for ch in root:
        tag = _local(ch.tag)
        if tag == "tabularSections":
            cols = [attr(a) for a in ch if _local(a.tag) == "attributes"]
            o["tabular"].append((_text(ch, "name"), _synonym_edt(ch), cols))
        elif tag in ("forms", "templates", "commands"):
            o[tag].append(_text(ch, "name") or (ch.text or "").strip())
        elif tag in _GROUPS_EDT:
            item = attr(ch) if _text(ch, "name") else ((ch.text or "").strip(), "", "")
            o["groups"].setdefault(_GROUPS_EDT[tag], []).append(item)
        elif tag in ("methodName", "handler", "event"):
            _extra(o, tag, (ch.text or "").strip())
        elif tag in ("registerRecords", "source"):
            _extra(o, tag, _types(ch) or ru_type(ch.text or ""))
        elif tag == "type":
            _extra(o, "type", _types(ch))
    return o


def _render_attr(item: tuple[str, str, str]) -> str:
    name, type_, synonym = item
    s = name
    if type_:
        s += f" ({type_})"
    if synonym and synonym.replace(" ", "").lower() != name.lower():
        s += f" — «{synonym}»"
    return s


def render_object(full_name: str, o: dict) -> str:
    lines = [full_name + (f" — «{o['synonym']}»" if o["synonym"] else "")]
    if o["comment"]:
        lines.append(f"Комментарий: {o['comment']}")
    for label, value in o["extra"].items():
        lines.append(f"{label}: {value}")
    for label, items in o["groups"].items():
        lines.append(f"{label}: " + "; ".join(_render_attr(i) for i in items if i[0]))
    for name, synonym, cols in o["tabular"]:
        head = f"Табличная часть {name}" + (f" («{synonym}»)" if synonym else "")
        lines.append(head + ": " + "; ".join(_render_attr(c) for c in cols if c[0]))
    for key, label in (("forms", "Формы"), ("commands", "Команды"), ("templates", "Макеты")):
        if o[key]:
            lines.append(f"{label}: " + ", ".join(x for x in o[key] if x))
    return "\n".join(lines)


def chunk_object(rel: str, raw: str) -> Chunk | None:
    try:
        root = ET.fromstring(raw.encode("utf-8"))
    except ET.ParseError:
        return None
    o = _parse_edt(root) if rel.lower().endswith(".mdo") else (
        _parse_configurator(root) if _local(root.tag) == "MetaDataObject" else None)
    if o is None:
        return None
    meta = path_meta(rel)
    text = render_object(f"{meta.object_type}.{o['name']}", o)
    name = o["name"] + (f" {o['synonym']}" if o["synonym"] else "")
    return Chunk("object", name, 1, max(1, raw.count("\n") + (0 if raw.endswith("\n") else 1)), text, text=text)


# --- база ---

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files(
    id INTEGER PRIMARY KEY, repo TEXT NOT NULL, path TEXT NOT NULL, kind TEXT NOT NULL,
    mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL, lines INTEGER NOT NULL,
    UNIQUE(repo, path));
CREATE TABLE IF NOT EXISTS chunks(
    id INTEGER PRIMARY KEY AUTOINCREMENT,      -- номера не переиспользуются: на них ссылаются векторы
    file_id INTEGER NOT NULL REFERENCES files(id),
    kind TEXT NOT NULL, object_type TEXT NOT NULL, object_name TEXT NOT NULL,
    object_lower TEXT NOT NULL, module_kind TEXT NOT NULL, sub_name TEXT NOT NULL,
    proc_name TEXT NOT NULL, proc_lower TEXT NOT NULL, is_export INTEGER NOT NULL,
    line_start INTEGER NOT NULL, line_end INTEGER NOT NULL, text TEXT);
CREATE INDEX IF NOT EXISTS chunks_file ON chunks(file_id);
CREATE INDEX IF NOT EXISTS chunks_proc ON chunks(proc_lower);
CREATE INDEX IF NOT EXISTS chunks_object ON chunks(object_lower);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(path, name, doc, body, tokenize='trigram');
"""


def fold(text: str) -> str:
    """Trigram не сводит «ё» к «е» — делаем это сами и в индексе, и в запросе."""
    return text.replace("ё", "е").replace("Ё", "Е")


def _open(index_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(index_path), timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def connect_rw(index_path: Path) -> sqlite3.Connection:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _open(index_path)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone():
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if not row or row[0] != SCHEMA_VERSION:
            log.info("схема индекса изменилась — индекс будет построен заново")
            conn.close()
            for suffix in ("", "-wal", "-shm"):
                Path(str(index_path) + suffix).unlink(missing_ok=True)
            conn = _open(index_path)
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR REPLACE INTO meta VALUES('schema_version', ?)", (SCHEMA_VERSION,))
    conn.commit()
    return conn


def _delete_file_chunks(conn: sqlite3.Connection, file_id: int) -> None:
    conn.execute("DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE file_id=?)", (file_id,))
    conn.execute("DELETE FROM chunks WHERE file_id=?", (file_id,))


def _index_file(conn: sqlite3.Connection, repo: Repo, rel: str, fsp: str, mtime_ns: int, size: int,
                kind: str, file_id: int | None) -> int:
    """Возвращает число чанков файла."""
    if kind == "xml" and size > MAX_XML_BYTES:
        chunks, lines = [], 0
    else:
        raw = read_text(fsp)
        lines = raw.count("\n") + (0 if raw.endswith("\n") or not raw else 1)
        if kind == "bsl":
            chunks = chunk_bsl(raw)
        else:
            obj = chunk_object(rel, raw)
            chunks = [obj] if obj else []
    if file_id is None:
        file_id = conn.execute(
            "INSERT INTO files(repo, path, kind, mtime_ns, size, lines) VALUES(?,?,?,?,?,?)",
            (repo.name, rel, kind, mtime_ns, size, lines)).lastrowid
    else:
        _delete_file_chunks(conn, file_id)
        conn.execute("UPDATE files SET kind=?, mtime_ns=?, size=?, lines=? WHERE id=?",
                     (kind, mtime_ns, size, lines, file_id))
    meta = path_meta(rel)
    fts_path = fold(f"{repo.name}/{rel} {meta.label}")
    for ch in chunks:
        proc = "" if ch.kind == "object" else ch.name     # у объекта name — «Имя Синоним», только для поиска
        rowid = conn.execute(
            "INSERT INTO chunks(file_id, kind, object_type, object_name, object_lower, module_kind, sub_name,"
            " proc_name, proc_lower, is_export, line_start, line_end, text) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (file_id, ch.kind, meta.object_type, meta.object_name, meta.object_name.lower(), meta.module_kind,
             meta.sub_name, proc, proc.lower(), int(ch.is_export), ch.line_start, ch.line_end, ch.text),
        ).lastrowid
        conn.execute("INSERT INTO chunks_fts(rowid, path, name, doc, body) VALUES(?,?,?,?,?)",
                     (rowid, fts_path, fold(ch.name), fold(ch.doc), fold(ch.body)))
    return len(chunks)


def scan(root: Path) -> dict[str, tuple[str, int, int, str]]:
    """Относительный путь (NFC, через «/») → (путь для открытия, mtime_ns, size, kind)."""
    found: dict[str, tuple[str, int, int, str]] = {}
    stack = [(fs_path(root), "")]
    while stack:
        directory, rel = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            log.warning("не читается каталог %s: %s", rel or ".", exc)
            continue
        for e in entries:
            if e.is_dir(follow_symlinks=False):
                if not e.name.startswith("."):
                    stack.append((e.path, rel + e.name + "/"))
                continue
            path = unicodedata.normalize("NFC", rel + e.name)
            low = e.name.lower()
            if low.endswith(".bsl"):
                kind = "bsl"
            elif low.endswith((".xml", ".mdo")) and is_object_file(path):
                kind = "xml"
            else:
                continue
            st = e.stat()
            found[path] = (e.path, st.st_mtime_ns, st.st_size, kind)
    return found


def git_pull(repo: Repo) -> None:
    if not Path(fs_path(repo.path / ".git")).exists():
        return
    try:
        r = subprocess.run(
            ["git", "-c", "core.longpaths=true", "-c", "core.quotepath=false", "-C", str(repo.path),
             "pull", "--ff-only", "--quiet"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0:
            log.warning("git pull (%s) не выполнен: %s", repo.name, (r.stderr or r.stdout).strip()[:500])
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("git pull (%s) не выполнен: %s", repo.name, exc)


def index_repo(conn: sqlite3.Connection, repo: Repo, full: bool = False) -> dict:
    if not Path(fs_path(repo.path)).is_dir():
        # Пропавший каталог (отвалился диск, не доехал clone) — не повод стирать индекс.
        log.error("каталог выгрузки «%s» не найден: %s — индекс этого репозитория не тронут", repo.name, repo.path)
        return {"repo": repo.name, "error": "каталог не найден"}
    found = scan(repo.path)
    known = {path: (fid, mtime, size) for fid, path, mtime, size in
             conn.execute("SELECT id, path, mtime_ns, size FROM files WHERE repo=?", (repo.name,))}
    indexed = removed = failed = 0
    for n, (rel, (fsp, mtime_ns, size, kind)) in enumerate(sorted(found.items()), 1):
        old = known.get(rel)
        if old and not full and old[1] == mtime_ns and old[2] == size:
            continue
        try:
            _index_file(conn, repo, rel, fsp, mtime_ns, size, kind, old[0] if old else None)
            indexed += 1
        except (OSError, sqlite3.Error) as exc:
            failed += 1
            log.warning("файл пропущен %s/%s: %s", repo.name, rel, exc)
        if indexed and indexed % COMMIT_EVERY_FILES == 0:
            conn.commit()
    for rel, (fid, _, _) in known.items():
        if rel not in found:
            _delete_file_chunks(conn, fid)
            conn.execute("DELETE FROM files WHERE id=?", (fid,))
            removed += 1
    conn.commit()
    return {"repo": repo.name, "files": len(found), "indexed": indexed, "removed": removed, "failed": failed}


def stats(conn: sqlite3.Connection, index_path: Path) -> dict:
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    size = sum(p.stat().st_size for p in (index_path, Path(str(index_path) + "-wal")) if p.exists())
    return {
        "files_bsl": one("SELECT count(*) FROM files WHERE kind='bsl'"),
        "files_xml": one("SELECT count(*) FROM files WHERE kind='xml'"),
        "chunks": one("SELECT count(*) FROM chunks"),
        "procedures": one("SELECT count(*) FROM chunks WHERE kind IN ('proc','func')"),
        "objects": one("SELECT count(*) FROM chunks WHERE kind='object'"),
        "vectors": emb.vector_count(conn),
        "index_mb": round(size / 1024 / 1024, 1),
        "last_run": (conn.execute("SELECT value FROM meta WHERE key='last_run'").fetchone() or [None])[0],
    }


def run(cfg: Config, *, full: bool = False, pull: bool = True) -> dict:
    started = time.monotonic()
    conn = connect_rw(cfg.index_path)
    try:
        was_empty = conn.execute("SELECT count(*) FROM files").fetchone()[0] == 0
        if full:
            conn.execute("DELETE FROM chunks_fts")
            conn.execute("DELETE FROM chunks")
            conn.execute("DELETE FROM files")
            conn.commit()
        repos = []
        for repo in cfg.repos:
            if pull and repo.git_pull:
                git_pull(repo)
            repos.append(index_repo(conn, repo, full=full))
        names = [r.name for r in cfg.repos]
        marks = ",".join("?" * len(names)) or "''"
        stale = [r[0] for r in conn.execute(f"SELECT id FROM files WHERE repo NOT IN ({marks})", names)]
        for fid in stale:                  # репозиторий убрали из конфига
            _delete_file_chunks(conn, fid)
            conn.execute("DELETE FROM files WHERE id=?", (fid,))
        changed = sum(r.get("indexed", 0) + r.get("removed", 0) for r in repos) + len(stale)
        if full or was_empty:
            conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('optimize')")
        conn.execute("INSERT OR REPLACE INTO meta VALUES('last_run', ?)",
                     (datetime.now().isoformat(timespec="seconds"),))
        conn.commit()
        semantic = None
        if cfg.semantic["enabled"]:
            # Поиск по словам уже обновлён и доступен боту; векторы досчитываются следом.
            try:
                embedder = emb.Embedder(cfg.semantic["model_dir"], int(cfg.semantic["max_tokens"]),
                                        int(cfg.semantic["threads"]))
                semantic = emb.embed_missing(conn, embedder, int(cfg.semantic["batch_size"]))
            except emb.Unavailable as exc:
                log.warning("смысловой поиск не обновлён: %s", exc)
                semantic = {"error": str(exc)}
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        result = {"repos": repos, "changed": changed, "semantic": semantic, **stats(conn, cfg.index_path),
                  "seconds": round(time.monotonic() - started, 1)}
    finally:
        conn.close()
    return result


def _setup_logging(cfg: Config | None) -> None:
    # Под pythonw (Планировщик задач, без консоли) sys.stderr отсутствует — пишем только в файл.
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)] if sys.stderr is not None else []
    if cfg and cfg.log_path:
        cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(cfg.log_path, maxBytes=1_000_000, backupCount=2, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO if handlers else logging.CRITICAL, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):   # pythonw: потоков нет
            stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Индексатор кода 1С (SQLite FTS5)")
    ap.add_argument("--config", help=f"путь к config.json (по умолчанию {DEFAULT_CONFIG.name} рядом со скриптом)")
    ap.add_argument("--full", action="store_true", help="перестроить индекс с нуля")
    ap.add_argument("--no-pull", action="store_true", help="не делать git pull")
    ap.add_argument("--stats", action="store_true", help="показать состояние индекса и выйти")
    ap.add_argument("--download-model", action="store_true", help="скачать модель смыслового поиска и выйти")
    args = ap.parse_args(argv)
    try:
        cfg = load_config(config_path(args.config))
    except (OSError, ValueError, KeyError) as exc:
        _setup_logging(None)
        log.error("конфиг не прочитан: %s", exc)
        return 2
    _setup_logging(cfg)
    if args.download_model:
        try:
            emb.download_model(cfg.semantic["model_dir"], cfg.semantic["model_url"])
        except OSError as exc:
            log.error("модель не скачана: %s. Можно скопировать каталог модели вручную: %s", exc,
                      cfg.semantic["model_dir"])
            return 1
        return 0
    if args.stats:
        conn = connect_rw(cfg.index_path)
        try:
            result = stats(conn, cfg.index_path)
        finally:
            conn.close()
    else:
        try:
            result = run(cfg, full=args.full, pull=not args.no_pull)
        except sqlite3.OperationalError as exc:
            log.error("индекс занят или недоступен: %s", exc)
            return 1
        log.info("готово: %s", json.dumps(result, ensure_ascii=False))
    if sys.stdout is not None:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if any("error" in r for r in result.get("repos", [])) else 0


if __name__ == "__main__":
    sys.exit(main())
