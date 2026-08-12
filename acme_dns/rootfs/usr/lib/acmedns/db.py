"""SQLite persistence for acme-dns accounts and TXT values."""

from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
import threading
import time
import uuid

_LOG = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    username  TEXT PRIMARY KEY,
    password  TEXT NOT NULL,
    subdomain TEXT NOT NULL UNIQUE,
    allowfrom TEXT NOT NULL DEFAULT '[]',
    -- Set when the account was pre-registered for a specific certificate
    -- domain by bootstrap; NULL for accounts created through POST /register.
    domain    TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS txt (
    subdomain  TEXT NOT NULL,
    value      TEXT NOT NULL DEFAULT '',
    lastupdate INTEGER NOT NULL DEFAULT 0
);

-- Without this the DNS hot path is a full table scan.
CREATE INDEX IF NOT EXISTS idx_txt_subdomain ON txt (subdomain);
"""


class Database:
    """Thread-safe wrapper around the account and TXT tables.

    The DNS listeners and the HTTP API run in different threads, so a single
    connection is shared under a lock rather than opened per request.
    """

    def __init__(self, path: str) -> None:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        _LOG.info("Opened database %s", path)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------------------------------------------------------- accounts

    def register(self, domain: str | None = None, allow_from: list[str] | None = None) -> dict:
        """Create a new account, with the two TXT slots it needs.

        Two rows are inserted per subdomain on purpose. A certificate covering
        both `example.org` and `*.example.org` produces two ACME authorizations
        whose challenge record is the *same* name, each needing a different TXT
        value present simultaneously. One slot would mean the second update
        overwrites the first and an authorization always fails.
        """
        account = {
            "username": str(uuid.uuid4()),
            "password": secrets.token_urlsafe(32),
            "subdomain": str(uuid.uuid4()),
            "allowfrom": list(allow_from or []),
        }

        with self._lock:
            self._conn.execute(
                "INSERT INTO accounts (username, password, subdomain, allowfrom, domain)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    account["username"],
                    account["password"],
                    account["subdomain"],
                    json.dumps(account["allowfrom"]),
                    domain,
                ),
            )
            self._conn.executemany(
                "INSERT INTO txt (subdomain, value, lastupdate) VALUES (?, '', 0)",
                [(account["subdomain"],), (account["subdomain"],)],
            )
            self._conn.commit()

        _LOG.info(
            "Registered account %s (subdomain %s)%s",
            account["username"],
            account["subdomain"],
            f" for {domain}" if domain else "",
        )
        return account

    def _fetch_account(self, column: str, value: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT username, password, subdomain, allowfrom, domain"
                f" FROM accounts WHERE {column} = ?",  # column is never user input
                (value,),
            ).fetchone()

        if row is None:
            return None

        account = dict(row)
        try:
            account["allowfrom"] = json.loads(account["allowfrom"])
        except (TypeError, json.JSONDecodeError):
            account["allowfrom"] = []
        return account

    def get_by_username(self, username: str) -> dict | None:
        return self._fetch_account("username", username)

    def get_by_domain(self, domain: str) -> dict | None:
        return self._fetch_account("domain", domain)

    def get_by_subdomain(self, subdomain: str) -> dict | None:
        return self._fetch_account("subdomain", subdomain)

    # -------------------------------------------------------------------- TXT

    def update_txt(self, subdomain: str, value: str) -> bool:
        """Write `value` into the least recently updated of the two slots."""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE txt SET value = ?, lastupdate = ?"
                " WHERE rowid = ("
                "   SELECT rowid FROM txt WHERE subdomain = ?"
                "   ORDER BY lastupdate ASC, rowid ASC LIMIT 1"
                " )",
                (value, int(time.time()), subdomain),
            )
            self._conn.commit()
            updated = cursor.rowcount > 0

        if not updated:
            _LOG.warning("No TXT slot found for subdomain %s", subdomain)
        return updated

    def get_txt_values(self, subdomain: str) -> list[str]:
        """Return every non-empty TXT value for a subdomain (at most two)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT value FROM txt WHERE subdomain = ? AND value != ''",
                (subdomain,),
            ).fetchall()
        return [row["value"] for row in rows]
