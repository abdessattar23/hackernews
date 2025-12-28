import sqlite3
import time
from typing import Optional


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_url TEXT NOT NULL UNIQUE,
            source_title TEXT,
            status TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            s3_prefix TEXT,
            error TEXT
        )
        """
    )
    conn.commit()


def was_completed(conn: sqlite3.Connection, source_url: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM posts WHERE source_url = ? AND status = 'completed' LIMIT 1",
        (source_url,),
    ).fetchone()
    return row is not None


def mark_started(conn: sqlite3.Connection, source_url: str, source_title: Optional[str]) -> None:
    now = int(time.time())
    conn.execute(
        """
        INSERT INTO posts (source_url, source_title, status, created_at, updated_at)
        VALUES (?, ?, 'started', ?, ?)
        ON CONFLICT(source_url) DO UPDATE SET
            source_title=excluded.source_title,
            status='started',
            updated_at=?
        """,
        (source_url, source_title, now, now, now),
    )
    conn.commit()


def mark_completed(conn: sqlite3.Connection, source_url: str, s3_prefix: str) -> None:
    now = int(time.time())
    conn.execute(
        """
        UPDATE posts
        SET status='completed', s3_prefix=?, error=NULL, updated_at=?
        WHERE source_url=?
        """,
        (s3_prefix, now, source_url),
    )
    conn.commit()


def mark_failed(conn: sqlite3.Connection, source_url: str, error: str) -> None:
    now = int(time.time())
    conn.execute(
        """
        UPDATE posts
        SET status='failed', error=?, updated_at=?
        WHERE source_url=?
        """,
        (error[:2000], now, source_url),
    )
    conn.commit()
