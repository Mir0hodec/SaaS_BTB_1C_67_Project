"""MCP-сервер (stdio): поиск по коду конфигурации 1С. Только чтение.

Запускается самим Claude Code на время вопроса — постоянного процесса нет.
Индекс строит onec_index.py; здесь он открывается с mode=ro. Конфиг —
config.json рядом со скриптом или путь в переменной ONEC_RAG_CONFIG.

Инструменты: search_1c, get_module, find_object. Каждый результат — со
ссылкой вида `репозиторий/путь:строки`.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import onec_index as ix  # noqa: E402

# --- обработка запроса ---

_WORD_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё_]+")
_CYR_RE = re.compile(r"[а-яё]")
_STOPWORDS = set("""
где как что чем кто когда почему зачем куда откуда какая какой какие какое каком какую каким
это этот эта эти или для при над под без про через если чтобы есть был была были быть будет
такое такой так тоже также все всех его нее них она они оно мне нам вам
делает делают делается происходит находится нужно можно надо называется
процедура процедуры процедуре процедуру процедурой функция функции функцию функцией
метод метода методе методы модуль модуля модуле модули код кода коде
the and for what where how does this that with from
""".split())

# Окончания — от длинных к коротким; основа остаётся не короче 4 букв (у коротких слов — 3).
_ENDINGS = sorted(set("""
иями ями ами иях ием ией иям ии ого его ому ему ыми ими ая яя ое ее ые ие ый ий ой ую юю ых их ым им ом ем
ов ев ей ия ию ью ья ье ам ям ах ях
ается яется аются яются ается ится ются утся атся ятся ться тся
ение ения ению ением ении ений ание ания анию анием ании аний
ает яет ают яют ует уют ить ать ять еть ыть уть ет ит ют ут ят ат ал ил ел ыл ала ила ели али или ыли ть ти
а я о е ы и у ю ь й
""".split()), key=len, reverse=True)

# Частые расхождения «как спрашивают» ↔ «как названо в коде». Дополняется в config.json (search.synonyms).
_SYNONYMS = {
    "счит": ["расчет", "рассчит", "расчит"],
    "расчет": ["рассчит", "расчит"],
    "рассчит": ["расчет"],
    "провод": ["проведен", "провест"],
    "проведен": ["провест", "провод"],
    "печат": ["печатн"],
    "созда": ["создан", "новый"],
    "удал": ["удален"],
    "запис": ["записат"],
    "откры": ["открыт"],
    "остатк": ["остаток"],
}


def _is_identifier(word: str) -> bool:
    """ЗначениеРеквизитаОбъекта, Module_1, ERP2 — имя из кода: ищем как есть."""
    return "_" in word or any(c.isdigit() for c in word) or any(c.isupper() for c in word[1:])


def stem(word: str) -> str:
    w = ix.fold(word).lower()
    if not _CYR_RE.search(w):
        return w
    for min_stem in (4, 3):                    # «печать» → «печа», но «пеня» → «пен», «цены» → «цен»
        for ending in _ENDINGS:
            if w.endswith(ending) and len(w) - len(ending) >= min_stem:
                return w[:-len(ending)]
    return w


@dataclass
class Term:
    variants: list[str]        # первая — основа слова, остальные — синонимы
    exact: bool                # имя из кода, без стемминга


def parse_query(query: str, synonyms: dict[str, list[str]] | None = None) -> list[Term]:
    syn = {**_SYNONYMS, **(synonyms or {})}
    terms: list[Term] = []
    seen: set[str] = set()
    for word in _WORD_RE.findall(query):
        low = ix.fold(word).lower()
        if low in _STOPWORDS:
            continue
        exact = _is_identifier(word)
        base = low if exact else stem(word)
        if len(base) < 3 or base in seen:      # trigram не ищет короче трёх символов
            continue
        seen.add(base)
        variants = [base] + ([] if exact else [ix.fold(v).lower() for v in syn.get(base, []) if len(v) >= 3])
        terms.append(Term(list(dict.fromkeys(variants)), exact))
    return terms


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def fts_expr(terms: list[Term], operator: str = "OR") -> str:
    groups = ["(" + " OR ".join(_quote(v) for v in t.variants) + ")" for t in terms]
    return f" {operator} ".join(groups)


# --- поиск ---

@dataclass
class Hit:
    repo: str
    path: str
    kind: str
    object_type: str
    object_name: str
    module_kind: str
    sub_name: str
    proc_name: str
    is_export: bool
    line_start: int
    line_end: int
    body: str
    score: float

    @property
    def link(self) -> str:
        return f"{self.repo}/{self.path}:{self.line_start}-{self.line_end}"

    @property
    def title(self) -> str:
        owner = f"{self.object_type}.{self.object_name}" if self.object_name else self.object_type
        what = {"proc": "Процедура", "func": "Функция", "module": "код вне процедур",
                "object": "структура объекта"}[self.kind]
        if self.kind in ("proc", "func"):
            what += f" {self.proc_name}" + (" (Экспорт)" if self.is_export else "")
        parts = [owner, self.module_kind + (f" {self.sub_name}" if self.sub_name else ""), what]
        return " · ".join(p for p in parts if p)


_SELECT = """
SELECT f.repo, f.path, c.kind, c.object_type, c.object_name, c.module_kind, c.sub_name, c.proc_name,
       c.is_export, c.line_start, c.line_end, chunks_fts.body, chunks_fts.name, chunks_fts.path,
       chunks_fts.doc, bm25(chunks_fts, ?, ?, ?, ?) AS rank
FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid JOIN files f ON f.id = c.file_id
WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?
"""

# Где нашлось слово запроса: в имени процедуры/объекта ценнее, чем в теле.
_FIELD_WEIGHTS = (("name", 1.0), ("doc", 0.7), ("path", 0.6), ("body", 0.35))
# Вопрос про состав объекта, а не про код: поднимаем чанки структуры.
_STRUCTURE_STEMS = {"реквизит", "состав", "структур", "табличн", "измерен", "ресурс", "синоним",
                    "регламентн", "подписк", "движен", "колонк", "поля", "пол"}


def _match_quality(text: str, low: str, variants: list[str]) -> float:
    """1.0 — слово начинается с основы (начало слова или часть ИмениВВерблюжьейЗаписи);
    0.4 — основа нашлась в середине слова («рол» в «контроль»)."""
    best = 0.0
    for v in variants:
        i, tries = low.find(v), 0
        while i != -1 and tries < 40:
            if i == 0 or text[i].isupper() or not text[i - 1].isalpha():
                return 1.0
            best = 0.4
            i, tries = low.find(v, i + 1), tries + 1
    return best


def search_chunks(conn: sqlite3.Connection, query: str, limit: int, search_cfg: dict | None = None) -> tuple[list[Hit], list[Term]]:
    """Кандидаты — bm25 по OR и по AND, по всему чанку и отдельно по имени с описанием;
    затем пересортировка: сколько слов запроса покрыто, где они нашлись (имя,
    описание, путь, тело) и начинается ли с них слово."""
    cfg = {**ix.SEARCH_DEFAULTS, **(search_cfg or {})}
    w = {**ix.SEARCH_DEFAULTS["weights"], **(cfg.get("weights") or {})}
    terms = parse_query(query, cfg.get("synonyms"))
    if not terms:
        return [], terms
    weights = (float(w["path"]), float(w["name"]), float(w["doc"]), float(w["body"]))
    rows: dict[tuple, tuple] = {}
    # Отдельно ищем по имени и описанию процедуры: на частых словах («роль», «дата») общий
    # bm25 забит телами процедур, а отвечает на вопрос обычно то, что так названо или описано.
    head = "{name doc} : "
    exprs = [(fts_expr(terms, "OR"), int(cfg["candidates"])), (head + "(" + fts_expr(terms, "OR") + ")", 200)]
    if len(terms) > 1:
        exprs += [(fts_expr(terms, "AND"), 100), (head + "(" + fts_expr(terms, "AND") + ")", 150)]
    for expr, n in exprs:
        for row in conn.execute(_SELECT, (*weights, expr, n)):
            rows.setdefault((row[0], row[1], row[9], row[2]), row)

    idents = {t.variants[0] for t in terms if t.exact}
    words = {ix.fold(x).lower() for x in _WORD_RE.findall(query)}
    wants_structure = any(t.variants[0] in _STRUCTURE_STEMS for t in terms)

    n = len(terms)
    hits: list[Hit] = []
    for row in rows.values():
        fields = {"name": row[12], "doc": row[14], "path": row[13], "body": row[11]}
        lows = {k: v.lower() for k, v in fields.items()}
        coverage = matched = in_name = 0.0
        for t in terms:
            best = max(fw * _match_quality(fields[f], lows[f], t.variants) for f, fw in _FIELD_WEIGHTS)
            coverage += best
            matched += best > 0
            in_name += _match_quality(fields["name"], lows["name"], t.variants) == 1.0
        # bm25 × покрытие слов запроса × доля найденных слов × доля слов, попавших в имя.
        score = -row[15] * (0.25 + coverage / n) ** 2 * (0.5 + matched / n) * (1 + in_name / n) ** 2
        kind, proc_l, object_l = row[2], row[7].lower(), row[4].lower()
        if proc_l and proc_l in idents:            # в вопросе точное имя процедуры
            score *= 4
        if object_l and object_l in words:         # и точное имя объекта или модуля
            score *= 2.5
        if kind == "object":
            score *= 2.5 if wants_structure else 0.6
        elif row[8]:                               # экспортные — программный интерфейс, о нём и спрашивают
            score *= 1.3
        hits.append(Hit(row[0], row[1], kind, row[3], row[4], row[5], row[6], row[7], bool(row[8]),
                        row[9], row[10], row[11], score))
    hits.sort(key=lambda h: -h.score)
    return hits[:limit], terms


def snippet(hit: Hit, terms: list[Term], max_lines: int = 6, width: int = 160) -> list[str]:
    """Заголовок процедуры и строки с совпадениями — с настоящими номерами строк."""
    lines = hit.body.split("\n")
    if hit.kind == "object":
        return [line[:width * 3] for line in lines[:max_lines]]
    variants = [v for t in terms for v in t.variants]
    picked: list[int] = []
    header = next((i for i, line in enumerate(lines) if ix.PROC_START.match(line)), None)
    if header is not None:
        picked.append(header)
    for i, line in enumerate(lines):
        if len(picked) >= max_lines:
            break
        low = line.lower()
        if i not in picked and any(v in low for v in variants):
            picked.append(i)
    if not picked:
        picked = list(range(min(len(lines), 3)))
    return [f"{hit.line_start + i}: {lines[i].strip()[:width]}" for i in sorted(picked)]


def format_hits(hits: list[Hit], terms: list[Term], max_chars: int) -> str:
    if not terms:
        return "В запросе нет слов для поиска (нужны слова от 3 букв: имя объекта, процедуры или описание)."
    words = " | ".join(t.variants[0] for t in terms)
    if not hits:
        return (f"Ничего не найдено (искал: {words}). Попробуй другие слова, имя объекта или процедуры. "
                "Если не находится — так и скажи, не выдумывай.")
    out = [f"Найдено: {len(hits)} (искал: {words}). Перед ответом прочитай код через get_module."]
    used = len(out[0])
    for n, hit in enumerate(hits, 1):
        block = "\n".join([f"{n}. {hit.link}", f"   {hit.title}", *("   " + s for s in snippet(hit, terms))])
        if used + len(block) > max_chars:
            out.append(f"… вывод обрезан: показано {n - 1} из {len(hits)}. Уточни запрос.")
            break
        out.append(block)
        used += len(block) + 1
    return "\n".join(out)


# --- чтение модулей и объектов ---

class NotFound(Exception):
    pass


def open_ro(index_path: Path) -> sqlite3.Connection:
    if not index_path.exists():
        raise NotFound("Индекс кода 1С ещё не построен (запустите onec_index.py).")
    conn = sqlite3.connect(index_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=15)
    conn.execute("PRAGMA query_only=1")
    return conn


_LINK_TAIL = re.compile(r":(\d+)(?:-(\d+))?$")
_TYPE_ALIASES = {ru.lower(): ru for ru in ix.TYPE_FOLDERS.values()}
_TYPE_ALIASES.update({folder.lower(): ru for folder, ru in ix.TYPE_FOLDERS.items()})
_TYPE_ALIASES.update({single.lower(): ru for single, ru in ix.KIND_RU.items()})
_TYPE_ALIASES.update({"документы": "Документ", "справочники": "Справочник", "отчеты": "Отчет",
                      "обработки": "Обработка", "перечисления": "Перечисление",
                      "регистрысведений": "РегистрСведений", "регистрынакопления": "РегистрНакопления",
                      "общиемодули": "ОбщийМодуль", "константы": "Константа"})


def split_object_name(name: str) -> tuple[str | None, str]:
    """«Документ.РасходнаяНакладная» → («Документ», «РасходнаяНакладная»)."""
    name = name.strip().strip("«»\"'")
    head, dot, tail = name.partition(".")
    if dot and ix.fold(head).lower() in _TYPE_ALIASES:
        return _TYPE_ALIASES[ix.fold(head).lower()], tail.split(".")[0]
    return None, name


def resolve_module(conn: sqlite3.Connection, cfg: ix.Config, path: str) -> tuple[list[tuple], tuple[int, int] | None]:
    """Файлы модулей по ссылке, пути, хвосту пути или имени объекта."""
    path = path.strip().strip("`'\"").replace("\\", "/")
    lines = None
    m = _LINK_TAIL.search(path)
    if m:
        lines = (int(m.group(1)), int(m.group(2) or m.group(1)))
        path = path[:m.start()]
    path = path.strip("/")
    sql = "SELECT id, repo, path, lines FROM files WHERE kind='bsl' AND "
    head, _, tail = path.partition("/")
    if tail and cfg.repo(head):
        rows = conn.execute(sql + "repo=? AND path=?", (head, tail)).fetchall()
        if rows:
            return rows, lines
    rows = conn.execute(sql + "path=?", (path,)).fetchall()
    if not rows and "/" in path:
        like = "%" + path.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = conn.execute(sql + "path LIKE ? ESCAPE '\\' ORDER BY path LIMIT 20", (like,)).fetchall()
    if not rows:                               # «ОбщегоНазначения», «Документ.РасходнаяНакладная»
        type_, name = split_object_name(path)
        rows = conn.execute(
            "SELECT DISTINCT f.id, f.repo, f.path, f.lines FROM files f JOIN chunks c ON c.file_id=f.id "
            "WHERE f.kind='bsl' AND c.object_lower=? AND (? IS NULL OR c.object_type=?) ORDER BY f.path LIMIT 40",
            (name.lower(), type_, type_)).fetchall()
    return rows, lines


def _numbered(lines: list[str], first: int) -> str:
    return "\n".join(f"{first + i}: {line}" for i, line in enumerate(lines))


def _toc(conn: sqlite3.Connection, file_id: int) -> list[str]:
    rows = conn.execute("SELECT kind, proc_name, is_export, line_start, line_end FROM chunks "
                        "WHERE file_id=? AND kind IN ('proc','func') ORDER BY line_start", (file_id,))
    return [f"{a}-{b}: {'Функция' if kind == 'func' else 'Процедура'} {name}{' Экспорт' if exp else ''}"
            for kind, name, exp, a, b in rows]


def read_module(conn: sqlite3.Connection, cfg: ix.Config, path: str, procedure: str = "",
                start_line: int = 0, max_chars: int | None = None) -> str:
    max_chars = max_chars or int(cfg.search["max_output_chars"])
    rows, link_lines = resolve_module(conn, cfg, path)
    if not rows:
        raise NotFound(f"Модуль «{path}» не найден в индексе. Найди его через search_1c или find_object.")
    procedure = procedure.strip()
    if len(rows) > 1 and procedure:            # процедура есть только в одном из модулей объекта
        ids = [r[0] for r in rows]
        marks = ",".join("?" * len(ids))
        with_proc = {r[0] for r in conn.execute(
            f"SELECT file_id FROM chunks WHERE proc_lower=? AND file_id IN ({marks})", (procedure.lower(), *ids))}
        rows = [r for r in rows if r[0] in with_proc] or rows
    if len(rows) > 1:
        return ("Подходит несколько модулей — укажи путь точнее:\n"
                + "\n".join(f"{repo}/{p}:1-{n}" for _, repo, p, n in rows))
    file_id, repo_name, rel, total = rows[0]
    repo = cfg.repo(repo_name)
    if repo is None:
        raise NotFound(f"Репозиторий «{repo_name}» убран из конфига.")
    try:
        lines = ix.read_text(repo.path / rel).split("\n")
    except OSError:
        raise NotFound(f"Файл {repo_name}/{rel} есть в индексе, но не читается с диска.") from None
    base = f"{repo_name}/{rel}"

    if procedure:
        found = conn.execute("SELECT line_start, line_end FROM chunks WHERE file_id=? AND proc_lower=? "
                             "ORDER BY line_start", (file_id, procedure.lower())).fetchall()
        if not found:
            toc = _toc(conn, file_id)
            near = [t for t in toc if procedure.lower() in t.lower()] or toc
            return (f"В модуле {base} нет процедуры «{procedure}». Есть:\n" + "\n".join(near[:200]))
        a, b = found[0]
        text = _numbered(lines[a - 1:b], a)
        if len(text) > max_chars:
            cut = text[:max_chars].rsplit("\n", 1)[0]
            shown = a + cut.count("\n")
            return (f"{base}:{a}-{b} (показано до строки {shown}; дальше — get_module с start_line={shown + 1})\n"
                    + cut)
        return f"{base}:{a}-{b}\n{text}"

    a = max(1, start_line or (link_lines[0] if link_lines else 1))
    b = link_lines[1] if link_lines and not start_line else len(lines)
    text = _numbered(lines[a - 1:b], a)
    if len(text) <= max_chars:
        return f"{base}:{a}-{min(b, len(lines))}\n{text}"
    if a == 1 and not link_lines:
        # Большой модуль целиком не отдаём: оглавление и подсказка, как читать по частям.
        toc = "\n".join(_toc(conn, file_id))
        if len(toc) > max_chars:
            toc = toc[:max_chars].rsplit("\n", 1)[0] + "\n… оглавление обрезано"
        return (f"{base}:1-{len(lines)} — модуль большой ({len(lines)} строк). Оглавление (строки: процедура); "
                f"читай нужное: get_module(path, procedure=\"Имя\") или start_line.\n{toc}")
    cut = text[:max_chars].rsplit("\n", 1)[0]
    shown = a + cut.count("\n")
    return f"{base}:{a}-{shown} (дальше — get_module с start_line={shown + 1})\n{cut}"


def describe_object(conn: sqlite3.Connection, name: str, max_chars: int) -> str:
    type_, bare = split_object_name(name)
    low = ix.fold(bare).lower()
    sql = ("SELECT c.object_type, c.object_name, c.text, f.repo, f.path, c.line_end FROM chunks c "
           "JOIN files f ON f.id=c.file_id WHERE c.kind='object' AND (? IS NULL OR c.object_type=?) AND ")
    rows = conn.execute(sql + "c.object_lower=? ORDER BY c.object_type LIMIT 5", (type_, type_, bare.lower())).fetchall()
    if not rows and len(low) >= 3:             # часть имени или синоним («расходная накладная»)
        words = [w for w in _WORD_RE.findall(low) if len(w) >= 3]
        expr = "name : (" + " AND ".join(_quote(stem(w)) for w in words) + ")" if words else ""
        if expr:
            rows = conn.execute(
                "SELECT c.object_type, c.object_name, c.text, f.repo, f.path, c.line_end FROM chunks_fts "
                "JOIN chunks c ON c.id=chunks_fts.rowid JOIN files f ON f.id=c.file_id "
                "WHERE chunks_fts MATCH ? AND c.kind='object' AND (? IS NULL OR c.object_type=?) "
                "ORDER BY length(c.object_name) LIMIT 5", (expr, type_, type_)).fetchall()
    blocks: list[str] = []
    owners: list[tuple[str | None, str, str]] = [(r[0], r[1], r[3]) for r in rows]
    for otype, oname, text, repo, path, end in rows:
        blocks.append(f"{repo}/{path}:1-{end}\n{text}")
    if not rows:
        owners = [(type_, bare, "")]
    modules: list[str] = []
    for otype, oname, repo in owners:
        for r in conn.execute(
                "SELECT f.repo, f.path, f.lines, c.module_kind, c.sub_name, c.object_type, "
                "sum(c.kind IN ('proc','func')) FROM chunks c JOIN files f ON f.id=c.file_id "
                "WHERE f.kind='bsl' AND c.object_lower=? AND (? IS NULL OR c.object_type=?) "
                "AND (?='' OR f.repo=?) GROUP BY f.id ORDER BY f.path LIMIT 60",
                (oname.lower(), otype, otype, repo, repo)):
            sub = f" {r[4]}" if r[4] else ""
            modules.append(f"{r[0]}/{r[1]}:1-{r[2]} — {r[5]}.{oname} · {r[3]}{sub}, процедур: {r[6]}")
    if not blocks and not modules:
        raise NotFound(f"Объект «{name}» не найден. Попробуй search_1c — возможно, имя пишется иначе.")
    if not blocks:
        blocks.append(f"Описания структуры «{name}» в индексе нет (в выгрузке нет XML объекта), есть только модули.")
    if modules:
        blocks.append("Модули:\n" + "\n".join(dict.fromkeys(modules)))
    text = "\n\n".join(blocks)
    return text if len(text) <= max_chars else text[:max_chars].rsplit("\n", 1)[0] + "\n… вывод обрезан"


# --- MCP ---

def build_server():
    try:
        from mcp.server.fastmcp import FastMCP
        from mcp.server.fastmcp.exceptions import ToolError
    except ImportError:                        # mcp 2.x: FastMCP переименован в MCPServer
        from mcp.server.mcpserver import MCPServer as FastMCP
        from mcp.server.mcpserver.exceptions import ToolError

    mcp = FastMCP(name="onec_code", instructions=(
        "Код конфигурации 1С (выгрузка в файлы), только чтение. Сначала search_1c, затем get_module, "
        "чтобы прочитать процедуру или модуль целиком. Отвечай со ссылками вида репозиторий/путь:строки."
    ))

    def call(fn):
        try:
            cfg = ix.load_config()
            conn = open_ro(cfg.index_path)
        except NotFound as exc:
            raise ToolError(str(exc)) from None
        except (OSError, ValueError, KeyError) as exc:
            raise ToolError(f"Конфиг поиска по коду 1С не прочитан: {exc}") from None
        try:
            return fn(conn, cfg)
        except NotFound as exc:
            raise ToolError(str(exc)) from None
        except sqlite3.Error as exc:
            raise ToolError(f"Индекс кода 1С недоступен: {exc}") from None
        finally:
            conn.close()

    @mcp.tool()
    def search_1c(query: str, limit: int = 8) -> str:
        """Поиск по коду конфигурации 1С: процедуры, функции, модули и структура
        объектов. query — слова по-русски («заполнение табличной части товары»),
        имя процедуры, объекта или «ОбщегоНазначения.ЗначениеРеквизитаОбъекта».
        Формы слов и окончания учитываются. Возвращает ссылки путь:строки и
        строки с совпадениями; сам код читай через get_module."""
        def run(conn, cfg):
            n = max(1, min(int(limit or cfg.search["default_limit"]), int(cfg.search["max_limit"])))
            hits, terms = search_chunks(conn, query, n, cfg.search)
            return format_hits(hits, terms, int(cfg.search["max_output_chars"]))
        return call(run)

    @mcp.tool()
    def get_module(path: str, procedure: str = "", start_line: int = 0) -> str:
        """Текст модуля 1С с номерами строк. path — ссылка из search_1c
        (репозиторий/путь или репозиторий/путь:строки), хвост пути или имя объекта
        («ОбщегоНазначения», «Документ.РасходнаяНакладная»). procedure — вернуть
        одну процедуру или функцию по имени. Большой модуль отдаётся оглавлением;
        start_line — читать с этой строки."""
        return call(lambda conn, cfg: read_module(conn, cfg, path, procedure, start_line))

    @mcp.tool()
    def find_object(name: str) -> str:
        """Структура объекта конфигурации: синоним, реквизиты с типами, табличные
        части, формы, команды, движения — и список его модулей. name — имя
        («РасходнаяНакладная»), с видом («Документ.РасходнаяНакладная») или
        синоним («расходная накладная»)."""
        return call(lambda conn, cfg: describe_object(conn, name, int(cfg.search["max_output_chars"])))

    return mcp


if __name__ == "__main__":
    for _stream in (sys.stdin, sys.stdout, sys.stderr):   # кириллица в пайпах Windows
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")
    build_server().run(transport="stdio")
