"""The bot's own small SQLite state (offsets and note ↔ message mapping)."""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notes (
  note_id            TEXT NOT NULL,
  commit_sha         TEXT NOT NULL,
  created_at         TEXT NOT NULL,
  receipt_message_id INTEGER,
  source_message_id  INTEGER
);
CREATE TABLE IF NOT EXISTS message_note (
  message_id INTEGER PRIMARY KEY,
  note_id    TEXT NOT NULL
);
"""


class State:
    """Thread-safe: one connection guarded by a lock."""

    def __init__(self, state_dir: Path):
        state_dir = Path(state_dir)
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(state_dir, 0o700)
        path = state_dir / "state.db"
        if not path.exists():
            fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- key/value -------------------------------------------------------

    def get(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, key: str, value: object) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    def get_int(self, key: str) -> int | None:
        value = self.get(key)
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            return None

    # --- notes -----------------------------------------------------------

    def record_note(
        self,
        note_id: str,
        commit_sha: str,
        receipt_message_id: int | None,
        source_message_ids: Iterable[int],
    ) -> None:
        """Remember a note the bot created and the messages that refer to it."""
        sources = [m for m in source_message_ids if type(m) is int]
        created = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO notes (note_id, commit_sha, created_at, receipt_message_id, source_message_id)"
                " VALUES (?, ?, ?, ?, ?)",
                (note_id, commit_sha, created, receipt_message_id, sources[0] if sources else None),
            )
            self._map(note_id, [receipt_message_id, *sources])

    def map_messages(self, note_id: str, message_ids: Iterable[int | None]) -> None:
        """Make replies to these messages append to ``note_id``."""
        with self._lock, self._conn:
            self._map(note_id, message_ids)

    def _map(self, note_id: str, message_ids: Iterable[int | None]) -> None:
        for message_id in message_ids:
            if type(message_id) is int:
                self._conn.execute(
                    "INSERT INTO message_note (message_id, note_id) VALUES (?, ?)"
                    " ON CONFLICT(message_id) DO UPDATE SET note_id = excluded.note_id",
                    (message_id, note_id),
                )

    def forget_note(self, note_id: str) -> int:
        """Drop a note that was undone: its message mappings and its ``notes`` rows.

        The id is free again afterwards (the next note of that day gets it), so
        replies to the undone note's receipt or source message must not reach
        the new note, and /append without an id must not pick it. Returns the
        number of message mappings removed.
        """
        with self._lock, self._conn:
            removed = self._conn.execute("DELETE FROM message_note WHERE note_id = ?", (note_id,)).rowcount
            self._conn.execute("DELETE FROM notes WHERE note_id = ?", (note_id,))
        return removed

    def note_for_message(self, message_id: int) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT note_id FROM message_note WHERE message_id = ?", (message_id,)
            ).fetchone()
        return row[0] if row else None

    def recent_note_ids(self, limit: int = 20) -> list[str]:
        """Notes the bot created, newest first, without duplicates."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT note_id FROM notes ORDER BY rowid DESC LIMIT ?", (limit * 2,)
            ).fetchall()
        seen: list[str] = []
        for (note_id,) in rows:
            if note_id not in seen:
                seen.append(note_id)
        return seen[:limit]

    def last_note_id(self) -> str | None:
        ids = self.recent_note_ids(1)
        return ids[0] if ids else None
