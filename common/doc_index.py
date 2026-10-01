"""Поисковый индекс базы знаний на SQLite FTS5 с русской стемматизацией.

Индексируются фрагменты (чанки) документов, а не документы целиком: в базе
есть руководства на 60–190 тыс. символов, и совпадение «где-то в документе»
модели ничего не даёт — ей нужен конкретный абзац.

Слова приводятся к основе (snowball, русский/английский), поэтому запрос
«проведение документа» находит «проведения документов». Индекс строится
отдельным скриптом (scripts/build_kb_index.py), MCP-серверы открывают его
только на чтение.
"""
from __future__ import annotations

import fnmatch
import hashlib
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import snowballstemmer

from common.doc_extract import SUPPORTED_EXTENSIONS, extract_text

CHUNK_TARGET_CHARS = 1200
CHUNK_HARD_LIMIT = 2000
MAX_READ_CHUNKS = 10

_WORD_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+")
_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
_RU = snowballstemmer.stemmer("russian")
_EN = snowballstemmer.stemmer("english")

_STOPWORDS_RAW = (
    "как что где когда почему зачем какой какая какие каким каком который которая "
    "в во на и или по для не ни с со а ли это этот эта эти из к ко у о об от до за при "
    "же бы то там тут можно нужно надо есть был была были быть будет мне меня мы вы "
    "он она они его ее их все всех так также если чтобы через после перед под над "
    "the a an of to in on for and or is are how what why"
)


def _stem(word: str) -> str:
    w = word.lower().replace("ё", "е")
    return _RU.stemWord(w) if _CYRILLIC_RE.search(w) else _EN.stemWord(w)


_STOPWORDS = {_stem(w) for w in _STOPWORDS_RAW.split()}


def stem_text(text: str) -> str:
    return " ".join(_stem(w) for w in _WORD_RE.findall(text))


def query_stems(query: str) -> list[str]:
    seen: list[str] = []
    for w in _WORD_RE.findall(query):
        s = _stem(w)
        if len(s) >= 2 and s not in _STOPWORDS and s not in seen:
            seen.append(s)
    return seen


@dataclass
class _Term:
    """Слово запроса: основа (ищется как префикс) + более короткие основы,
    реально встречающиеся в индексе (ищутся точно).

    Snowball не сводит однокоренные слова к одной основе: «обменнике» →
    «обменник», а «Обменный» → «обмен». Префиксный поиск по «обменник» не
    найдёт «обмен», поэтому добавляем самую длинную основу из словаря
    индекса, которая является началом основы запроса."""
    stem: str
    shorter: list[str]

    def matches(self, token: str) -> bool:
        return token.startswith(self.stem) or token in self.shorter

    def fts(self) -> str:
        parts = [f'"{self.stem}"*'] + [f'"{s}"' for s in self.shorter]
        return "(" + " OR ".join(parts) + ")" if len(parts) > 1 else parts[0]


# --- Классификация по продукту ------------------------------------------------

PRODUCT_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("ОСИ/ЖКХ", ("кондоминиум", "жкх", "жильц", "оси", "квитанц", "лицев", "расчетн центр",
                 "расчётн центр", "собственник", "пени", "начислени")),
    ("Обменный пункт", ("обменн", "кассир", "вебкасс", "курс валют", "фрому", "терроризм",
                        "aml", "национальн банк", "отчетов нб", "золот", "табло", "валют")),
]
DEFAULT_PRODUCT = "Общее"


def classify_product(title: str, text: str) -> str:
    haystack = f"{title} {text[:3000]}".lower().replace("ё", "е")
    scores = {name: sum(haystack.count(k) for k in keys) for name, keys in PRODUCT_RULES}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else DEFAULT_PRODUCT


# --- Разбиение на фрагменты ---------------------------------------------------

def chunk_text(text: str) -> list[str]:
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for p in paragraphs:
        for piece in _split_long(p):
            if current and size + len(piece) > CHUNK_TARGET_CHARS:
                chunks.append("\n".join(current))
                current, size = [], 0
            current.append(piece)
            size += len(piece) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def _split_long(paragraph: str) -> list[str]:
    if len(paragraph) <= CHUNK_HARD_LIMIT:
        return [paragraph]
    words, pieces, current = paragraph.split(), [], ""
    for w in words:
        if current and len(current) + len(w) + 1 > CHUNK_TARGET_CHARS:
            pieces.append(current)
            current = w
        else:
            current = f"{current} {w}" if current else w
    if current:
        pieces.append(current)
    return pieces


