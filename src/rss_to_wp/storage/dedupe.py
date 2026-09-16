"""SQLite-based deduplication storage."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from rss_to_wp.config import get_data_dir
from rss_to_wp.utils import get_logger

logger = get_logger("storage.dedupe")


class DedupeStore:
    """SQLite-based store for tracking processed entries."""

    def __init__(self, db_path: Optional[Path] = None):
        """Initialize the deduplication store.

        Args:
            db_path: Path to SQLite database. Defaults to data/processed.db
        """
        if db_path is None:
            db_path = get_data_dir() / "processed.db"

        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """Initialize the database schema."""
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS processed_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entry_key TEXT UNIQUE NOT NULL,
                    feed_url TEXT,
                    entry_title TEXT,
                    entry_link TEXT,
                    wp_post_id INTEGER,
                    wp_post_url TEXT,
                    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_entry_key
                ON processed_entries(entry_key)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_feed_url
                ON processed_entries(feed_url)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS editorial_decisions (
                    fingerprint TEXT PRIMARY KEY, reason TEXT NOT NULL,
                    decided_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS roundup_candidates (
                    source_url TEXT PRIMARY KEY, payload TEXT NOT NULL, expires_at TEXT NOT NULL
                )
            """)
            conn.commit()

        logger.debug("database_initialized", path=str(self.db_path))

    @contextmanager
    def _get_connection(self):
        """Get a database connection context manager."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def is_processed(self, entry_key: str) -> bool:
        """Check if an entry has already been processed.

        Args:
            entry_key: Unique key for the entry.

        Returns:
            True if entry was already processed.
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT 1 FROM processed_entries WHERE entry_key = ?",
                (entry_key,),
            )
            result = cursor.fetchone() is not None

        if result:
            logger.debug("entry_already_processed", key=entry_key)

        return result

    def mark_processed(
        self,
        entry_key: str,
        feed_url: str,
        entry_title: str,
        entry_link: str,
        wp_post_id: Optional[int] = None,
        wp_post_url: Optional[str] = None,
    ) -> None:
        """Mark an entry as processed.

        Args:
            entry_key: Unique key for the entry.
            feed_url: URL of the source feed.
            entry_title: Title of the entry.
            entry_link: Original link of the entry.
            wp_post_id: WordPress post ID (if published).
            wp_post_url: WordPress post URL (if published).
        """
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO processed_entries
                (entry_key, feed_url, entry_title, entry_link, wp_post_id, wp_post_url, processed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_key,
                    feed_url,
                    entry_title,
                    entry_link,
                    wp_post_id,
                    wp_post_url,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()

        logger.info(
            "entry_marked_processed",
            key=entry_key,
            wp_post_id=wp_post_id,
        )

    def rejection_reason(self, fingerprint: str) -> str | None:
        """Unchanged rejected inputs cool down for 24 hours; repaired sources retry."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT reason FROM editorial_decisions WHERE fingerprint=? AND decided_at>?",
                (fingerprint, cutoff),
            ).fetchone()
        return row["reason"] if row else None

    def record_rejection(self, fingerprint: str, reason: str) -> None:
        with self._get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO editorial_decisions VALUES (?, ?, ?)",
                (fingerprint, reason, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()

    def source_seen(self, source_url: str) -> bool:
        from rss_to_wp.editorial import canonical_url

        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT entry_link FROM processed_entries WHERE entry_link != ''"
            ).fetchall()
        for row in rows:
            try:
                if canonical_url(row["entry_link"]) == canonical_url(source_url):
                    return True
            except ValueError:
                continue
        return False

    def queue_roundup(self, candidate: dict) -> None:
        published = datetime.fromisoformat(candidate["source_published_at"])
        expires = (published.astimezone(timezone.utc) + timedelta(hours=48)).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                "DELETE FROM roundup_candidates WHERE expires_at < ?",
                (datetime.now(timezone.utc).isoformat(),),
            )
            conn.execute(
                "INSERT OR REPLACE INTO roundup_candidates VALUES (?, ?, ?)",
                (candidate["source_url"], json.dumps(candidate), expires),
            )
            conn.commit()

    def load_roundup_candidates(self) -> list[dict]:
        # Read-only: dry runs must not mutate queue state or expiration history.
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT payload FROM roundup_candidates ORDER BY expires_at"
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def remove_roundup(self, source_url: str) -> None:
        with self._get_connection() as conn:
            conn.execute("DELETE FROM roundup_candidates WHERE source_url=?", (source_url,))
            conn.commit()

    def get_processed_count(self, feed_url: Optional[str] = None) -> int:
        """Get count of processed entries.

        Args:
            feed_url: Optional filter by feed URL.

        Returns:
            Number of processed entries.
        """
        with self._get_connection() as conn:
            if feed_url:
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM processed_entries WHERE feed_url = ?",
                    (feed_url,),
                )
            else:
                cursor = conn.execute("SELECT COUNT(*) FROM processed_entries")

            return cursor.fetchone()[0]

    def get_recent_entries(
        self,
        limit: int = 100,
        feed_url: Optional[str] = None,
    ) -> list[dict]:
        """Get recently processed entries.

        Args:
            limit: Maximum entries to return.
            feed_url: Optional filter by feed URL.

        Returns:
            List of entry dictionaries.
        """
        with self._get_connection() as conn:
            if feed_url:
                cursor = conn.execute(
                    """
                    SELECT * FROM processed_entries
                    WHERE feed_url = ?
                    ORDER BY processed_at DESC
                    LIMIT ?
                    """,
                    (feed_url, limit),
                )
            else:
                cursor = conn.execute(
                    """
                    SELECT * FROM processed_entries
                    ORDER BY processed_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                )

            return [dict(row) for row in cursor.fetchall()]

    def clear_all(self) -> int:
        """Clear all processed entries.

        Returns:
            Number of entries deleted.
        """
        with self._get_connection() as conn:
            cursor = conn.execute("DELETE FROM processed_entries")
            count = cursor.rowcount
            conn.execute("DELETE FROM editorial_decisions")
            conn.execute("DELETE FROM roundup_candidates")
            conn.commit()

        logger.warning("database_cleared", deleted_count=count)
        return count
