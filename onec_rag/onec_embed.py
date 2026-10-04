"""Смысловой слой поиска по коду 1С: эмбеддинги в том же файле SQLite (sqlite-vec).

Необязательная часть. Включается в config.json (`semantic.enabled: true`) и
требует пакетов из requirements-semantic.txt: onnxruntime, tokenizers,
sqlite-vec, numpy. Модель (multilingual-e5-small, ONNX, 118 МБ) лежит на
диске и считается на CPU — код конфигурации никуда не отправляется.

    python onec_rag/onec_index.py --download-model     # один раз скачать модель

Если пакетов или модели нет, индексатор и MCP-сервер работают как раньше —
только с поиском по словам.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("onec_index")

TABLE = "chunks_vec"
DEFAULTS = {
    "enabled": False,
    "model_dir": "models/multilingual-e5-small",
    "model_url": "https://huggingface.co/Xenova/multilingual-e5-small/resolve/main",
    "max_tokens": 256,
    "batch_size": 32,
    "threads": 0,          # 0 — все ядра; на общем сервере можно ограничить
    "top_k": 50,           # сколько ближайших по смыслу брать в слияние
    "weight": 0.6,         # вес смыслового списка относительно поиска по словам
}
# Файл в каталоге модели → путь в репозитории модели.
MODEL_FILES = {"model.onnx": "onnx/model_quantized.onnx", "tokenizer.json": "tokenizer.json"}

_CAMEL_RE = re.compile(r"[А-ЯЁA-Z]+(?![а-яёa-z])|[А-ЯЁA-Z]?[а-яёa-z]+|\d+")


class Unavailable(Exception):
    """Смысловой поиск включён, но работать не может (нет пакета, модели, расширения)."""


def camel_words(name: str) -> str:
    """ЗначениеРеквизитаОбъекта → «значение реквизита объекта»: модель понимает слова, а не слитные имена."""
    return " ".join(w.lower() for w in _CAMEL_RE.findall(name))


def download_model(model_dir: Path, base_url: str) -> None:
    model_dir.mkdir(parents=True, exist_ok=True)
    for name, remote in MODEL_FILES.items():
        target = model_dir / name
        if target.exists() and target.stat().st_size > 0:
            log.info("модель: %s уже есть", name)
            continue
        url = f"{base_url.rstrip('/')}/{remote}"
        log.info("модель: скачиваю %s", url)
        tmp = target.with_suffix(target.suffix + ".part")
        with urllib.request.urlopen(url, timeout=120) as response, open(tmp, "wb") as out:
            while True:
                block = response.read(1 << 20)
                if not block:
                    break
                out.write(block)
        tmp.replace(target)
        log.info("модель: %s — %.0f МБ", name, target.stat().st_size / 1e6)


def load_vec(conn: sqlite3.Connection) -> None:
    """Подключить расширение sqlite-vec к соединению."""
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except (ImportError, AttributeError, sqlite3.Error) as exc:
        raise Unavailable(f"расширение sqlite-vec не загружено: {exc}") from None


def has_vectors(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone() is not None


def vector_count(conn: sqlite3.Connection) -> int:
    """Число векторов — через служебную таблицу, расширение для этого не нужно."""
    if not has_vectors(conn):
        return 0
    return conn.execute(f"SELECT count(*) FROM {TABLE}_rowids").fetchone()[0]


class Embedder:
    """multilingual-e5: префиксы «query: » / «passage: », усреднение по токенам, нормировка."""

    def __init__(self, model_dir: Path, max_tokens: int = 256, threads: int = 0):
        try:
            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise Unavailable(f"не установлен пакет {exc.name} (pip install -r requirements-semantic.txt)") from None
        model, tok = model_dir / "model.onnx", model_dir / "tokenizer.json"
        if not model.exists() or not tok.exists():
            raise Unavailable(f"нет файлов модели в {model_dir} (onec_index.py --download-model)")
        self.np = np
        options = ort.SessionOptions()
        if threads:
            options.intra_op_num_threads = threads
        options.log_severity_level = 3
        self.session = ort.InferenceSession(str(model), options, providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.session.get_inputs()}
        self.tokenizer = Tokenizer.from_file(str(tok))
        self.tokenizer.enable_truncation(max_length=max_tokens)
        self.tokenizer.enable_padding()
        self.dim = self.session.get_outputs()[0].shape[-1]

    def embed(self, texts: list[str], prefix: str):
        np = self.np
        enc = self.tokenizer.encode_batch([prefix + t for t in texts])
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.inputs:
            feed["token_type_ids"] = np.zeros_like(ids)
        hidden = self.session.run(None, feed)[0]
        m = mask[:, :, None].astype(np.float32)
        pooled = (hidden * m).sum(axis=1) / np.clip(m.sum(axis=1), 1e-9, None)
        pooled /= np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-9, None)
        return pooled.astype(np.float32)

    def query(self, text: str) -> bytes:
        return self.embed([text], "query: ")[0].tobytes()


def passage_text(kind: str, object_type: str, object_name: str, module_kind: str, sub_name: str,
                 proc_name: str, doc: str, body: str, text: str | None) -> str:
    """Что именно превращаем в вектор: вид и имя объекта, имя процедуры словами,
    её описание. Тело целиком в модель не влезает и смысл размывает; начало тела
    добавляем, только когда описания нет."""
    owner = f"{object_type} {camel_words(object_name)}".strip()
    if kind == "object":
        return f"{owner}. {text or body}"[:1200]
    head = f"{owner}. {module_kind} {camel_words(sub_name)}".strip()
    if kind == "module":
        return f"{head}. {body[:600]}"
    what = "функция" if kind == "func" else "процедура"
    out = f"{head}. {what} {camel_words(proc_name)}. {doc[:700]}"
    if len(doc) < 80:
        code = "\n".join(line for line in body.split("\n") if not line.lstrip().startswith("//"))
        out += " " + code[:400]
    return out


def embed_missing(conn: sqlite3.Connection, embedder: Embedder, batch_size: int = 32) -> dict:
    """Посчитать векторы для чанков, у которых их ещё нет, и убрать векторы удалённых
    чанков. Работает порциями с фиксацией — прерванный прогон продолжится со следующего."""
    started = time.monotonic()
    load_vec(conn)
    conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS {TABLE} USING vec0(embedding float[{embedder.dim}])")
    removed = conn.execute(f"DELETE FROM {TABLE} WHERE rowid NOT IN (SELECT id FROM chunks)").rowcount
    rows = conn.execute(
        "SELECT c.id, c.kind, c.object_type, c.object_name, c.module_kind, c.sub_name, c.proc_name, "
        "chunks_fts.doc, chunks_fts.body, c.text FROM chunks c JOIN chunks_fts ON chunks_fts.rowid=c.id "
        f"WHERE c.id NOT IN (SELECT rowid FROM {TABLE}_rowids)").fetchall()
    items = sorted(((r[0], passage_text(*r[1:])) for r in rows), key=lambda x: len(x[1]))  # близкие по длине — вместе
    done = 0
    for start in range(0, len(items), batch_size):
        batch = items[start:start + batch_size]
        vectors = embedder.embed([t for _, t in batch], "passage: ")
        conn.executemany(f"INSERT INTO {TABLE}(rowid, embedding) VALUES(?, ?)",
                         [(rowid, vec.tobytes()) for (rowid, _), vec in zip(batch, vectors)])
        done += len(batch)
        if (start // batch_size) % 50 == 49:
            conn.commit()
            log.info("векторы: %d из %d", done, len(items))
    conn.commit()
    return {"embedded": done, "removed": max(removed, 0), "seconds": round(time.monotonic() - started, 1)}


def knn(conn: sqlite3.Connection, vector: bytes, k: int) -> list[int]:
    """Идентификаторы k ближайших чанков, от ближнего к дальнему."""
    return [r[0] for r in conn.execute(
        f"SELECT rowid FROM {TABLE} WHERE embedding MATCH ? AND k = ? ORDER BY distance", (vector, k))]
