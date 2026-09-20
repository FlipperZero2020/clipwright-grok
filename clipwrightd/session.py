"""clipwrightd.session — the daemon's memory, in one SQLite file.

Three tables, one ``Store`` per daemon:

- ``sessions``: a 6-char token -> the live recipe instance, its undo stack,
  the Telegram message the preview lives in and, for a session opened in a
  group, the message that asked for it (``origin_message_id``: everything
  the daemon sends for the session replies to it; None in a DM). Callbacks
  carry only the token (``c/<token>/<knob>/<value>``), so the recipe never
  round-trips through a button.
- ``ledger``: a sent file's ``file_unique_id`` -> the recipe that made it and
  the user who exported it. This is what lets ``/remix`` on any old GIF
  reopen its knobs — for its owner; the recipe names their private upload.
- ``quota``: (user_id, UTC day) -> renders today.

Sessions are the only rows that age out: ``expire_sessions`` drops the ones
idle past a cutoff so the daemon can reclaim their render directories, and
``referenced_inputs`` names every upload a live session or a ledger entry
still points at, so the sweep never removes a clip ``/remix`` could need.

The connection is opened with ``check_same_thread=False`` and every access
goes through one ``threading.Lock`` so the render worker thread can write
while the poll thread reads. WAL journal mode keeps readers off the writer's
back. Recipes are stored as JSON text; every read returns a fresh ``dict``,
so callers can mutate freely.

The file holds every user's recipes, server paths and ledger, so it is kept
private the way ``bot.env`` is: the state dir is created 0700 and the
database chmod'd 0600 before WAL mode creates its ``-wal``/``-shm`` sidecars
(SQLite gives those the main file's mode), whatever the process umask.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

UNDO_DEPTH = 20
TOKEN_LEN = 6
_TOKEN_TRIES = 64
DIR_MODE = 0o700
FILE_MODE = 0o600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    chat_id     INTEGER NOT NULL,
    message_id  INTEGER,
    origin_message_id INTEGER,
    recipe_json TEXT NOT NULL,
    undo_json   TEXT NOT NULL DEFAULT '[]',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    file_unique_id TEXT PRIMARY KEY,
    recipe_json    TEXT NOT NULL,
    token          TEXT,
    user_id        INTEGER,
    created_at     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS quota (
    user_id INTEGER NOT NULL,
    day     TEXT NOT NULL,
    count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);
"""

# Columns added after the first release, applied to an older file on open.
_MIGRATIONS = [("ledger", "user_id", "INTEGER"), ("sessions", "origin_message_id", "INTEGER")]


@dataclass(frozen=True)
class Session:
    """One row of ``sessions``, decoded. ``undo`` is oldest-first; ``origin_message_id`` is None in a DM."""

    token: str
    user_id: int
    chat_id: int
    message_id: int | None
    recipe: dict
    undo: list[dict]
    created_at: float
    updated_at: float
    origin_message_id: int | None = None


@dataclass(frozen=True)
class LedgerEntry:
    """One row of ``ledger``, decoded. ``user_id`` is None only for rows older than the column."""

    file_unique_id: str
    recipe: dict
    token: str | None
    user_id: int | None
    created_at: float


def _new_token() -> str:
    """6 URL-safe chars ([A-Za-z0-9_-]); ~36 bits, retried on collision."""
    return secrets.token_urlsafe(8)[:TOKEN_LEN]


def _make_private(path: str) -> None:
    """chmod ``path`` to 0600; a failure (foreign owner, odd filesystem) is not fatal."""
    try:
        os.chmod(path, FILE_MODE)
    except OSError:
        pass


def utc_day(now: float | None = None) -> str:
    """The quota bucket: ``YYYY-MM-DD`` in UTC."""
    ts = time.time() if now is None else now
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


