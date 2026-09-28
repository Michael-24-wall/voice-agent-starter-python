"""Password hashing, session cookies, and the session cookie itself.

Standard library only. Session tokens are generated here and stored in the
`sessions` table by db.py, so a server restart does not log anyone out.
"""

import hashlib
import hmac
import secrets
from http.cookies import SimpleCookie

import db

# OWASP's floor for PBKDF2-HMAC-SHA256. 260k keeps interactive logins under
# ~100ms on a laptop while staying far above a trivial hash.
ITERATIONS = 260_000
ALGORITHM = "pbkdf2_sha256"

COOKIE_NAME = "calldesk_session"
COOKIE_MAX_AGE = db.SESSION_MAX_AGE_DAYS * 24 * 60 * 60


# --- passwords -----------------------------------------------------------

def hash_password(password):
    """Return a self-describing hash string.

    Format: pbkdf2_sha256$ITERATIONS$SALT_HEX$DK_HEX

    The salt is stored alongside the digest, so the string is self-contained
    and no per-user column is needed. A bare unsalted hex digest would be one
    rainbow table from cracking every user at once.
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
    """Mint a token and persist it. Returns the token for the cookie."""
    return db.create_session(user_id, secrets.token_urlsafe(32))


def get_session_user(token):
    return db.get_session_user(token)


def delete_session(token):
    db.delete_session(token)


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


def set_cookie(token):
    """Build a Set-Cookie value. Not Secure: local runs over plain http."""
    return (
        f"{COOKIE_NAME}={token}; Path=/; Max-Age={COOKIE_MAX_AGE}; "
        "HttpOnly; SameSite=Lax"
    )


def clear_cookie():
    return f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
