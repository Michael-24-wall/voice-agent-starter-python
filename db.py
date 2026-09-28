"""SQLite storage for CallDesk. Standard library only, no ORM.

Every helper opens its own short-lived connection and closes it. The server
runs ThreadingHTTPServer, and sqlite3 connections are not safe to share
across threads, so pooling would buy nothing for a demo-scale app.

Sessions live in the database rather than in memory, so a restart does not log
everyone out.
"""

import os
import re
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
    google_id     TEXT UNIQUE,
    business_name TEXT,
    mobile_number TEXT,
    -- Which vertical this account runs. Drives the agent, the setup page and
    -- the tool endpoints. A row written before this column existed is a
    -- service business, which is what every earlier account was.
    business_type TEXT DEFAULT 'service',
    -- The AssemblyAI agent published for this account alone, by
    -- agent_manager.publish_user_agent(). NULL until they activate one.
    agent_id      TEXT,
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

-- --- vertical availability ---------------------------------------------
-- Every vertical answers "is there anything free, and can I book it" from
-- rows, not from a description. The agent calls a tool, the tool runs a
-- query, and the answer it speaks is the answer the database gave.

CREATE TABLE IF NOT EXISTS hotel_rooms (
    id              INTEGER PRIMARY KEY,
    user_id         INTEGER,
    room_number     TEXT,
    room_type       TEXT,
    price_per_night REAL,
    capacity        INTEGER,
    amenities       TEXT,
    status          TEXT DEFAULT 'available',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS hotel_bookings (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER,
    room_id    INTEGER,
    guest_name TEXT,
    guest_phone TEXT,
    check_in   DATE,
    check_out  DATE,
    status     TEXT DEFAULT 'confirmed',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users (id),
    FOREIGN KEY (room_id) REFERENCES hotel_rooms (id)
);

CREATE TABLE IF NOT EXISTS hospital_departments (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER,
    name        TEXT,
    description TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS hospital_slots (
    id              INTEGER PRIMARY KEY,
    user_id         INTEGER,
    department_id   INTEGER,
    doctor_name     TEXT,
    slot_datetime   TIMESTAMP,
    duration_minutes INTEGER DEFAULT 30,
    status          TEXT DEFAULT 'available',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users (id),
    FOREIGN KEY (department_id) REFERENCES hospital_departments (id)
);

CREATE TABLE IF NOT EXISTS hospital_appointments (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER,
    slot_id       INTEGER,
    patient_name  TEXT,
    patient_phone TEXT,
    reason        TEXT,
    status        TEXT DEFAULT 'confirmed',
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users (id),
    FOREIGN KEY (slot_id) REFERENCES hospital_slots (id)
);

CREATE TABLE IF NOT EXISTS restaurant_tables (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER,
    table_number TEXT,
    capacity     INTEGER,
    status       TEXT DEFAULT 'available',
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users (id)
);

CREATE TABLE IF NOT EXISTS restaurant_reservations (
    id                  INTEGER PRIMARY KEY,
    user_id             INTEGER,
    table_id            INTEGER,
    guest_name          TEXT,
    guest_phone         TEXT,
    party_size          INTEGER,
    reservation_datetime TIMESTAMP,
    status              TEXT DEFAULT 'confirmed',
    created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users (id),
    FOREIGN KEY (table_id) REFERENCES restaurant_tables (id)
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id);
CREATE INDEX IF NOT EXISTS idx_numbers_user  ON phone_numbers (user_id);
CREATE INDEX IF NOT EXISTS idx_calls_user    ON calls (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_rooms_user    ON hotel_rooms (user_id);
CREATE INDEX IF NOT EXISTS idx_bookings_user ON hotel_bookings (user_id, check_in);
CREATE INDEX IF NOT EXISTS idx_depts_user    ON hospital_departments (user_id);
CREATE INDEX IF NOT EXISTS idx_slots_user    ON hospital_slots (user_id, slot_datetime);
CREATE INDEX IF NOT EXISTS idx_appts_user    ON hospital_appointments (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tables_user   ON restaurant_tables (user_id);
CREATE INDEX IF NOT EXISTS idx_resv_user     ON restaurant_reservations (user_id, reservation_datetime);
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
    _add_column(conn, "users", "google_id", "TEXT")
    # Multi-vertical. Existing accounts are service businesses, which is what
    # they were before this column existed.
    _add_column(conn, "users", "business_type", "TEXT DEFAULT 'service'")
    _add_column(conn, "users", "agent_id", "TEXT")
    conn.execute(
        "UPDATE users SET business_type = 'service' "
        "WHERE business_type IS NULL OR business_type = ''")
    # An ALTER cannot carry UNIQUE, so the index is what enforces it. SQLite
    # treats each NULL as distinct, which is right: a password-only account
    # has no google_id.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google_id ON users (google_id)")

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

# The four verticals. A vertical decides the agent, the setup page, and which
# tool endpoints the agent's tools point at.
BUSINESS_TYPES = ("service", "hotel", "hospital", "restaurant")

BUSINESS_TYPE_LABELS = {
    "service": "Service Business",
    "hotel": "Hotel / Guesthouse",
    "hospital": "Hospital / Clinic",
    "restaurant": "Restaurant",
}


def normalize_business_type(value):
    """Anything unrecognized is a service business, never an error.

    A signup form is a public endpoint, so an unknown or missing value has to
    land somewhere that works rather than raise.
    """
    v = (value or "").strip().lower()
    return v if v in BUSINESS_TYPES else "service"


def create_user(email, password_hash, business_name, mobile_number=None,
                business_type="service"):
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, business_name, mobile_number, "
            "business_type) VALUES (?, ?, ?, ?, ?)",
            (email.strip().lower(), password_hash, business_name.strip(),
             (mobile_number or "").strip() or None,
             normalize_business_type(business_type)),
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


def first_user_of_type(business_type):
    """The oldest account of a vertical, for tool calls with no `to` number.

    A tool call normally arrives without the number that received the call, so
    this keeps a single-tenant demo answering. With several hotels on the
    account the answer would be wrong, which is why the handlers log when they
    fall back to it.
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE business_type = ? ORDER BY id LIMIT 1",
            (normalize_business_type(business_type),),
        ).fetchone()
    return dict(row) if row else None


def set_business_type(user_id, business_type):
    with connect() as conn:
        conn.execute("UPDATE users SET business_type = ? WHERE id = ?",
                     (normalize_business_type(business_type), int(user_id)))


def get_user_agent(user_id):
    """The agent published for this account alone, or None."""
    with connect() as conn:
        row = conn.execute("SELECT agent_id FROM users WHERE id = ?",
                           (int(user_id),)).fetchone()
    if not row:
        return None
    return (row["agent_id"] or "").strip() or None


def set_user_agent(user_id, agent_id):
    with connect() as conn:
        conn.execute("UPDATE users SET agent_id = ? WHERE id = ?",
                     ((agent_id or "").strip() or None, int(user_id)))


def get_user_by_google_id(google_id):
    """The account linked to a Google subject id, if any."""
    if not google_id:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE google_id = ?", (str(google_id).strip(),)
        ).fetchone()
    return dict(row) if row else None


def link_google_id(user_id, google_id):
    with connect() as conn:
        conn.execute("UPDATE users SET google_id = ? WHERE id = ?",
                     (str(google_id).strip(), int(user_id)))


def find_or_create_google_user(email, google_id, business_name=""):
    """Sign a Google identity in, linking to an existing account when the
    email matches one. A Google-only account stores password_hash='' so it
    can never be signed into with a password.
    """
    email = (email or "").strip().lower()
    existing = get_user_by_google_id(google_id) or get_user_by_email(email)
    if existing:
        if not (existing.get("google_id") or "").strip():
            link_google_id(existing["id"], google_id)
            print(f"google: linked {email} to account {existing['id']}", flush=True)
        return get_user_by_id(existing["id"])
    # Google only gives us a profile name, so use it, or fall back to the
    # local part of the address.
    name = (business_name or "").strip() or (
        email.split("@")[0] if email else "New account")
    user_id = create_user(email, "", name, None)
    link_google_id(user_id, google_id)
    print(f"google: created account {email} ({user_id})", flush=True)
    return get_user_by_id(user_id)


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


def list_calls(user_id, limit=None, offset=0, since=None):
    """Newest first. `since` is a 'YYYY-MM-DD' day string, `offset` pages."""
    sql = "SELECT * FROM calls WHERE user_id = ?"
    params = [int(user_id)]
    if since:
        sql += " AND created_at >= ?"
        params.append(str(since))
    sql += " ORDER BY created_at DESC, id DESC"
    if limit:
        sql += " LIMIT ? OFFSET ?"
        params += [int(limit), int(offset)]
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def count_calls(user_id, since=None):
    """How many calls a tenant has logged, optionally since a given day."""
    sql = "SELECT COUNT(*) FROM calls WHERE user_id = ?"
    params = [int(user_id)]
    if since:
        sql += " AND created_at >= ?"
        params.append(str(since))
    with connect() as conn:
        return conn.execute(sql, params).fetchone()[0]


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


"""A restaurant booking holds a table for this long.

A reservation has no checkout time, so two tables at 7:00pm and 8:00pm on the
same table are a double booking even though the timestamps differ. Bookings
closer together than this are treated as conflicting.
"""
RESERVATION_HOLD_MINUTES = 120

# Half-open date ranges: a guest leaving on the 3rd can check into the room
# freed on the 3rd, so overlap is a < b and b < a rather than <=.
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _check_date(value, field):
    v = (value or "").strip()
    if not DATE_RE.match(v):
        raise ValueError(f"{field} must look like 2026-10-01")
    return v


# --- hotel ----------------------------------------------------------------

def list_rooms(user_id):
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM hotel_rooms WHERE user_id = ? ORDER BY room_number, id",
            (int(user_id),),
        ).fetchall()
    return [dict(r) for r in rows]


def add_room(user_id, room_number, room_type, price_per_night, capacity, amenities=""):
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO hotel_rooms (user_id, room_number, room_type, price_per_night, "
            "capacity, amenities) VALUES (?, ?, ?, ?, ?, ?)",
            (int(user_id), room_number.strip(), room_type.strip(),
             float(price_per_night or 0), int(capacity or 1), (amenities or "").strip()),
        )
        return cur.lastrowid


def delete_room(user_id, room_id):
    """Remove an empty room. Scoped to the owner, so one hotel cannot delete
    another's room.

    A room that has bookings is refused rather than deleted: the guest stays
    booked, which is a record the hotel wants, and a cascade would quietly
    erase revenue history the first time somebody tidied up the room list.
    """
    with connect() as conn:
        room = conn.execute("SELECT room_number FROM hotel_rooms WHERE id = ? AND user_id = ?",
                            (int(room_id), int(user_id))).fetchone()
        if not room:
            raise ValueError("that room is not on your list")
        booked = conn.execute(
            "SELECT COUNT(*) FROM hotel_bookings WHERE room_id = ? "
            "AND COALESCE(status, 'confirmed') != 'cancelled'", (int(room_id),)).fetchone()[0]
        if booked:
            raise ValueError(
                f"room {room['room_number']} has {booked} booking(s) on it, "
                f"so it cannot be removed yet")
        cur = conn.execute("DELETE FROM hotel_rooms WHERE id = ? AND user_id = ?",
                           (int(room_id), int(user_id)))
        return cur.rowcount


def list_bookings(user_id, limit=20):
    with connect() as conn:
        rows = conn.execute(
            "SELECT b.*, r.room_number, r.room_type FROM hotel_bookings b "
            "LEFT JOIN hotel_rooms r ON r.id = b.room_id "
            "WHERE b.user_id = ? ORDER BY b.check_in, b.id DESC LIMIT ?",
            (int(user_id), int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def find_available_rooms(user_id, check_in, check_out, room_type=None):
    """Rooms with no booking that overlaps the requested stay.

    A room is unavailable when a confirmed booking overlaps the range. The
    room's own status only rules out maintenance: it deliberately does not
    encode occupancy, because occupancy belongs to a date range. Flipping
    status to 'occupied' on a booking would make the room look taken on every
    other date too, which is the bug this avoids.
    """
    check_in = _check_date(check_in, "check_in")
    check_out = _check_date(check_out, "check_out")
    if check_out <= check_in:
        raise ValueError("check_out has to be after check_in")
    sql = (
        "SELECT * FROM hotel_rooms r WHERE r.user_id = ? "
        "AND COALESCE(r.status, 'available') != 'maintenance' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM hotel_bookings b WHERE b.room_id = r.id "
        "  AND COALESCE(b.status, 'confirmed') != 'cancelled' "
        "  AND b.check_in < ? AND b.check_out > ?"
        ")"
    )
    params = [int(user_id), check_out, check_in]
    if (room_type or "").strip():
        sql += " AND LOWER(r.room_type) = LOWER(?)"
        params.append(room_type.strip())
    sql += " ORDER BY r.price_per_night, r.room_number"
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def book_room(user_id, room_number, guest_name, guest_phone, check_in, check_out):
    """Book a room, refusing if someone else got the overlapping dates first.

    BEGIN IMMEDIATE takes the write lock before the overlap check, so two
    simultaneous tool calls cannot both read 'free' and both insert.
    """
    check_in = _check_date(check_in, "check_in")
    check_out = _check_date(check_out, "check_out")
    if check_out <= check_in:
        raise ValueError("check_out has to be after check_in")
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        room = conn.execute(
            "SELECT * FROM hotel_rooms WHERE user_id = ? AND LOWER(room_number) = LOWER(?)",
            (int(user_id), (room_number or "").strip()),
        ).fetchone()
        if not room:
            raise ValueError(f"we do not have a room {room_number}")
        if (room["status"] or "available") == "maintenance":
            raise ValueError(f"room {room['room_number']} is out of service")
        clash = conn.execute(
            "SELECT 1 FROM hotel_bookings WHERE room_id = ? "
            "AND COALESCE(status, 'confirmed') != 'cancelled' "
            "AND check_in < ? AND check_out > ? LIMIT 1",
            (room["id"], check_out, check_in),
        ).fetchone()
        if clash:
            raise ValueError(
                f"room {room['room_number']} is already booked for those dates")
        cur = conn.execute(
            "INSERT INTO hotel_bookings (user_id, room_id, guest_name, guest_phone, "
            "check_in, check_out) VALUES (?, ?, ?, ?, ?, ?)",
            (int(user_id), room["id"], (guest_name or "").strip(),
             (guest_phone or "").strip(), check_in, check_out),
        )
        return {"booking_id": cur.lastrowid, "room_number": room["room_number"],
                "room_type": room["room_type"], "check_in": check_in,
                "check_out": check_out, "guest_name": (guest_name or "").strip()}


# --- hospital -------------------------------------------------------------

def list_departments(user_id):
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM hospital_departments WHERE user_id = ? ORDER BY name, id",
            (int(user_id),),
        ).fetchall()
    return [dict(r) for r in rows]


def add_department(user_id, name, description=""):
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO hospital_departments (user_id, name, description) VALUES (?, ?, ?)",
            (int(user_id), name.strip(), (description or "").strip()),
        )
        return cur.lastrowid


def list_slots(user_id, department_id=None, limit=50):
    sql = ("SELECT s.*, d.name AS department FROM hospital_slots s "
           "LEFT JOIN hospital_departments d ON d.id = s.department_id "
           "WHERE s.user_id = ?")
    params = [int(user_id)]
    if department_id:
        sql += " AND s.department_id = ?"
        params.append(int(department_id))
    sql += " ORDER BY s.slot_datetime, s.id LIMIT ?"
    params.append(int(limit))
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def add_slot(user_id, department_id, doctor_name, slot_datetime, duration_minutes=30):
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO hospital_slots (user_id, department_id, doctor_name, "
            "slot_datetime, duration_minutes) VALUES (?, ?, ?, ?, ?)",
            (int(user_id), int(department_id), (doctor_name or "").strip(),
             (slot_datetime or "").strip().replace("T", " "),
             int(duration_minutes or 30)),
        )
        return cur.lastrowid


def list_appointments(user_id, limit=20):
    with connect() as conn:
        rows = conn.execute(
            "SELECT a.*, s.slot_datetime, s.doctor_name, d.name AS department "
            "FROM hospital_appointments a "
            "LEFT JOIN hospital_slots s ON s.id = a.slot_id "
            "LEFT JOIN hospital_departments d ON d.id = s.department_id "
            "WHERE a.user_id = ? ORDER BY a.created_at DESC, a.id DESC LIMIT ?",
            (int(user_id), int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def find_available_slots(user_id, department, date):
    """Open slots in one department on one day.

    The department is matched case-insensitively because the model may say
    'cardiology' when the owner wrote 'Cardiology'. An unknown department is an
    error rather than an empty list, so the agent tells the caller that instead
    of claiming there are no appointments.
    """
    date = _check_date(date, "date")
    with connect() as conn:
        dept = conn.execute(
            "SELECT * FROM hospital_departments WHERE user_id = ? AND name = ? COLLATE NOCASE",
            (int(user_id), (department or "").strip()),
        ).fetchone()
        if not dept:
            known = [r["name"] for r in conn.execute(
                "SELECT name FROM hospital_departments WHERE user_id = ? ORDER BY name",
                (int(user_id),)).fetchall()]
            raise ValueError(
                f"we do not have a {department or 'that'} department"
                + (f". we have {', '.join(known)}" if known else ""))
        rows = conn.execute(
            "SELECT s.*, d.name AS department FROM hospital_slots s "
            "JOIN hospital_departments d ON d.id = s.department_id "
            "WHERE s.user_id = ? AND s.department_id = ? "
            "AND date(s.slot_datetime) = ? AND COALESCE(s.status, 'available') = 'available' "
            "ORDER BY s.slot_datetime",
            (int(user_id), dept["id"], date),
        ).fetchall()
    return [dict(r) for r in rows]


def book_appointment(user_id, department, doctor_name, patient_name, patient_phone,
                     slot_datetime, reason=""):
    """Claim a slot with one conditional UPDATE.

    The WHERE clause carries the test, so the row is only taken if it was still
    free when this statement ran. Checking first and then inserting would leave
    a window where two callers both saw the slot open.
    """
    when = (slot_datetime or "").strip().replace("T", " ")
    if not when:
        raise ValueError("I need a date and time for the appointment")
    with connect() as conn:
        row = conn.execute(
            "SELECT s.id, s.doctor_name, s.slot_datetime, d.name AS department "
            "FROM hospital_slots s JOIN hospital_departments d ON d.id = s.department_id "
            "WHERE s.user_id = ? AND d.name = ? COLLATE NOCASE "
            "AND datetime(s.slot_datetime) = datetime(?)"
            + (" AND LOWER(s.doctor_name) = LOWER(?)" if (doctor_name or "").strip() else ""),
            ([int(user_id), (department or "").strip(), when]
             + ([(doctor_name or "").strip()] if (doctor_name or "").strip() else [])),
        ).fetchone()
        if not row:
            raise ValueError("I could not find that appointment slot, please check the time")
        cur = conn.execute(
            "UPDATE hospital_slots SET status = 'booked' "
            "WHERE id = ? AND COALESCE(status, 'available') = 'available'",
            (row["id"],),
        )
        if cur.rowcount != 1:
            raise ValueError("that slot was just taken, please pick another time")
        appt = conn.execute(
            "INSERT INTO hospital_appointments (user_id, slot_id, patient_name, "
            "patient_phone, reason) VALUES (?, ?, ?, ?, ?)",
            (int(user_id), row["id"], (patient_name or "").strip(),
             (patient_phone or "").strip(), (reason or "").strip()),
        )
        return {"appointment_id": appt.lastrowid, "department": row["department"],
                "doctor_name": row["doctor_name"],
                "slot_datetime": row["slot_datetime"],
                "patient_name": (patient_name or "").strip()}


# --- restaurant -----------------------------------------------------------

def list_tables(user_id):
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM restaurant_tables WHERE user_id = ? "
            "ORDER BY CAST(REPLACE(table_number, 'T', '') AS INTEGER), table_number",
            (int(user_id),),
        ).fetchall()
    return [dict(r) for r in rows]


def add_table(user_id, table_number, capacity):
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO restaurant_tables (user_id, table_number, capacity) VALUES (?, ?, ?)",
            (int(user_id), table_number.strip(), int(capacity)),
        )
        return cur.lastrowid


def delete_table(user_id, table_id):
    """Remove a table that has no reservations on it, for the same reason
    delete_room refuses: a cancelled booking is still a booking."""
    with connect() as conn:
        table = conn.execute(
            "SELECT table_number FROM restaurant_tables WHERE id = ? AND user_id = ?",
            (int(table_id), int(user_id))).fetchone()
        if not table:
            raise ValueError("that table is not on your list")
        booked = conn.execute(
            "SELECT COUNT(*) FROM restaurant_reservations WHERE table_id = ? "
            "AND COALESCE(status, 'confirmed') != 'cancelled'", (int(table_id),)).fetchone()[0]
        if booked:
            raise ValueError(
                f"table {table['table_number']} has {booked} reservation(s) on it, "
                f"so it cannot be removed yet")
        cur = conn.execute("DELETE FROM restaurant_tables WHERE id = ? AND user_id = ?",
                           (int(table_id), int(user_id)))
        return cur.rowcount


def list_reservations(user_id, limit=20):
    with connect() as conn:
        rows = conn.execute(
            "SELECT v.*, t.table_number, t.capacity FROM restaurant_reservations v "
            "LEFT JOIN restaurant_tables t ON t.id = v.table_id "
            "WHERE v.user_id = ? ORDER BY v.reservation_datetime, v.id DESC LIMIT ?",
            (int(user_id), int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def find_available_tables(user_id, when, party_size):
    """Tables that seat the party and are free around that time.

    Reservation times arrive from speech as '7pm', so any time within
    RESERVATION_HOLD_MINUTES of an existing booking counts as a conflict.
    """
    when = (when or "").strip().replace("T", " ")
    if not when:
        raise ValueError("I need a date and time for the reservation")
    try:
        size = int(party_size)
    except (TypeError, ValueError):
        raise ValueError("how many people are coming?")
    if size < 1:
        raise ValueError("how many people are coming?")
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM restaurant_tables t WHERE t.user_id = ? "
            "AND t.capacity >= ? AND COALESCE(t.status, 'available') != 'out_of_service' "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM restaurant_reservations v WHERE v.table_id = t.id "
            "  AND COALESCE(v.status, 'confirmed') != 'cancelled' "
            "  AND ABS(strftime('%s', v.reservation_datetime) - strftime('%s', ?)) <= ?"
            ")"
            " ORDER BY t.capacity, CAST(REPLACE(t.table_number, 'T', '') AS INTEGER)",
            (int(user_id), size, when, RESERVATION_HOLD_MINUTES * 60),
        ).fetchall()
    return [dict(r) for r in rows]


def book_table(user_id, table_number, guest_name, guest_phone, party_size,
               reservation_datetime):
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        table = conn.execute(
            "SELECT * FROM restaurant_tables WHERE user_id = ? "
            "AND LOWER(table_number) = LOWER(?)",
            (int(user_id), (table_number or "").strip()),
        ).fetchone()
        if not table:
            raise ValueError(f"we do not have a table {table_number}")
        try:
            size = int(party_size)
        except (TypeError, ValueError):
            raise ValueError("how many people are coming?")
        if table["capacity"] < size:
            raise ValueError(
                f"table {table['table_number']} seats {table['capacity']}, "
                f"so it will not fit {size}")
        when = (reservation_datetime or "").strip().replace("T", " ")
        clash = conn.execute(
            "SELECT 1 FROM restaurant_reservations WHERE table_id = ? "
            "AND COALESCE(status, 'confirmed') != 'cancelled' "
            "AND ABS(strftime('%s', reservation_datetime) - strftime('%s', ?)) <= ? LIMIT 1",
            (table["id"], when, RESERVATION_HOLD_MINUTES * 60),
        ).fetchone()
        if clash:
            raise ValueError(f"table {table['table_number']} is already taken then")
        cur = conn.execute(
            "INSERT INTO restaurant_reservations (user_id, table_id, guest_name, "
            "guest_phone, party_size, reservation_datetime) VALUES (?, ?, ?, ?, ?, ?)",
            (int(user_id), table["id"], (guest_name or "").strip(),
             (guest_phone or "").strip(), size, when),
        )
        return {"reservation_id": cur.lastrowid, "table_number": table["table_number"],
                "capacity": table["capacity"], "party_size": size,
                "reservation_datetime": when, "guest_name": (guest_name or "").strip()}


# --- dashboard counts -----------------------------------------------------

def business_counts(user_id):
    """Row counts per vertical, so the dashboard can show a hotel's rooms and
    bookings without a second query per table."""
    uid = int(user_id)
    with connect() as conn:
        def count(table, extra=""):
            return conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE user_id = ? {extra}", (uid,)
            ).fetchone()[0]
        return {
            "rooms": count("hotel_rooms"),
            "room_types": conn.execute(
                "SELECT COUNT(DISTINCT LOWER(room_type)) FROM hotel_rooms WHERE user_id = ?",
                (uid,)).fetchone()[0],
            "bookings": count("hotel_bookings"),
            "departments": count("hospital_departments"),
            "slots": count("hospital_slots"),
            "slots_available": count("hospital_slots", "AND COALESCE(status,'available')='available'"),
            "appointments": count("hospital_appointments"),
            "tables": count("restaurant_tables"),
            "reservations": count("restaurant_reservations"),
        }


if __name__ == "__main__":
    print(init_db())
