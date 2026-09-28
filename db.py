"""SQLite storage for CallDesk. Standard library only, no ORM.

Every helper opens its own short-lived connection and closes it. The server
runs ThreadingHTTPServer, and sqlite3 connections are not safe to share
across threads, so pooling would buy nothing for a demo-scale app.

Sessions live in the database rather than in memory, so a restart does not log
everyone out.
"""

import os
import sqlite3

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.join(ROOT, "data")
DB_PATH = os.environ.get("CALLDESK_DB", os.path.join(DB_DIR, "calldesk.db"))

# Session lifetime. SQLite stores CURRENT_TIMESTAMP in UTC, so expiry compares
# against UTC and needs no timezone handling.
SESSION_MAX_AGE_DAYS = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    email         TEXT UNIQUE,
    password_hash TEXT,
    business_name TEXT,
    mobile_number TEXT,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS twilio_connections (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER UNIQUE,
    account_sid  TEXT,
    connected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS phone_numbers (
    id             INTEGER PRIMARY KEY,
    user_id        INTEGER,
    phone_number   TEXT,
    agent_id       TEXT,
    twilio_sid     TEXT,
    answering_mode TEXT DEFAULT 'agent',
    forward_to     TEXT,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS calls (
    id              INTEGER PRIMARY KEY,
    user_id         INTEGER,
    caller_name     TEXT,
    callback_number TEXT,
    problem         TEXT,
    urgency         TEXT,
    transcript      TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users (id)
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id);
CREATE INDEX IF NOT EXISTS idx_numbers_user  ON phone_numbers (user_id);
CREATE INDEX IF NOT EXISTS idx_calls_user    ON calls (user_id, created_at DESC);
"""


def connect():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _table_exists(conn, name):
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)
    ).fetchone())


def _columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn, table, column, decl):
    if column not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _migrate(conn):
    """Bring a database written by an earlier version up to SCHEMA.

    CREATE TABLE IF NOT EXISTS silently leaves an existing table alone, so new
    columns have to be added by hand and a table whose constraints changed has
    to be rebuilt.
    """
    if not _table_exists(conn, "users"):
        return

    _add_column(conn, "users", "mobile_number", "TEXT")

    # phone_numbers gained answering_mode and forward_to.
    if _table_exists(conn, "phone_numbers"):
        _add_column(conn, "phone_numbers", "answering_mode", "TEXT DEFAULT 'agent'")
        _add_column(conn, "phone_numbers", "forward_to", "TEXT")

    # twilio_connections dropped auth_token and gained UNIQUE on user_id.
    # SQLite cannot add a constraint in place, so rebuild it and keep the most
    # recent connection per user.
    if _table_exists(conn, "twilio_connections"):
        columns = _columns(conn, "twilio_connections")
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='twilio_connections'"
        ).fetchone()["sql"]
        needs_unique = "user_id INTEGER UNIQUE" not in " ".join(sql.split())
        if "auth_token" in columns or needs_unique:
            rows = conn.execute(
                "SELECT user_id, account_sid FROM twilio_connections "
                "WHERE user_id IS NOT NULL ORDER BY connected_at DESC, id DESC"
            ).fetchall()
            latest = {}
            for row in rows:
                latest.setdefault(row["user_id"], row["account_sid"])
            conn.execute("ALTER TABLE twilio_connections RENAME TO twilio_connections_old")
            conn.execute("""CREATE TABLE twilio_connections (
                id           INTEGER PRIMARY KEY,
                user_id      INTEGER UNIQUE,
                account_sid  TEXT,
                connected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )""")
            for user_id, account_sid in latest.items():
                conn.execute(
                    "INSERT INTO twilio_connections (user_id, account_sid) VALUES (?, ?)",
                    (user_id, account_sid),
                )
            conn.execute("DROP TABLE twilio_connections_old")
            print(f"db: rebuilt twilio_connections, kept {len(latest)} account(s)")

    if _table_exists(conn, "phone_numbers"):
        conn.execute(
            "UPDATE phone_numbers SET answering_mode = 'agent' "
            "WHERE answering_mode IS NULL OR answering_mode = ''"
        )


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
    return DB_PATH


# --- users ---------------------------------------------------------------

def create_user(email, password_hash, business_name, mobile_number=None):
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, business_name, mobile_number) "
            "VALUES (?, ?, ?, ?)",
            (email.strip().lower(), password_hash, business_name.strip(),
             (mobile_number or "").strip() or None),
        )
        return cur.lastrowid


def get_user_by_email(email):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email = ?", ((email or "").strip().lower(),)
        ).fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def first_user():
    """The single-tenant fallback owner, used by the send_summary handler."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM users ORDER BY id LIMIT 1").fetchone()
    return dict(row) if row else None


def update_user_profile(user_id, business_name=None, mobile_number=None):
    fields, values = [], []
    if business_name is not None:
        fields.append("business_name = ?")
        values.append(business_name.strip())
    if mobile_number is not None:
        fields.append("mobile_number = ?")
        values.append(mobile_number.strip() or None)
    if not fields:
        return
    values.append(user_id)
    with connect() as conn:
        conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", values)


# --- sessions ------------------------------------------------------------

def create_session(user_id, token):
    with connect() as conn:
        conn.execute(
            "INSERT INTO sessions (token, user_id) VALUES (?, ?)", (token, int(user_id))
        )
    return token


def get_session_user(token):
    """The user behind a session token, or None. Expired tokens are cleaned up."""
    if not token:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT s.token, s.created_at, u.* FROM sessions s "
            "JOIN users u ON u.id = s.user_id WHERE s.token = ?",
            (token,),
        ).fetchone()
        if not row:
            return None
        created = str(row["created_at"] or "")
        # "YYYY-MM-DD HH:MM:SS" is UTC, so a plain string compare is correct
        # against the same format produced by datetime.utcnow.
        cutoff = utcnow_minus(SESSION_MAX_AGE_DAYS).strftime("%Y-%m-%d %H:%M:%S")
        if created and created < cutoff:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            return None
    user = dict(row)
    user.pop("token", None)
    return user


