"""SQLite-backed store for tracking which events have already been notified."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path


class NotificationStore:
    def __init__(self, db_path: str = "epg_notifier.db"):
        self.path = Path(db_path)
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self):
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sent (
                    uid TEXT NOT NULL,
                    subscription_label TEXT NOT NULL,
                    notified_at TEXT NOT NULL,
                    PRIMARY KEY (uid, subscription_label)
                )
                """
            )

    def already_sent(self, uid: str, subscription_label: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM sent WHERE uid = ? AND subscription_label = ?",
                (uid, subscription_label),
            ).fetchone()
        return row is not None

    def mark_sent(self, uid: str, subscription_label: str, notified_at: str):
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sent (uid, subscription_label, notified_at) VALUES (?, ?, ?)",
                (uid, subscription_label, notified_at),
            )

    def prune_old(self, before_iso: str):
        """Remove entries for events that started before *before_iso* (ISO timestamp)."""
        with self._conn() as conn:
            conn.execute("DELETE FROM sent WHERE notified_at < ?", (before_iso,))
