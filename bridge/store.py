"""Состояние моста в одной SQLite: сессии, журнал, пауза, лимит, вложения."""
from __future__ import annotations

import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    user_key TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    base_id TEXT,
    last_active TEXT NOT NULL,
    cost_total REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    channel TEXT NOT NULL,
    user_key TEXT NOT NULL,
    user_name TEXT,
    base_id TEXT,
    question TEXT,
    status TEXT NOT NULL,
    answer_chars INTEGER,
    files INTEGER,
    duration_ms INTEGER,
    cost_usd REAL,
    error TEXT
);
CREATE TABLE IF NOT EXISTS seen_messages (message_id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS pending_files (
    id INTEGER PRIMARY KEY, user_key TEXT NOT NULL, path TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Session:
    session_id: str
    base_id: str | None
    is_new: bool
    cost_total: float


class Store:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(journal)")}
        if "answer" not in cols:   # журнал старых версий — без текста ответа
            self._conn.execute("ALTER TABLE journal ADD COLUMN answer TEXT")
        if "started" not in {r[1] for r in self._conn.execute("PRAGMA table_info(sessions)")}:
            # 0 — разговор Claude ещё не начат (после сбоя), база разговора при этом сохранена
            self._conn.execute("ALTER TABLE sessions ADD COLUMN started INTEGER NOT NULL DEFAULT 1")
        self._lock = threading.Lock()

    def _exec(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # --- сессии ---

    def session(self, user_key: str, base_id: str | None, idle_minutes: int, keep_base: bool) -> Session:
        """Продолжить разговор или начать новый.

        Новый — после часа простоя или при смене базы: модель видит одну базу
        и не путает её цифры с цифрами прошлой. keep_base=True (в сообщении
        не было ссылки на базу) — остаёмся в базе текущего разговора.
        """
        row = self._exec("SELECT session_id, base_id, last_active, cost_total, started FROM sessions "
                         "WHERE user_key=?", (user_key,))
        stamp = now().isoformat()
        if row:
            sid, cur_base, last, cost, started = row[0]
            fresh = now() - datetime.fromisoformat(last) <= timedelta(minutes=idle_minutes)
            if keep_base:
                base_id = cur_base if fresh else None
            if fresh and base_id == cur_base:
                self._exec("UPDATE sessions SET last_active=?, started=1 WHERE user_key=?", (stamp, user_key))
                return Session(sid, cur_base, not started, cost)
        sid = str(uuid.uuid4())
        self._exec("INSERT OR REPLACE INTO sessions (user_key, session_id, base_id, last_active, cost_total, started) "
                   "VALUES (?, ?, ?, ?, 0, 1)", (user_key, sid, base_id, stamp))
        return Session(sid, base_id, True, 0.0)

    def restart_session(self, user_key: str) -> None:
        """После сбоя: следующий вопрос начнёт новый разговор Claude, но в той же базе."""
        self._exec("UPDATE sessions SET session_id=?, cost_total=0, started=0 WHERE user_key=?",
                   (str(uuid.uuid4()), user_key))

    def add_cost(self, user_key: str, session_total: float) -> float:
        """CLI сообщает накопленную стоимость разговора — возвращаем прирост за вопрос."""
        row = self._exec("SELECT cost_total FROM sessions WHERE user_key=?", (user_key,))
        prev = row[0][0] if row else 0.0
        self._exec("UPDATE sessions SET cost_total=? WHERE user_key=?", (session_total, user_key))
        return max(0.0, session_total - prev)

    def reset_session(self, user_key: str) -> None:
        self._exec("DELETE FROM sessions WHERE user_key=?", (user_key,))

    # --- журнал ---

    def log(self, *, channel: str, user_key: str, user_name: str | None, base_id: str | None,
            question: str, status: str, answer_chars: int = 0, files: int = 0,
            duration_ms: int = 0, cost_usd: float | None = None, error: str | None = None,
            answer: str | None = None) -> None:
        self._exec(
            "INSERT INTO journal (created_at, channel, user_key, user_name, base_id, question, status, "
            "answer_chars, files, duration_ms, cost_usd, error, answer) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now().isoformat(), channel, user_key, user_name, base_id, question[:2000], status,
             answer_chars, files, duration_ms, cost_usd, error, answer[:20000] if answer else None),
        )

    def journal(self) -> list[dict]:
        cols = ["created_at", "channel", "user_key", "user_name", "base_id", "question", "status",
                "answer_chars", "files", "duration_ms", "cost_usd", "error"]
        rows = self._exec(f"SELECT {', '.join(cols)} FROM journal ORDER BY id")
        return [dict(zip(cols, r)) for r in rows]

    # --- выборки для панели управления ---

    _PANEL_COLS = ["id", "created_at", "channel", "user_key", "user_name", "base_id", "question", "status",
                   "answer_chars", "files", "duration_ms", "cost_usd", "error"]

    def journal_page(self, *, limit: int = 50, before_id: int | None = None, statuses: list[str] | None = None,
                     user_key: str | None = None, query: str | None = None) -> list[dict]:
        """Записи журнала от новых к старым, без текста ответа."""
        where, params = [], []
        if before_id:
            where.append("id < ?")
            params.append(before_id)
        if statuses:
            where.append(f"status IN ({','.join('?' * len(statuses))})")
            params += statuses
        if user_key:
            where.append("user_key = ?")
            params.append(user_key)
        if query:
            where.append("(question LIKE ? OR answer LIKE ? OR error LIKE ?)")
            params += [f"%{query}%"] * 3
        sql = f"SELECT {', '.join(self._PANEL_COLS)} FROM journal"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        rows = self._exec(sql, (*params, max(1, min(limit, 500))))
        return [dict(zip(self._PANEL_COLS, r)) for r in rows]

    def journal_entry(self, entry_id: int) -> dict | None:
        cols = [*self._PANEL_COLS, "answer"]
        rows = self._exec(f"SELECT {', '.join(cols)} FROM journal WHERE id = ?", (entry_id,))
        return dict(zip(cols, rows[0])) if rows else None

    def journal_since(self, since: datetime) -> list[dict]:
        """Записи с момента since (UTC) — для сводки и графика."""
        cols = ["created_at", "user_key", "status", "duration_ms", "cost_usd", "channel"]
        rows = self._exec(f"SELECT {', '.join(cols)} FROM journal WHERE created_at >= ? ORDER BY id",
                          (since.astimezone(timezone.utc).isoformat(),))
        return [dict(zip(cols, r)) for r in rows]

    # --- дедупликация вебхуков ---

    def first_time(self, message_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("INSERT OR IGNORE INTO seen_messages VALUES (?, ?)",
                                     (message_id, now().isoformat()))
            return cur.rowcount == 1

    # --- пауза и лимит ---

    def get_state(self, key: str) -> str | None:
        row = self._exec("SELECT value FROM state WHERE key=?", (key,))
        return row[0][0] if row else None

    def set_state(self, key: str, value: str | None) -> None:
        if value is None:
            self._exec("DELETE FROM state WHERE key=?", (key,))
        else:
            self._exec("INSERT OR REPLACE INTO state VALUES (?, ?)", (key, value))

    # --- вложения, ждущие вопроса ---

    def add_pending_file(self, user_key: str, path: Path) -> None:
        self._exec("INSERT INTO pending_files (user_key, path, created_at) VALUES (?, ?, ?)",
                   (user_key, str(path), now().isoformat()))

    def take_pending_files(self, user_key: str, max_age_minutes: int = 30) -> list[Path]:
        rows = self._exec("SELECT path, created_at FROM pending_files WHERE user_key=? ORDER BY id", (user_key,))
        self._exec("DELETE FROM pending_files WHERE user_key=?", (user_key,))
        border = now() - timedelta(minutes=max_age_minutes)
        return [Path(p) for p, created in rows if datetime.fromisoformat(created) >= border and Path(p).exists()]
