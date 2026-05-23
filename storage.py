"""SQLite-backed store for tracking which events have already been notified."""

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)


class NotificationStore:
    def __init__(self, db_path: str = "alertle.db"):
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
                    description_hash TEXT,
                    PRIMARY KEY (uid, subscription_label)
                )
                """
            )
            # Migration for existing installs
            try:
                conn.execute("ALTER TABLE sent ADD COLUMN description_hash TEXT")
            except sqlite3.OperationalError:
                pass  # column already exists
            except Exception as exc:
                log.warning("DB migration failed: %s", exc)

    def already_sent(self, uid: str, subscription_label: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM sent WHERE uid = ? AND subscription_label = ?",
                (uid, subscription_label),
            ).fetchone()
        return row is not None

    def description_already_sent(self, description_hash: str) -> bool:
        """True if any previously sent record has this exact description hash."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM sent WHERE description_hash = ? LIMIT 1",
                (description_hash,),
            ).fetchone()
        return row is not None

    def mark_sent(self, uid: str, subscription_label: str, notified_at: str,
                  description_hash: str | None = None):
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sent (uid, subscription_label, notified_at, description_hash) "
                "VALUES (?, ?, ?, ?)",
                (uid, subscription_label, notified_at, description_hash),
            )

    def prune_old(self, before_iso: str):
        """Remove entries for events that started before *before_iso* (ISO timestamp)."""
        with self._conn() as conn:
            conn.execute("DELETE FROM sent WHERE notified_at < ?", (before_iso,))
