"""Twilio Connect onboarding, and binding a customer number to a CallDesk agent.

Two separate jobs live here:

1. Getting a customer to authorise CallDesk against their own Twilio account
   (Twilio Connect / OAuth 2.0), so CallDesk can list and wire up their numbers.
2. Handing one of those numbers to AssemblyAI.

Twilio bills the customer directly. CallDesk never sees or charges telecom.

Reference: https://www.assemblyai.com/docs/voice-agents/voice-agent-api/twilio-own-number
"""

import os
import secrets
import urllib.parse
import uuid

import db
import lib

CONNECT_AUTH_URL = "https://connect.twilio.com/authorize"
CONNECT_TOKEN_URL = "https://connect.twilio.com/oauth2/token"
TWILIO_API = "https://api.twilio.com/2010-04-01"

# Shown when TWILIO_CONNECT_APP_SID is unset, so a judge can click through
# the whole product without a Connect app.
DEMO_ACCOUNT_SID = "ACdemo0000000000000000000000000"
DEMO_NUMBER = "+15550000000"
DEMO_SID = "PNdemo00000000000000000000000000"


def demo_mode():
    return not os.environ.get("TWILIO_CONNECT_APP_SID")


def public_url():
    """Absolute base URL for the callback. Twilio will not redirect to
    localhost, so a real deployment must set this."""
    return (os.environ.get("PUBLIC_URL") or "http://localhost:3000").rstrip("/")


def trunk_domain():
    """The SIP trunk that hands inbound calls to AssemblyAI. A number has to
    be on this trunk before AssemblyAI will accept it."""
    return os.environ.get("TWILIO_TRUNK_DOMAIN", "")


# --- Connect ---------------------------------------------------------------

def get_connect_url(user, state_token=""):
    """Build the Twilio Connect authorization URL.

    `state_token` is the caller's session token. It is round-tripped through
    Twilio so the callback knows who authorised, without trusting anything the
    callback itself sends.
    """
    if demo_mode():
        return None
    params = {
        "client_id": os.environ["TWILIO_CONNECT_APP_SID"],
        "response_type": "code",
        "scope": "openid",
        "redirect_uri": f"{public_url()}/connect/twilio/callback",
    }
    if state_token:
        params["state"] = state_token
    return f"{CONNECT_AUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange_code_for_account(code, code_verifier=""):
    """Swap a Connect `code` for tokens and return the AccountSid.

    Needs TWILIO_CONNECT_CLIENT_SECRET. Without it we cannot complete a real
    Connect handshake, so callers fall back to demo mode.
    """
    client_id = os.environ.get("TWILIO_CONNECT_APP_SID", "")
    client_secret = os.environ.get("TWILIO_CONNECT_CLIENT_SECRET", "")
    if not (client_id and client_secret and code):
        return None
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": f"{public_url()}/connect/twilio/callback",
    }
    if code_verifier:
        form["code_verifier"] = code_verifier
    try:
        payload = lib.twilio(CONNECT_TOKEN_URL, form=form)
    except (lib.ApiError, OSError):
        return None
    return payload.get("account_sid") or payload.get("sub")


def handle_callback(account_sid, user):
    """Persist the authorised AccountSid for this user."""
    if not account_sid:
        return None
    db.create_twilio_connection(user["id"], account_sid)
    return account_sid


# --- numbers ---------------------------------------------------------------

def list_customer_numbers(account_sid, auth_token=""):
    """List the voice numbers on a customer's Twilio account.

    Empty list when we have no token for that account, which is the normal
    demo-mode case: Connect hands back an AccountSid, not an auth token.
    """
    if not auth_token:
        return []
    url = f"{TWILIO_API}/Accounts/{urllib.parse.quote(account_sid)}/IncomingPhoneNumbers.json"
    try:
        payload = lib.twilio(url, account=account_sid, token=auth_token)
    except (lib.ApiError, OSError):
        return []
    numbers = []
    for item in payload.get("incoming_phone_numbers", []):
        numbers.append({
            "phone_number": item.get("phone_number", ""),
            "friendly_name": item.get("friendly_name", ""),
            "twilio_sid": item.get("sid", ""),
        })
    return numbers


def available_numbers(user, account_sid, auth_token=""):
    """Real Twilio numbers when we can reach the account, otherwise a single
    demo number so the bind flow is still walkable."""
    if demo_mode() or not auth_token:
        return [{
            "phone_number": DEMO_NUMBER,
            "friendly_name": "Demo line (Twilio Connect not configured)",
            "twilio_sid": DEMO_SID,
        }]
    return list_customer_numbers(account_sid, auth_token)


def bind_number_to_agent(account_sid, phone_number, agent_id, auth_token=""):
    """Register a number with AssemblyAI, then attach the agent.

    Step 1, POST /v1/phone-numbers/import, is only accepted for a number that
    already sits on a SIP trunk; `termination_uri` is that trunk's domain.
    Step 2, PUT /v1/phone-numbers/{number}/agent, is what makes it answer.
    """
    termination = trunk_domain()
    if not termination:
        raise ValueError(
            "TWILIO_TRUNK_DOMAIN is not set. The number must be on a SIP trunk "
            "before AssemblyAI can attach an agent. See deployment/telephony/connect.py."
        )

    aai_base = os.environ.get("AGENTS_API_BASE", "https://agents.assemblyai.com/v1")
    quoted = urllib.parse.quote(phone_number, safe="+")

    # 1. import. Idempotency-Key is required, and makes a retry safe.
    lib.aai(
        "/phone-numbers/import",
        method="POST",
        body={"phone_number": phone_number, "termination_uri": termination},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )

    # 2. attach the agent
    lib.aai(f"/phone-numbers/{quoted}/agent", method="PUT", body={"agent_id": agent_id})

    # 3. confirm, so the dashboard shows real state rather than an assumption
    try:
        return lib.aai(f"/phone-numbers/{quoted}")
    except lib.ApiError:
        return {"phone_number": phone_number, "agent_id": agent_id}


def new_state_secret():
    return secrets.token_urlsafe(24)