_TITLE_PREFIX_RE = re.compile(r"^[\s!+.\d]+")
_TITLE_SUFFIX_RE = re.compile(r"(\s*[—-]\s*копия|\s*\(\d+\))+$", re.I)


def title_from_filename(name: str) -> str:
    stem = Path(name).stem
    stem = _TITLE_SUFFIX_RE.sub("", stem)
    cleaned = _TITLE_PREFIX_RE.sub("", stem).strip()
    return cleaned or stem


# --- Результаты ---------------------------------------------------------------

@dataclass
class SearchHit:
    path: str
    title: str
    product: str
    chunk_no: int
    total_chunks: int
    snippet: str
    matched_terms: int
    query_terms: int


@dataclass
class BuildReport:
    indexed: list[str] = field(default_factory=list)
    duplicates: list[tuple[str, str]] = field(default_factory=list)  # (дубль, оставленный файл)
    excluded: list[tuple[str, str]] = field(default_factory=list)    # (файл, причина)
    empty: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)      # (файл, ошибка)
    unsupported: list[str] = field(default_factory=list)
    chunks: int = 0


@dataclass
class ExcludeRule:
    pattern: str
    reason: str


# --- Индекс -------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    aliases TEXT NOT NULL DEFAULT '',
    product TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    chars INTEGER NOT NULL,
    n_chunks INTEGER NOT NULL,
    modified_at TEXT
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    doc_id INTEGER NOT NULL REFERENCES documents(id),
    chunk_no INTEGER NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id, chunk_no);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(title_stem, body_stem);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vocab USING fts5vocab(chunks_fts, 'row');
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class IndexNotBuiltError(RuntimeError):
    pass


