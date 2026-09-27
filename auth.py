"""Password hashing and cookie sessions. Standard library only.

Sessions live in memory, so restarting the server logs everyone out. That is
acceptable for a hackathon demo and keeps this to zero dependencies.
"""

import hashlib
import hmac
import secrets
import threading
import time
from http.cookies import SimpleCookie

import db

# OWASP's floor for PBKDF2-HMAC-SHA256. 260k keeps interactive logins under
# ~100ms on a laptop while staying far above a trivial hash.
ITERATIONS = 260_000
ALGORITHM = "pbkdf2_sha256"

COOKIE_NAME = "calldesk_session"
SESSION_TTL_SECONDS = 60 * 60 * 12  # 12 hours

# token -> {"user_id": int, "expires": float}
SESSIONS = {}
_LOCK = threading.Lock()


# --- passwords -----------------------------------------------------------

def hash_password(password):
    """Return a self-describing hash string.

    Format: pbkdf2_sha256$ITERATIONS$SALT_HEX$DK_HEX

    The salt is stored alongside the digest, so the whole string is
    self-contained. A bare unsalted hex digest would be one rainbow table
    away from cracking every user at once, which is why this is not just
    `pbkdf2_hmac(...).hex()`.
    """
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, ITERATIONS
    )
    return f"{ALGORITHM}${ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password, stored):
    """Constant-time check. Returns False on any malformed input."""
    if not stored or not isinstance(stored, str):
        return False
    parts = stored.split("$")
    if len(parts) != 4 or parts[0] != ALGORITHM:
        return False
    try:
        iterations = int(parts[1])
        salt = bytes.fromhex(parts[2])
        expected = bytes.fromhex(parts[3])
    except (ValueError, TypeError):
        return False
    if not salt or not expected:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


# --- sessions ------------------------------------------------------------

def create_session(user_id):
    token = secrets.token_urlsafe(32)
    with _LOCK:
        SESSIONS[token] = {
            "user_id": int(user_id),
            "expires": time.time() + SESSION_TTL_SECONDS,
        }
    return token


def get_session_user(token):
    """Return the user row dict for a valid token, else None."""
    if not token:
        return None

    now = time.time()
    with _LOCK:
        entry = SESSIONS.get(token)
        if not entry:
            return None
        if entry["expires"] < now:
            SESSIONS.pop(token, None)
            return None
    return db.get_user_by_id(entry["user_id"])


def delete_session(token):
    if not token:
        return
    with _LOCK:
        SESSIONS.pop(token, None)


# --- cookies -------------------------------------------------------------

def token_from_cookie(header):
    """Pull the session token out of a raw Cookie header, or None."""
    if not header:
        return None
    jar = SimpleCookie()
    try:
        jar.load(header)
    except Exception:
        return None
    morsel = jar.get(COOKIE_NAME)
    return morsel.value if morsel else None


def set_cookie(token, max_age=SESSION_TTL_SECONDS):
    """Build a Set-Cookie value. Not Secure: local runs over plain http."""
    return (
        f"{COOKIE_NAME}={token}; Path=/; Max-Age={max_age}; "
        "HttpOnly; SameSite=Lax"
    )


def clear_cookie():
    return f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
