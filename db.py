"""SQLite storage for CallDesk. Standard library only, no ORM.

Every helper opens its own short-lived connection and closes it. The server
runs ThreadingHTTPServer, and sqlite3 connections are not safe to share
across threads, so pooling would buy nothing for a demo-scale app.
"""

import os
import sqlite3

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.join(ROOT, "data")
DB_PATH = os.environ.get("CALLDESK_DB", os.path.join(DB_DIR, "calldesk.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    email         TEXT UNIQUE,
    password_hash TEXT,
    business_name TEXT,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS twilio_connections (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER,
    account_sid TEXT,
    auth_token  TEXT,
    connected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS phone_numbers (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER,
    phone_number TEXT,
    agent_id     TEXT,
    twilio_sid   TEXT,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
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

CREATE INDEX IF NOT EXISTS idx_numbers_user  ON phone_numbers (user_id);
CREATE INDEX IF NOT EXISTS idx_calls_user    ON calls (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_twilio_user   ON twilio_connections (user_id);
"""


def connect():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)
    return DB_PATH


# --- users ---------------------------------------------------------------

def create_user(email, password_hash, business_name):
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, business_name) VALUES (?, ?, ?)",
            (email.strip().lower(), password_hash, business_name.strip()),
        )
        return cur.lastrowid


def get_user_by_email(email):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE email = ?", (email.strip().lower(),)
        ).fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def first_user():
    """The demo fallback owner. PART 5 routes unmatched calls here."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM users ORDER BY id LIMIT 1").fetchone()
    return dict(row) if row else None


# --- twilio --------------------------------------------------------------

def create_twilio_connection(user_id, account_sid, auth_token=None):
    with connect() as conn:
        conn.execute(
            "INSERT INTO twilio_connections (user_id, account_sid, auth_token) "
            "VALUES (?, ?, ?)",
            (user_id, account_sid, auth_token),
        )
    return account_sid


def get_twilio_connection(user_id):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM twilio_connections WHERE user_id = ? "
            "ORDER BY connected_at DESC, id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


# --- phone numbers -------------------------------------------------------

def add_phone_number(user_id, phone_number, agent_id=None, twilio_sid=None):
    with connect() as conn:
        existing = conn.execute(
            "SELECT id FROM phone_numbers WHERE user_id = ? AND phone_number = ?",
            (user_id, phone_number),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE phone_numbers SET agent_id = ?, twilio_sid = ? WHERE id = ?",
                (agent_id, twilio_sid, existing["id"]),
            )
            return existing["id"]
        cur = conn.execute(
            "INSERT INTO phone_numbers (user_id, phone_number, agent_id, twilio_sid) "
            "VALUES (?, ?, ?, ?)",
            (user_id, phone_number, agent_id, twilio_sid),
        )
        return cur.lastrowid


def list_phone_numbers(user_id):
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM phone_numbers WHERE user_id = ? ORDER BY created_at DESC, id DESC",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def owner_of_number(phone_number):
    """Reverse lookup used by the send_summary tool call handler."""
    if not phone_number:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM phone_numbers WHERE phone_number = ? ORDER BY id LIMIT 1",
            (phone_number.strip(),),
        ).fetchone()
    return dict(row) if row else None


# --- calls ---------------------------------------------------------------

def log_call(user_id, caller_name, callback_number, problem, urgency, transcript=None):
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO calls (user_id, caller_name, callback_number, problem, "
            "urgency, transcript) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, caller_name, callback_number, problem, urgency, transcript),
        )
        return cur.lastrowid


def list_calls(user_id, limit=None):
    sql = "SELECT * FROM calls WHERE user_id = ? ORDER BY created_at DESC, id DESC"
    params = [user_id]
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    print(init_db())