class DocIndex:
    def __init__(self, db_path: Path, *, readonly: bool = False):
        self.db_path = db_path
        self.readonly = readonly
        # MCP-фреймворк вызывает синхронные инструменты из пула потоков —
        # одно соединение на всех, доступ сериализован локом.
        self._lock = threading.Lock()
        if readonly:
            if not db_path.exists():
                raise IndexNotBuiltError(
                    f"Индекс {db_path} не построен. Запустите: python scripts/build_kb_index.py"
                )
            self._conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
        else:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- построение ---

    @staticmethod
    def folder_signature(root: Path, exclude: list[ExcludeRule] | None = None) -> str:
        """Отпечаток папки и правил исключения: имена, размеры, даты файлов."""
        h = hashlib.sha1("|".join(r.pattern for r in exclude or []).encode("utf-8"))
        for p in sorted(root.rglob("*")):
            if p.is_file():
                st = p.stat()
                h.update(f"{p.relative_to(root)}|{st.st_size}|{int(st.st_mtime)}\n".encode("utf-8"))
        return h.hexdigest()

    def stored_signature(self) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = 'signature'").fetchone()
        return row[0] if row else None

    def rebuild_if_changed(self, root: Path, exclude: list[ExcludeRule] | None = None) -> BuildReport | None:
        """Переиндексировать, только если папка или исключения изменились."""
        if not root.is_dir() or self.stored_signature() == self.folder_signature(root, exclude):
            return None
        return self.rebuild_from_dir(root, exclude)

    def rebuild_from_dir(self, root: Path, exclude: list[ExcludeRule] | None = None) -> BuildReport:
        """Полная переиндексация в одной транзакции: пока она идёт, уже
        запущенные MCP-серверы продолжают читать прежнюю версию индекса."""
        if self.readonly:
            raise RuntimeError("Индекс открыт только на чтение")
        exclude = exclude or []
        report = BuildReport()
        signature = self.folder_signature(root, exclude)

        by_hash: dict[str, list[Path]] = {}
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            rel = str(path.relative_to(root))
            rule = next((r for r in exclude if fnmatch.fnmatch(path.name.lower(), r.pattern.lower())
                         or fnmatch.fnmatch(rel.lower(), r.pattern.lower())), None)
            if rule:
                report.excluded.append((rel, rule.reason))
                continue
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                report.unsupported.append(rel)
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            by_hash.setdefault(digest, []).append(path)

        prepared = []
        for digest, paths in by_hash.items():
            canonical = min(paths, key=lambda p: ("копия" in p.name.lower(), len(p.name), p.name))
            others = [p for p in paths if p != canonical]
            for o in others:
                report.duplicates.append((str(o.relative_to(root)), str(canonical.relative_to(root))))
            rel = str(canonical.relative_to(root))
            try:
                text = extract_text(canonical)
            except Exception as exc:  # noqa: BLE001 — один битый файл не останавливает сборку
                report.failed.append((rel, f"{type(exc).__name__}: {exc}"))
                continue
            chunks = chunk_text(text)
            if not chunks:
                report.empty.append(rel)
                continue
            title = title_from_filename(canonical.name)
            aliases = sorted({title_from_filename(o.name) for o in others} - {title})
            mtime = datetime.fromtimestamp(canonical.stat().st_mtime, tz=timezone.utc).isoformat()
            prepared.append((rel, title, aliases, classify_product(title, text), digest, len(text), chunks, mtime))

        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN")
            try:
                cur.execute("DELETE FROM chunks_fts")
                cur.execute("DELETE FROM chunks")
                cur.execute("DELETE FROM documents")
                for rel, title, aliases, product, digest, chars, chunks, mtime in sorted(prepared):
                    cur.execute(
                        "INSERT INTO documents (path, title, aliases, product, sha256, chars, n_chunks, modified_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (rel, title, " | ".join(aliases), product, digest, chars, len(chunks), mtime),
                    )
                    doc_id = cur.lastrowid
                    title_stem = stem_text(" ".join([title, *aliases]))
                    for no, chunk in enumerate(chunks):
                        cur.execute("INSERT INTO chunks (doc_id, chunk_no, text) VALUES (?, ?, ?)", (doc_id, no, chunk))
                        cur.execute(
                            "INSERT INTO chunks_fts (rowid, title_stem, body_stem) VALUES (?, ?, ?)",
                            (cur.lastrowid, title_stem, stem_text(chunk)),
                        )
                    report.indexed.append(rel)
                    report.chunks += len(chunks)
                cur.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('built_at', ?)",
                    (datetime.now(timezone.utc).isoformat(),),
                )
                cur.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('signature', ?)", (signature,))
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
        return report

    # --- чтение ---

    def _terms(self, stems: list[str]) -> list[_Term]:
        candidates = {s[:k] for s in stems for k in range(max(4, len(s) - 3), len(s))}
        if not candidates:
            return [_Term(s, []) for s in stems]
        marks = ",".join("?" * len(candidates))
        with self._lock:
            known = {r[0] for r in self._conn.execute(
                f"SELECT term FROM chunks_vocab WHERE term IN ({marks})", tuple(candidates)
            )}
        terms = []
        for s in stems:
            longest = next((s[:k] for k in range(len(s) - 1, max(4, len(s) - 3) - 1, -1) if s[:k] in known), None)
            terms.append(_Term(s, [longest] if longest else []))
        return terms

    def search(self, query: str, limit: int = 8, product: str | None = None) -> list[SearchHit]:
        stems = query_stems(query)
        if not stems:
            return []
        terms = self._terms(stems)
        fts_query = " OR ".join(t.fts() for t in terms)
        sql = (
            "SELECT c.doc_id, c.chunk_no, c.text, d.path, d.title, d.product, d.n_chunks, "
            "       f.title_stem, f.body_stem, bm25(chunks_fts, 4.0, 1.0) AS score "
            "FROM chunks_fts f JOIN chunks c ON c.id = f.rowid JOIN documents d ON d.id = c.doc_id "
            "WHERE chunks_fts MATCH ?"
        )
        params: list = [fts_query]
        if product:
            sql += " AND d.product = ?"
            params.append(product)
        sql += " ORDER BY score LIMIT ?"
        params.append(max(limit * 10, 50))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()

        # Сначала — фрагменты, где нашлось больше разных слов запроса, внутри
        # равных — по bm25. Иначе частое слово («новый», «отчет») вытесняет
        # фрагмент, где есть редкое и главное («кассир»).
        candidates = []
        for doc_id, chunk_no, text, path, title, prod, n_chunks, title_stem, body_stem, score in rows:
            present = set(f"{title_stem} {body_stem}".split())
            matched = sum(1 for t in terms if any(t.matches(tok) for tok in present))
            candidates.append((-matched, score, doc_id, chunk_no, text, path, title, prod, n_chunks, matched))
        candidates.sort(key=lambda c: (c[0], c[1]))

        hits: list[SearchHit] = []
        per_doc: dict[int, int] = {}
        seen_texts: set[str] = set()
        for _neg, _score, doc_id, chunk_no, text, path, title, prod, n_chunks, matched in candidates:
            # Один и тот же абзац встречается в нескольких версиях документа
            # (например, «Руководство … 2024» и «… 2025») — показываем один раз.
            fingerprint = hashlib.sha1(text.encode("utf-8")).hexdigest()
            if per_doc.get(doc_id, 0) >= 2 or fingerprint in seen_texts:
                continue
            per_doc[doc_id] = per_doc.get(doc_id, 0) + 1
            seen_texts.add(fingerprint)
            hits.append(SearchHit(
                path=path, title=title, product=prod, chunk_no=chunk_no, total_chunks=n_chunks,
                snippet=_snippet(text, terms), matched_terms=matched, query_terms=len(terms),
            ))
            if len(hits) >= limit:
                break
        return hits

    def read_document(self, path: str, start_chunk: int = 0, max_chunks: int = 3) -> dict:
        max_chunks = max(1, min(max_chunks, MAX_READ_CHUNKS))
        start_chunk = max(0, start_chunk)
        with self._lock:
            doc = self._conn.execute(
                "SELECT id, title, product, n_chunks FROM documents WHERE path = ?", (path,)
            ).fetchone()
            if doc is None:
                raise KeyError(path)
            rows = self._conn.execute(
                "SELECT chunk_no, text FROM chunks WHERE doc_id = ? AND chunk_no >= ? "
                "ORDER BY chunk_no LIMIT ?",
                (doc[0], start_chunk, max_chunks),
            ).fetchall()
        return {
            "path": path,
            "title": doc[1],
            "product": doc[2],
            "total_chunks": doc[3],
            "chunks": [{"chunk_no": n, "text": t} for n, t in rows],
        }

    def list_documents(self, product: str | None = None) -> list[dict]:
        sql = "SELECT path, title, product, n_chunks FROM documents"
        params: tuple = ()
        if product:
            sql += " WHERE product = ?"
            params = (product,)
        sql += " ORDER BY product, title"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [{"path": p, "title": t, "product": pr, "total_chunks": n} for p, t, pr, n in rows]

    def built_at(self) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = 'built_at'").fetchone()
        return row[0] if row else None


def _snippet(text: str, terms: list[_Term], window_words: int = 45) -> str:
    """Окно текста с наибольшим числом разных совпавших слов запроса; совпадения в [скобках]."""
    words = list(_WORD_RE.finditer(text))
    if not words:
        return text[:300]
    hit_terms = [{i for i, t in enumerate(terms) if t.matches(_stem(m.group()))} for m in words]
    matches = [bool(h) for h in hit_terms]

    best_start, best_score = 0, -1
    for start in range(0, len(words), 5):
        found = set().union(*hit_terms[start:start + window_words])
        if len(found) > best_score:
            best_start, best_score = start, len(found)
    end = min(best_start + window_words, len(words)) - 1

    a, b = words[best_start].start(), words[end].end()
    out, pos = [], a
    for i in range(best_start, end + 1):
        m = words[i]
        out.append(text[pos:m.start()])
        out.append(f"[{m.group()}]" if matches[i] else m.group())
        pos = m.end()
    out.append(text[pos:b])
    prefix = "…" if a > 0 else ""
    suffix = "…" if b < len(text) else ""
    return prefix + "".join(out).replace("\n", " ") + suffix
