"""SQLite storage for items, reminder messages and moderation strikes."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

import config

_conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row

_conn.executescript(
    """
    CREATE TABLE IF NOT EXISTS items (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         INTEGER NOT NULL,
        user_name       TEXT    NOT NULL,
        description     TEXT    NOT NULL,
        photo_file_id   TEXT,
        found_date      TEXT    NOT NULL,   -- YYYY-MM-DD
        found_location  TEXT    NOT NULL,
        claim_location  TEXT    NOT NULL,
        created_at      TEXT    NOT NULL,   -- ISO timestamp, UTC
        status          TEXT    NOT NULL DEFAULT 'active',  -- active | returned | removed | expired
        group_message_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS reminder_messages (
        message_id INTEGER PRIMARY KEY,
        item_id    INTEGER            -- NULL for the header message
    );
    CREATE TABLE IF NOT EXISTS strikes (
        user_id    INTEGER PRIMARY KEY,
        count      INTEGER NOT NULL DEFAULT 0,
        banned     INTEGER NOT NULL DEFAULT 0,
        muted_until TEXT
    );
    CREATE TABLE IF NOT EXISTS mod_log (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER NOT NULL,
        by_user_id INTEGER,           -- NULL when automatic
        action     TEXT    NOT NULL,
        reason     TEXT,
        created_at TEXT    NOT NULL
    );
    """
)
_conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------- items ----------

def add_item(user_id: int, user_name: str, description: str, photo_file_id: Optional[str],
             found_date: str, found_location: str, claim_location: str) -> int:
    cur = _conn.execute(
        "INSERT INTO items (user_id, user_name, description, photo_file_id, found_date,"
        " found_location, claim_location, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, user_name, description, photo_file_id, found_date, found_location, claim_location, _now()),
    )
    _conn.commit()
    return cur.lastrowid


def get_item(item_id: int) -> Optional[sqlite3.Row]:
    return _conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()


def set_group_message(item_id: int, message_id: int) -> None:
    _conn.execute("UPDATE items SET group_message_id = ? WHERE id = ?", (message_id, item_id))
    _conn.commit()


def set_status(item_id: int, status: str) -> None:
    _conn.execute("UPDATE items SET status = ? WHERE id = ?", (status, item_id))
    _conn.commit()


def active_items() -> list[sqlite3.Row]:
    return _conn.execute("SELECT * FROM items WHERE status = 'active' ORDER BY id").fetchall()


def active_items_for_user(user_id: int) -> list[sqlite3.Row]:
    return _conn.execute(
        "SELECT * FROM items WHERE status = 'active' AND user_id = ? ORDER BY id", (user_id,)
    ).fetchall()


def items_older_than(cutoff_iso: str) -> list[sqlite3.Row]:
    return _conn.execute(
        "SELECT * FROM items WHERE status = 'active' AND created_at < ?", (cutoff_iso,)
    ).fetchall()


# ---------- reminder messages ----------

def add_reminder_message(message_id: int, item_id: Optional[int]) -> None:
    _conn.execute("INSERT OR REPLACE INTO reminder_messages VALUES (?, ?)", (message_id, item_id))
    _conn.commit()


def pop_reminder_messages(item_id: Optional[int] = None) -> list[int]:
    """Remove and return reminder message ids (all of them, or only those for one item)."""
    if item_id is None:
        rows = _conn.execute("SELECT message_id FROM reminder_messages").fetchall()
        _conn.execute("DELETE FROM reminder_messages")
    else:
        rows = _conn.execute("SELECT message_id FROM reminder_messages WHERE item_id = ?", (item_id,)).fetchall()
        _conn.execute("DELETE FROM reminder_messages WHERE item_id = ?", (item_id,))
    _conn.commit()
    return [r["message_id"] for r in rows]


# ---------- moderation ----------

def get_strikes(user_id: int) -> sqlite3.Row:
    _conn.execute("INSERT OR IGNORE INTO strikes (user_id) VALUES (?)", (user_id,))
    _conn.commit()
    return _conn.execute("SELECT * FROM strikes WHERE user_id = ?", (user_id,)).fetchone()


def set_strikes(user_id: int, count: int) -> None:
    get_strikes(user_id)
    _conn.execute("UPDATE strikes SET count = ? WHERE user_id = ?", (max(0, count), user_id))
    _conn.commit()


def set_banned(user_id: int, banned: bool) -> None:
    get_strikes(user_id)
    _conn.execute("UPDATE strikes SET banned = ? WHERE user_id = ?", (int(banned), user_id))
    _conn.commit()


def set_muted_until(user_id: int, until_iso: Optional[str]) -> None:
    get_strikes(user_id)
    _conn.execute("UPDATE strikes SET muted_until = ? WHERE user_id = ?", (until_iso, user_id))
    _conn.commit()


def log_action(user_id: int, by_user_id: Optional[int], action: str, reason: Optional[str]) -> None:
    _conn.execute(
        "INSERT INTO mod_log (user_id, by_user_id, action, reason, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, by_user_id, action, reason, _now()),
    )
    _conn.commit()
