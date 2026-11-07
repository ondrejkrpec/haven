"""Versioned SQLite storage for the wishlist domain; no network or transport."""

from contextlib import contextmanager
import json
import secrets
import sqlite3


# Execute statements individually: executescript can commit an open transaction.
MIGRATIONS = ((
    "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE entries (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        entry_id TEXT NOT NULL UNIQUE, owner_id TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version >= 1),
        state TEXT NOT NULL CHECK (state IN ('active', 'paused', 'purchased')),
        classification TEXT NOT NULL CHECK (classification IN ('complete', 'needs_details')),
        label TEXT, purpose TEXT CHECK (purpose IN ('self', 'gift')),
        purchase_mode TEXT CHECK (purchase_mode IN ('once', 'recurring')),
        recipient TEXT, note TEXT, paused_until TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE (entry_id, owner_id),
        CHECK ((state = 'paused') = (paused_until IS NOT NULL)))""",
    "CREATE INDEX entries_owner_sequence ON entries (owner_id, sequence)",
    """CREATE TABLE offers (
        offer_id TEXT PRIMARY KEY, entry_id TEXT NOT NULL UNIQUE, owner_id TEXT NOT NULL,
        url TEXT NOT NULL, shop_host TEXT NOT NULL, url_sha256 TEXT NOT NULL,
        collection_status TEXT NOT NULL CHECK (collection_status IN
            ('pending', 'ok', 'blocked', 'unsupported', 'failed', 'stale')),
        collection_generation INTEGER NOT NULL CHECK (collection_generation >= 1),
        last_observed_at TEXT,
        FOREIGN KEY (entry_id, owner_id) REFERENCES entries (entry_id, owner_id))""",
    """CREATE TABLE receipts (
        request_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
        fingerprint TEXT NOT NULL, response TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE events (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id TEXT NOT NULL, entry_id TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE,
        operation TEXT NOT NULL, entry_version INTEGER NOT NULL, occurred_at TEXT NOT NULL,
        FOREIGN KEY (entry_id, owner_id) REFERENCES entries (entry_id, owner_id),
        FOREIGN KEY (request_id) REFERENCES receipts (request_id) DEFERRABLE INITIALLY DEFERRED)""",
    """CREATE TABLE purchases (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id TEXT NOT NULL, entry_id TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE,
        purchased_at TEXT NOT NULL, recorded_at TEXT NOT NULL,
        FOREIGN KEY (entry_id, owner_id) REFERENCES entries (entry_id, owner_id),
        FOREIGN KEY (request_id) REFERENCES receipts (request_id) DEFERRABLE INITIALLY DEFERRED)""",
),)


class Store:
    def __init__(self, path, *, bot_id, owner_id, timezone):
        self.db = sqlite3.connect(path, timeout=1, isolation_level=None,
                                  autocommit=sqlite3.LEGACY_TRANSACTION_CONTROL)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA foreign_keys = ON")
            self.db.execute("PRAGMA synchronous = FULL")
            with self.transaction():
                version = self.db.execute("PRAGMA user_version").fetchone()[0]
                if version > len(MIGRATIONS):
                    raise ValueError("Unsupported wishlist database version")
                if version == 0 and self.db.execute(
                        "SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchone():
                    raise ValueError("Not an empty wishlist database")
                for index in range(version, len(MIGRATIONS)):
                    for statement in MIGRATIONS[index]:
                        self.db.execute(statement)
                    self.db.execute(f"PRAGMA user_version = {index + 1}")
                binding = json.dumps([bot_id, owner_id, timezone], separators=(",", ":"))
                row = self.db.execute("SELECT value FROM metadata WHERE key = 'binding'").fetchone()
                if row is None:
                    self.db.executemany("INSERT INTO metadata VALUES (?, ?)", (
                        ("binding", binding), ("cursor_key", secrets.token_hex(32))))
                elif row[0] != binding:
                    raise ValueError("Wishlist database identity or timezone mismatch")
                self.cursor_key = bytes.fromhex(self.db.execute(
                    "SELECT value FROM metadata WHERE key = 'cursor_key'").fetchone()[0])
        except BaseException:
            self.close()
            raise

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def close(self):
        self.db.close()