class Store:
    """SQLite-backed sessions, undo, file ledger and daily quota."""

    def __init__(self, path: str) -> None:
        self.path = path
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, mode=DIR_MODE, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        _make_private(path)
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """Add columns the schema gained since the file was created (idempotent)."""
        for table, column, kind in _MIGRATIONS:
            present = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in present:
                with self._conn:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- sessions -----------------------------------------------------------

    def create_session(self, user_id: int, chat_id: int, recipe: dict,
                       origin_message_id: int | None = None) -> str:
        """Insert a new session and return its token. ``origin_message_id`` is the group message it answers."""
        now = time.time()
        recipe_json = json.dumps(recipe)
        with self._lock:
            for _ in range(_TOKEN_TRIES):
                token = _new_token()
                try:
                    with self._conn:
                        self._conn.execute(
                            "INSERT INTO sessions (token, user_id, chat_id, message_id, origin_message_id,"
                            " recipe_json, undo_json, created_at, updated_at)"
                            " VALUES (?, ?, ?, NULL, ?, ?, '[]', ?, ?)",
                            (token, user_id, chat_id, origin_message_id, recipe_json, now, now),
                        )
                except sqlite3.IntegrityError:
                    continue
                return token
        raise RuntimeError(f"could not allocate a unique session token in {_TOKEN_TRIES} tries")

    def get(self, token: str) -> Session | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE token = ?", (token,)
            ).fetchone()
        return _session_from_row(row) if row else None

    def update(self, token: str, recipe: dict, message_id: int | None = None) -> None:
        """Replace the live recipe, pushing the previous one onto the undo stack.

        The stack keeps the last ``UNDO_DEPTH`` recipes. An update whose recipe
        equals the current one (for example, one that only records
        ``message_id`` after ``sendAnimation``) pushes nothing, so Undo never
        appears to do nothing. ``message_id`` is left untouched when ``None``.
        Raises ``KeyError`` for an unknown token.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT recipe_json, undo_json FROM sessions WHERE token = ?", (token,)
            ).fetchone()
            if row is None:
                raise KeyError(token)
            previous = json.loads(row["recipe_json"])
            undo = json.loads(row["undo_json"])
            if recipe != previous:
                undo = (undo + [previous])[-UNDO_DEPTH:]
            with self._conn:
                self._conn.execute(
                    "UPDATE sessions SET recipe_json = ?, undo_json = ?,"
                    " message_id = COALESCE(?, message_id), updated_at = ?"
                    " WHERE token = ?",
                    (json.dumps(recipe), json.dumps(undo), message_id, time.time(), token),
                )

    def set_message_id(self, token: str, message_id: int) -> None:
        """Record the preview message without touching the recipe or the undo stack.

        The render worker calls this from a snapshot of the session, so it
        must not write the recipe back — a knob press may have landed since.
        Raises ``KeyError`` for an unknown token.
        """
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE sessions SET message_id = ?, updated_at = ? WHERE token = ?",
                (message_id, time.time(), token),
            )
            if cur.rowcount == 0:
                raise KeyError(token)

    def undo(self, token: str) -> dict | None:
        """Pop the undo stack into the live recipe and return it; None if empty."""
        with self._lock:
            row = self._conn.execute(
                "SELECT undo_json FROM sessions WHERE token = ?", (token,)
            ).fetchone()
            if row is None:
                return None
            undo = json.loads(row["undo_json"])
            if not undo:
                return None
            restored = undo.pop()
            with self._conn:
                self._conn.execute(
                    "UPDATE sessions SET recipe_json = ?, undo_json = ?, updated_at = ?"
                    " WHERE token = ?",
                    (json.dumps(restored), json.dumps(undo), time.time(), token),
                )
        return restored

    def expire_sessions(self, cutoff: float) -> list[Session]:
        """Delete every session last touched before ``cutoff`` and return them."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sessions WHERE updated_at < ?", (cutoff,)
            ).fetchall()
            with self._conn:
                self._conn.execute("DELETE FROM sessions WHERE updated_at < ?", (cutoff,))
        return [_session_from_row(row) for row in rows]

    def session_tokens(self) -> set[str]:
        """Every live session token."""
        with self._lock:
            rows = self._conn.execute("SELECT token FROM sessions").fetchall()
        return {row["token"] for row in rows}

    def referenced_inputs(self) -> set[str]:
        """Every ``input`` path a live session or a ledger entry still names."""
        with self._lock:
            rows = (self._conn.execute("SELECT recipe_json FROM sessions").fetchall()
                    + self._conn.execute("SELECT recipe_json FROM ledger").fetchall())
        paths: set[str] = set()
        for row in rows:
            src = json.loads(row["recipe_json"]).get("input")
            if isinstance(src, str) and src:
                paths.add(src)
        return paths

    # -- ledger -------------------------------------------------------------

    def ledger_put(self, file_unique_id: str, recipe: dict, token: str | None = None,
                   user_id: int | None = None) -> None:
        """Remember which recipe produced a sent file, and for whom (replaces an earlier entry)."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO ledger (file_unique_id, recipe_json, token, user_id, created_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(file_unique_id) DO UPDATE SET"
                " recipe_json = excluded.recipe_json, token = excluded.token,"
                " user_id = excluded.user_id, created_at = excluded.created_at",
                (file_unique_id, json.dumps(recipe), token, user_id, time.time()),
            )

    def ledger_get(self, file_unique_id: str) -> dict | None:
        """The recipe that produced a sent file, or None."""
        entry = self.ledger_entry(file_unique_id)
        return entry.recipe if entry else None

    def ledger_entry(self, file_unique_id: str) -> LedgerEntry | None:
        """The whole ledger row for a sent file, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM ledger WHERE file_unique_id = ?", (file_unique_id,)
            ).fetchone()
        if row is None:
            return None
        return LedgerEntry(file_unique_id=row["file_unique_id"], recipe=json.loads(row["recipe_json"]),
                           token=row["token"], user_id=row["user_id"], created_at=row["created_at"])

    # -- quota --------------------------------------------------------------

    def quota_hit(self, user_id: int, per_day: int, *, day: str | None = None) -> bool:
        """Count one render for ``user_id`` today (UTC); True once over ``per_day``.

        The increment happens before the comparison, so the call that crosses
        the limit is the first to return True. ``day`` overrides the bucket
        (tests, or a daemon that wants to pin the clock).
        """
        bucket = utc_day() if day is None else day
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO quota (user_id, day, count) VALUES (?, ?, 1)"
                " ON CONFLICT(user_id, day) DO UPDATE SET count = count + 1",
                (user_id, bucket),
            )
            count = self._conn.execute(
                "SELECT count FROM quota WHERE user_id = ? AND day = ?", (user_id, bucket)
            ).fetchone()["count"]
        return count > per_day


def _session_from_row(row: sqlite3.Row) -> Session:
    return Session(
        token=row["token"],
        user_id=row["user_id"],
        chat_id=row["chat_id"],
        message_id=row["message_id"],
        recipe=json.loads(row["recipe_json"]),
        undo=json.loads(row["undo_json"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        origin_message_id=row["origin_message_id"],
    )
