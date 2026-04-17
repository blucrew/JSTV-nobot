"""SQLite storage. WAL mode, autocommit, single-process. No migration framework —
if schema changes later, bump SCHEMA_VERSION and add a small migrate() step."""
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

from config import DB_PATH


SCHEMA = """
CREATE TABLE IF NOT EXISTS streamers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    jtv_user_id TEXT UNIQUE NOT NULL,
    jtv_username TEXT NOT NULL,
    channel_id TEXT,
    access_token TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    token_expires_at INTEGER NOT NULL,
    panel_slug TEXT UNIQUE NOT NULL,
    panel_token TEXT NOT NULL,
    installed_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_states (
    state TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_streamers_slug ON streamers(panel_slug);
CREATE INDEX IF NOT EXISTS idx_streamers_jtv_user ON streamers(jtv_user_id);
"""


@dataclass
class Streamer:
    id: int
    jtv_user_id: str
    jtv_username: str
    channel_id: Optional[str]
    access_token: str
    refresh_token: str
    token_expires_at: int
    panel_slug: str
    panel_token: str
    installed_at: int
    updated_at: int


def _row_to_streamer(row: sqlite3.Row) -> Streamer:
    return Streamer(
        id=row["id"],
        jtv_user_id=row["jtv_user_id"],
        jtv_username=row["jtv_username"],
        channel_id=row["channel_id"],
        access_token=row["access_token"],
        refresh_token=row["refresh_token"],
        token_expires_at=row["token_expires_at"],
        panel_slug=row["panel_slug"],
        panel_token=row["panel_token"],
        installed_at=row["installed_at"],
        updated_at=row["updated_at"],
    )


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(DB_PATH), isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
    finally:
        conn.close()


def init() -> None:
    with connect() as c:
        c.executescript(SCHEMA)


def new_slug() -> str:
    # ~8 chars url-safe, enough for this scale
    return secrets.token_urlsafe(6)


def new_panel_token() -> str:
    return secrets.token_urlsafe(32)


# --- streamer CRUD -----------------------------------------------------------

def upsert_streamer(
    *,
    jtv_user_id: str,
    jtv_username: str,
    channel_id: Optional[str],
    access_token: str,
    refresh_token: str,
    token_expires_at: int,
) -> Streamer:
    """Insert or update a streamer. Preserves slug/panel_token on re-install."""
    now = int(time.time())
    with connect() as c:
        existing = c.execute(
            "SELECT * FROM streamers WHERE jtv_user_id = ?",
            (jtv_user_id,),
        ).fetchone()
        if existing:
            c.execute(
                """UPDATE streamers SET
                    jtv_username=?, channel_id=?, access_token=?, refresh_token=?,
                    token_expires_at=?, updated_at=?
                   WHERE id=?""",
                (
                    jtv_username,
                    channel_id,
                    access_token,
                    refresh_token,
                    token_expires_at,
                    now,
                    existing["id"],
                ),
            )
            row = c.execute(
                "SELECT * FROM streamers WHERE id = ?", (existing["id"],)
            ).fetchone()
        else:
            slug = new_slug()
            token = new_panel_token()
            # Extremely unlikely collision, but guard anyway
            while c.execute(
                "SELECT 1 FROM streamers WHERE panel_slug=?", (slug,)
            ).fetchone():
                slug = new_slug()
            c.execute(
                """INSERT INTO streamers (
                    jtv_user_id, jtv_username, channel_id, access_token, refresh_token,
                    token_expires_at, panel_slug, panel_token, installed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    jtv_user_id,
                    jtv_username,
                    channel_id,
                    access_token,
                    refresh_token,
                    token_expires_at,
                    slug,
                    token,
                    now,
                    now,
                ),
            )
            row = c.execute(
                "SELECT * FROM streamers WHERE jtv_user_id = ?",
                (jtv_user_id,),
            ).fetchone()
    return _row_to_streamer(row)


def get_streamer_by_id(streamer_id: int) -> Optional[Streamer]:
    with connect() as c:
        row = c.execute(
            "SELECT * FROM streamers WHERE id = ?", (streamer_id,)
        ).fetchone()
    return _row_to_streamer(row) if row else None


def get_streamer_by_slug(slug: str) -> Optional[Streamer]:
    with connect() as c:
        row = c.execute(
            "SELECT * FROM streamers WHERE panel_slug = ?", (slug,)
        ).fetchone()
    return _row_to_streamer(row) if row else None


def all_streamers() -> list[Streamer]:
    with connect() as c:
        rows = c.execute("SELECT * FROM streamers ORDER BY id").fetchall()
    return [_row_to_streamer(r) for r in rows]


def update_tokens(
    streamer_id: int,
    access_token: str,
    refresh_token: str,
    token_expires_at: int,
) -> None:
    now = int(time.time())
    with connect() as c:
        c.execute(
            """UPDATE streamers SET access_token=?, refresh_token=?,
               token_expires_at=?, updated_at=? WHERE id=?""",
            (access_token, refresh_token, token_expires_at, now, streamer_id),
        )


def rotate_panel_token(streamer_id: int) -> str:
    token = new_panel_token()
    now = int(time.time())
    with connect() as c:
        c.execute(
            "UPDATE streamers SET panel_token=?, updated_at=? WHERE id=?",
            (token, now, streamer_id),
        )
    return token


# --- oauth state -------------------------------------------------------------

def new_oauth_state() -> str:
    state = secrets.token_urlsafe(24)
    now = int(time.time())
    with connect() as c:
        c.execute(
            "INSERT INTO oauth_states (state, created_at) VALUES (?, ?)",
            (state, now),
        )
        # opportunistic cleanup of states older than 1h
        c.execute(
            "DELETE FROM oauth_states WHERE created_at < ?", (now - 3600,)
        )
    return state


def consume_oauth_state(state: str) -> bool:
    """Returns True if state was valid and freshly consumed."""
    with connect() as c:
        cur = c.execute(
            "DELETE FROM oauth_states WHERE state = ?", (state,)
        )
        return cur.rowcount > 0