def delete_session(token):
    if not token:
        return
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def purge_expired_sessions(days=SESSION_MAX_AGE_DAYS):
    cutoff = utcnow_minus(days).strftime("%Y-%m-%d %H:%M:%S")
    with connect() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE created_at < ?", (cutoff,))
        return cur.rowcount


def utcnow_minus(days):
    from datetime import datetime, timedelta

    return datetime.utcnow() - timedelta(days=days)


# --- twilio --------------------------------------------------------------

def save_twilio_connection(user_id, account_sid):
    """One connection per user, so reconnecting replaces the old row."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO twilio_connections (user_id, account_sid) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET account_sid = excluded.account_sid, "
            "connected_at = CURRENT_TIMESTAMP",
            (int(user_id), account_sid),
        )
    return account_sid


def get_twilio_connection(user_id):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM twilio_connections WHERE user_id = ?", (int(user_id),)
        ).fetchone()
    return dict(row) if row else None


# --- phone numbers -------------------------------------------------------

def add_phone_number(user_id, phone_number, agent_id=None, twilio_sid=None,
                     answering_mode="agent", forward_to=None):
    with connect() as conn:
        existing = conn.execute(
            "SELECT id FROM phone_numbers WHERE user_id = ? AND phone_number = ?",
            (int(user_id), phone_number),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE phone_numbers SET agent_id = ?, twilio_sid = ? WHERE id = ?",
                (agent_id, twilio_sid, existing["id"]),
            )
            return existing["id"]
        cur = conn.execute(
            "INSERT INTO phone_numbers (user_id, phone_number, agent_id, twilio_sid, "
            "answering_mode, forward_to) VALUES (?, ?, ?, ?, ?, ?)",
            (int(user_id), phone_number, agent_id, twilio_sid, answering_mode, forward_to),
        )
        return cur.lastrowid


def list_phone_numbers(user_id):
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM phone_numbers WHERE user_id = ? "
            "ORDER BY created_at DESC, id DESC",
            (int(user_id),),
        ).fetchall()
    return [dict(r) for r in rows]


def get_phone_number(user_id, phone_number):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM phone_numbers WHERE user_id = ? AND phone_number = ?",
            (int(user_id), phone_number),
        ).fetchone()
    return dict(row) if row else None


def owner_of_number(phone_number):
    """Reverse lookup used by the send_summary tool handler."""
    if not phone_number:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM phone_numbers WHERE phone_number = ? ORDER BY id LIMIT 1",
            (phone_number.strip(),),
        ).fetchone()
    return dict(row) if row else None


def update_phone_number_mode(user_id, phone_number, answering_mode, forward_to=None):
    with connect() as conn:
        cur = conn.execute(
            "UPDATE phone_numbers SET answering_mode = ?, forward_to = ? "
            "WHERE user_id = ? AND phone_number = ?",
            (answering_mode, (forward_to or "").strip() or None,
             int(user_id), phone_number),
        )
        return cur.rowcount


# --- calls ---------------------------------------------------------------

def log_call(user_id, caller_name, callback_number, problem, urgency, transcript=None):
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO calls (user_id, caller_name, callback_number, problem, "
            "urgency, transcript) VALUES (?, ?, ?, ?, ?, ?)",
            (int(user_id), caller_name, callback_number, problem, urgency, transcript),
        )
        return cur.lastrowid


def list_calls(user_id, limit=None):
    sql = "SELECT * FROM calls WHERE user_id = ? ORDER BY created_at DESC, id DESC"
    params = [int(user_id)]
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_call_by_id(call_id, user_id=None):
    """Scoped by user when given, so one account cannot read another's call."""
    sql = "SELECT * FROM calls WHERE id = ?"
    params = [call_id]
    if user_id is not None:
        sql += " AND user_id = ?"
        params.append(int(user_id))
    with connect() as conn:
        row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


if __name__ == "__main__":
    print(init_db())
