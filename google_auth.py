"""Google OAuth 2.0 sign-in, using google-auth and google-auth-oauthlib.

The authorization-code flow: build a consent URL, trade the returned code for
tokens, then verify the ID token and read the profile out of it.

Reference: https://developers.google.com/identity/protocols/oauth2/web-server
"""

import os
import secrets
import time
import uuid

import requests
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from google_auth_oauthlib.flow import Flow

# The scopes we need: an email address to key the account on, and the subject
# id that identifies this Google account for good.
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

# The redirect URI is registered once in the Cloud Console and has to match
# character for character, so it is built from one place only.
ACCESS_TYPE = "offline"
AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
REVOKE_URI = "https://oauth2.googleapis.com/revoke"

# How long an unused state token stays valid.
STATE_TTL_SECONDS = 600

# In-memory only. A restart drops these, which just means an in-flight
# sign-in has to be restarted, so nothing is written to disk.
_STATES = {}


class GoogleAuthError(Exception):
    """Anything that stops a Google sign-in. The message is shown to the
    customer, so it should read like an explanation, not a stack trace."""


def client_id():
    return os.environ.get("GOOGLE_CLIENT_ID", "").strip()


def client_secret():
    return os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()


def configured():
    """True when both halves of the OAuth client are present."""
    return bool(client_id() and client_secret())


def base_url():
    """The public origin of this CallDesk instance, no trailing slash."""
    return os.environ.get("CALLDESK_BASE_URL", "").strip().rstrip("/")


def redirect_uri():
    """Where Google sends the customer back. Must be registered in the Cloud
    Console exactly as written here."""
    base = base_url()
    if not base:
        raise GoogleAuthError(
            "CALLDESK_BASE_URL is not set, so the Google redirect URI cannot be "
            "built. Set it to this instance's public address.")
    return f"{base}/auth/google/callback"


def _client_config():
    return {
        "web": {
            "client_id": client_id(),
            "client_secret": client_secret(),
            "auth_uri": AUTH_URI,
            "token_uri": TOKEN_URI,
            "auth_provider_x509_cert_url": (
                "https://www.googleapis.com/oauth2/v1/certs"
            ),
            "userinfo_host": "https://www.googleapis.com",
            "revoke_uri": REVOKE_URI,
        }
    }


def get_google_auth_url(state, request_host=""):
    """The consent-screen URL for `state`.

    `state` is ours, not Google's: the value is carried through the round trip
    and checked on the way back, which is what stops someone else's
    authorization being attached to our session.
    """
    if not configured():
        raise GoogleAuthError(
            "Google sign-in is not configured. Set GOOGLE_CLIENT_ID and "
            "GOOGLE_CLIENT_SECRET in .env to enable it.")

    flow = Flow.from_client_config(_client_config(), scopes=SCOPES,
                                   redirect_uri=redirect_uri())
    auth_url, state_token = flow.authorization_url(
        access_type=ACCESS_TYPE,
        include_granted_scopes="true",
        prompt="consent",
    )
    _STATES[state_token] = {"state": state, "issued": time.time()}
    _expire_states()
    return auth_url


def consume_state(state_token):
    """Check a state token and throw it away.

    Single use, and only valid for STATE_TTL_SECONDS. Returning the stored
    value is what tells the caller who was signing in.
    """
    entry = _STATES.pop(state_token, None) if state_token else None
    if not entry:
        raise GoogleAuthError(
            "That Google sign-in link is no longer valid. Please try again.")
    if time.time() - entry["issued"] > STATE_TTL_SECONDS:
        raise GoogleAuthError(
            "That Google sign-in link has expired. Please try again.")
    return entry["state"]


def _expire_states():
    """Keep _STATES from growing without bound on a long-running process."""
    cutoff = time.time() - STATE_TTL_SECONDS
    for token in [t for t, e in _STATES.items() if e["issued"] < cutoff]:
        _STATES.pop(token, None)


def _fetch_tokens(code):
    """Swap the authorization code for tokens. Google must be given the same
    redirect_uri it was, or it rejects the exchange."""
    if not code:
        raise GoogleAuthError(
            "Google did not send an authorization code. Please try again.")
    if not configured():
        raise GoogleAuthError(
            "Google sign-in is not configured. Set GOOGLE_CLIENT_ID and "
            "GOOGLE_CLIENT_SECRET in .env to enable it.")

    flow = Flow.from_client_config(_client_config(), scopes=SCOPES,
                                   state=uuid.uuid4().hex,
                                   redirect_uri=redirect_uri())
    try:
        flow.fetch_token(code=code)
    except Exception as err:  # google-auth raises a wide range of errors
        raise GoogleAuthError(
            f"Google would not complete the sign-in ({err}). Please try again."
        ) from None
    return flow.credentials


def exchange_code_for_user(code, request_host=""):
    """Trade the authorization code for a verified profile.

    Returns {email, google_id, name}. `google_id` is Google's stable subject id
    for that account, which is what we key the link on, because an email
    address can change.
    """
    credentials = _fetch_tokens(code)
    if not credentials.id_token:
        raise GoogleAuthError(
            "Google did not return an ID token, so the sign-in could not be "
            "verified. Check that the openid scope is allowed.")

    # This is the check that makes the rest trustworthy: it verifies the ID
    # token's signature against Google's public keys, its audience against our
    # client id, and that it has not expired.
    try:
        claims = google_id_token.verify_oauth2_token(
            credentials.id_token, google_requests.Request(),
            client_id(),
        )
    except ValueError as err:
        raise GoogleAuthError(
            f"Google's sign-in could not be verified ({err}). Please try again."
        ) from None

    email = (claims.get("email") or "").strip().lower()
    google_id = (claims.get("sub") or "").strip()
    if not email:
        raise GoogleAuthError(
            "That Google account has no email address, so it cannot be used to "
            "sign in. Add one in Google and try again.")
    if not google_id:
        raise GoogleAuthError(
            "Google did not return an account id, so the sign-in cannot be "
            "matched to an account. Please try again.")
    if claims.get("email_verified") is False:
        raise GoogleAuthError(
            "That Google account's email address is not verified, so it cannot "
            "be used to sign in. Verify it in Google and try again.")

    return {"email": email, "google_id": google_id,
            "name": (claims.get("name") or "").strip()}


def find_or_create_google_user(email, google_id, business_name=""):
    """Sign a Google identity in, creating or linking the local account.

    If the email already has an account, the Google identity is linked to it
    rather than making a second one, so signing up twice does not orphan call
    history. A new Google-only account gets an empty password_hash, which
    means it can never be signed into with a password.
    """
    import db

    return db.find_or_create_google_user(email, google_id, business_name)
