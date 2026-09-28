"""Google OAuth 2.0 sign-in. Standard library only, no pip install.

Why no google-auth: verifying a JWT signature needs RSA, which the standard
library does not implement. google-auth exists for exactly that. Rather than
add a dependency, this verifies the token Google's own introspection endpoint
already validated, and reads the profile from Google's userinfo endpoint.

  code  -> POST https://oauth2.googleapis.com/token  -> access_token
  access_token -> GET https://www.googleapis.com/oauth2/v3/userinfo -> {email, sub}

The access token came from Google over TLS in exchange for our client secret, so
the userinfo response for it is authentic. That is the same guarantee the
audience and signature checks would give, with nothing to install.

Reference: https://developers.google.com/identity/protocols/oauth2/web-server
"""

import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request

# Where `state` lives between the redirect out and the callback back. A short
# TTL keeps the table from growing without bound.
STATE_TTL_SECONDS = 600

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
SCOPE = "openid email profile"

_STATES = {}


class GoogleAuthError(Exception):
    """Anything that stops a Google sign-in. Message is shown to the user."""


def configured():
    return bool(client_id() and client_secret())


def client_id():
    return os.environ.get("GOOGLE_CLIENT_ID", "").strip()


def client_secret():
    return os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()


def base_url():
    configured_url = os.environ.get("CALLDESK_BASE_URL", "").strip()
    return configured_url.rstrip("/")


def redirect_uri(request_host=""):
    """Where Google sends the customer back. Must match the OAuth client
    exactly, registered character for character in the Cloud Console."""
    base = base_url()
    if not base and request_host:
        scheme = "https" if os.environ.get("FORCE_HTTPS") else "http"
        base = f"{scheme}://{request_host}"
    if not base:
        raise GoogleAuthError(
            "CALLDESK_BASE_URL is not set, so the Google redirect URI is unknown.")
    return f"{base}/auth/google/callback"


def get_google_auth_url(state, request_host=""):
    """The consent-screen URL. `state` is single-use and expires."""
    if not configured():
        raise GoogleAuthError(
            "Google sign-in is not configured. Set GOOGLE_CLIENT_ID and "
            "GOOGLE_CLIENT_SECRET in .env.")
    token = secrets.token_urlsafe(24)
    _STATES[token] = {"state": state, "expires": _now() + STATE_TTL_SECONDS}
    params = {
        "client_id": client_id(),
        "redirect_uri": redirect_uri(request_host),
        "response_type": "code",
        "scope": SCOPE,
        # Forces the consent screen every time, so a returning customer always
        # lands back on the account we expect.
        "prompt": "select_account",
        "access_type": "offline",
        "state": token,
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def consume_state(token):
    """Check and burn a state token. One time use, and it expires."""
    entry = _STATES.pop(token, None) if token else None
    if not entry:
        raise GoogleAuthError("That sign-in link has expired. Please try again.")
    if entry["expires"] < _now():
        raise GoogleAuthError("That sign-in link has expired. Please try again.")
    return entry["state"]


def _now():
    import time

    return time.time()


def _post(url, form):
    data = urllib.parse.urlencode(form).encode()
    request = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as err:
        try:
            detail = json.loads(err.read().decode())
            message = detail.get("error_description") or detail.get("error") or ""
        except (ValueError, OSError):
            message = ""
        raise GoogleAuthError(
            f"Google rejected the sign-in ({err.code}){': ' + message if message else ''}"
        ) from None
    except (urllib.error.URLError, OSError) as err:
        raise GoogleAuthError(f"Could not reach Google: {err}") from None


def _get(url, token):
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as err:
        raise GoogleAuthError(
            "Google did not confirm that sign-in. Please try again.") from None
    except (urllib.error.URLError, OSError) as err:
        raise GoogleAuthError(f"Could not reach Google: {err}") from None


def exchange_code_for_user(code, request_host=""):
    """Trade the authorization code for a profile.

    Returns {email, google_id, name}. Raises GoogleAuthError with a message
    meant for the login page.
    """
    if not code:
        raise GoogleAuthError("Google did not send an authorization code.")
    if not configured():
        raise GoogleAuthError(
            "Google sign-in is not configured. Set GOOGLE_CLIENT_ID and "
            "GOOGLE_CLIENT_SECRET in .env.")

    tokens = _post(TOKEN_URL, {
        "code": code,
        "client_id": client_id(),
        "client_secret": client_secret(),
        "redirect_uri": redirect_uri(request_host),
        "grant_type": "authorization_code",
    })
    access_token = tokens.get("access_token")
    if not access_token:
        raise GoogleAuthError("Google did not return an access token.")

    profile = _get(USERINFO_URL, access_token)
    email = (profile.get("email") or "").strip().lower()
    google_id = (profile.get("sub") or "").strip()
    if not email:
        raise GoogleAuthError(
            "That Google account has no email address, so it cannot be used "
            "to sign in. Add one in Google and try again.")
    if not google_id:
        raise GoogleAuthError("Google did not return an account id.")
    if profile.get("email_verified") is False:
        raise GoogleAuthError("That Google account's email is not verified.")

    return {
        "email": email,
        "google_id": google_id,
        "name": (profile.get("name") or "").strip(),
    }


def find_or_create_google_user(email, google_id, business_name=""):
    """Sign a Google identity in, creating or linking the local account.

    Lives here so the caller has one import, but the rows live in db.py.
    """
    import db

    return db.find_or_create_google_user(email, google_id, business_name)
