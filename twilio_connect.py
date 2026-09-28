"""Twilio Connect onboarding, binding a number to an agent, and answering modes.

Two separate jobs:

1. Getting a customer to authorise CallDesk against their own Twilio account
   (Twilio Connect), so CallDesk can list and wire up their numbers.
2. Pointing one of those numbers at the AssemblyAI voice agent, either directly
   or behind a "ring my mobile first" TwiML Bin.

Twilio bills the customer directly. CallDesk never charges telecom.

References:
  https://www.assemblyai.com/docs/voice-agents/voice-agent-api/twilio-own-number
  https://www.twilio.com/docs/voice/api/incoming-phone-numbers
  https://www.twilio.com/docs/voice/twiml/say
  https://www.twilio.com/docs/voice/twiml/dial
  https://www.twilio.com/docs/voice/api/twiml-bin
"""

import os
import urllib.parse
import uuid

import db
import lib

CONNECT_AUTH_URL = "https://connect.twilio.com/authorize"
TWILIO_API = "https://api.twilio.com/2010-04-01"

# Where AssemblyAI's SIP endpoint lives. Trunk origination URLs point here.
ASSEMBLYAI_SIP = "sip:sip.assemblyai.com"

# Mode B rings the mobile for this long before CallDesk takes over.
HUMAN_RING_SECONDS = 20

ANSWERING_MODES = ("agent", "human_first")

# Fallbacks shown when a customer has no real Twilio number. Never used to make
# a call, only so the pages have something to render.
DEMO_ACCOUNT_SID = "ACdemo0000000000000000000000000"
DEMO_NUMBER = "+15550000000"
DEMO_SID = "PNdemo00000000000000000000000000"


class TwilioError(Exception):
    """A Twilio call failed. Carries the message shown to the user."""


def _fail(label, err):
    raise TwilioError(f"{label}: {err}") from None


# --- configuration -------------------------------------------------------

def connect_app_sid():
    return os.environ.get("TWILIO_CONNECT_APP_SID", "").strip()


def connect_configured():
    return bool(connect_app_sid())


def base_url():
    """Absolute base URL for TwiML that Twilio will call back.

    CALLDESK_BASE_URL wins. On a local run it is unset, and the request's own
    Host header is the only thing that can be right; the caller passes it in.
    """
    configured = os.environ.get("CALLDESK_BASE_URL", "").strip()
    if configured:
        return configured.rstrip("/")
    return os.environ.get("PUBLIC_URL", "").strip().rstrip("/")


def resolve_base_url(request_host=""):
    configured = base_url()
    if configured:
        return configured
    if request_host:
        scheme = "https" if os.environ.get("FORCE_HTTPS") else "http"
        return f"{scheme}://{request_host}"
    return "http://localhost:3000"


def trunk_domain():
    """The SIP trunk that hands inbound calls to AssemblyAI. A number must be on
    a trunk before AssemblyAI will accept it."""
    return os.environ.get("TWILIO_TRUNK_DOMAIN", "").strip()


def list_available_agent_ids():
    """The CallDesk agent id. publish.py writes AGENT_ID_CALLDESK into .env."""
    from lib import load_env, stored_agent_id

    load_env()
    return os.environ.get("AGENT_ID_CALLDESK") or stored_agent_id("calldesk") or ""


# --- Connect -------------------------------------------------------------

def get_connect_url(user_id):
    """Twilio Connect authorization URL, carrying the user id in `state`.

    The Authorize URL configured in the Twilio Console is
    {CALLDESK_BASE_URL}/connect/twilio/callback, so Twilio sends the customer
    back here with an AccountSid.
    """
    app_sid = connect_app_sid()
    if not app_sid:
        return None
    params = {
        "client_id": app_sid,
        "response_type": "code",
        "state": str(user_id),
    }
    return f"{CONNECT_AUTH_URL}?{urllib.parse.urlencode(params)}"


def handle_authorize_callback(account_sid, state):
    """Save the authorised AccountSid for the user named in `state`.

    Returns the user dict. Raises TwilioError when the state does not name a
    real user, because that means the callback did not come from our flow.
    """
    try:
        user_id = int(str(state).strip())
    except (TypeError, ValueError):
        raise TwilioError("Twilio did not say which account to connect.") from None
    user = db.get_user_by_id(user_id)
    if not user:
        raise TwilioError("That sign-in link is no longer valid. Log in again.")
    if not account_sid:
        raise TwilioError("Twilio did not return an AccountSid.")
    db.save_twilio_connection(user_id, account_sid)
    print(f"twilio: {user['business_name']} connected as {account_sid}", flush=True)
    return user


# --- numbers -------------------------------------------------------------

def list_customer_numbers(account_sid):
    """Incoming voice numbers on the customer's Twilio account.

    Uses the customer's AccountSid with our TWILIO_AUTH_TOKEN. That reaches
    any account we are a subaccount of; for an unrelated Connect account the
    token is rejected with 401 and the page says so rather than crashing.
    """
    url = (f"{TWILIO_API}/Accounts/{urllib.parse.quote(account_sid)}"
           f"/IncomingPhoneNumbers.json?PageSize=100")
    try:
        payload = lib.twilio(url, account=account_sid)
    except lib.ApiError as err:
        _fail("Could not read your Twilio numbers", err)
    except OSError as err:
        _fail("Could not reach Twilio", err)
    numbers = []
    for item in (payload or {}).get("incoming_phone_numbers", []):
        numbers.append({
            "phone_number": item.get("phone_number", ""),
            "friendly_name": item.get("friendly_name", "") or "",
            "twilio_sid": item.get("sid", ""),
            "voice_url": item.get("voice_url", "") or "",
            "voice_method": item.get("voice_method", "POST") or "POST",
        })
    return numbers


def find_number(account_sid, phone_number):
    for number in list_customer_numbers(account_sid):
        if number["phone_number"] == phone_number:
            return number
    return None


def bind_number_to_agent(account_sid, phone_number, agent_id):
    """Register a number with AssemblyAI, then attach the agent.

    Step 1, POST /v1/phone-numbers/import, is only accepted for a number that
    already sits on a SIP trunk, and `termination_uri` is that trunk's domain.
    Step 2, PUT /v1/phone-numbers/{number}/agent, is what makes it answer.
    """
    termination = trunk_domain()
    if not termination:
        raise TwilioError(
            "TWILIO_TRUNK_DOMAIN is not set. The number must be on a SIP trunk "
            "before AssemblyAI can attach an agent. See deployment/telephony/connect.py."
        )
    quoted = urllib.parse.quote(phone_number, safe="+")
    try:
        # Idempotency-Key is required, and makes a retry safe.
        lib.aai(
            "/phone-numbers/import",
            method="POST",
            body={"phone_number": phone_number, "termination_uri": termination},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        lib.aai(f"/phone-numbers/{quoted}/agent", method="PUT",
                body={"agent_id": agent_id})
    except lib.ApiError as err:
        _fail(f"AssemblyAI could not bind {phone_number}", err)
    except OSError as err:
        _fail("Could not reach AssemblyAI", err)
    return {"phone_number": phone_number, "agent_id": agent_id}


# --- answering modes -----------------------------------------------------

def _twiml_url(account_sid):
    return f"{TWILIO_API}/Accounts/{urllib.parse.quote(account_sid)}"


def _find_sid(account_sid, phone_number):
    """The PN... sid for a number, in the customer's account."""
    number = find_number(account_sid, phone_number)
    if not number:
        raise TwilioError(f"{phone_number} is not an incoming number on this Twilio account.")
    return number


def _set_voice_url(account_sid, number_sid, voice_url, http_method="POST"):
    url = f"{_twiml_url(account_sid)}/IncomingPhoneNumbers/{number_sid}.json"
    try:
        lib.twilio(url, form={"VoiceUrl": voice_url, "VoiceMethod": http_method})
    except lib.ApiError as err:
        _fail("Could not point the number at CallDesk", err)
    except OSError as err:
        _fail("Could not reach Twilio", err)


def _upsert_bin(account_sid, friendly_name, twiml):
    """Create or replace a TwiML Bin holding `twiml`, and return its SID.

    Reusing one bin per number keeps the account from filling up with bins on
    every settings save.
    """
    sid = _find_bin(account_sid, friendly_name)
    url = f"{_twiml_url(account_sid)}/Twiml/Bins.json" if not sid else \
          f"{_twiml_url(account_sid)}/Twiml/Bins/{sid}.json"
    form = {"FriendlyName": friendly_name, "Twiml": twiml}
    try:
        result = lib.twilio(url, form=form)
    except lib.ApiError as err:
        _fail("Could not save the TwiML Bin", err)
    except OSError as err:
        _fail("Could not reach Twilio", err)
    return (result or {}).get("sid", "")


def _find_bin(account_sid, friendly_name):
    url = f"{_twiml_url(account_sid)}/Twiml/Bins.json?PageSize=100"
    try:
        payload = lib.twilio(url)
    except (lib.ApiError, OSError):
        return ""
    for item in (payload or {}).get("bins", []):
        if item.get("friendly_name") == friendly_name:
            return item.get("sid", "")
    return ""


def _bin_voice_url(account_sid, bin_sid):
    """The https URL Twilio fetches to render a TwiML Bin.

    Documented shape: the TwiML V1 path for the account that owns the bin,
    with BinSid naming it. The bin lives in the customer's account, so it is
    the customer's sid, not ours.
    """
    return (f"https://api.twilio.com/2010-04-01/Accounts/"
            f"{urllib.parse.quote(account_sid)}/Twiml/V1?BinSid={bin_sid}")


def set_number_answering_mode(account_sid, phone_number, mode, forward_to, base_url_):
    """Point a Twilio number at the agent, directly or behind a mobile ring.

    'agent'        voice URL is a Redirect straight to the AssemblyAI SIP
                   endpoint, so the call goes into the agent with no Twilio
                   media in the path.
    'human_first'  voice URL is a TwiML Bin that dials forward_to for 20s and
                   then redirects to /twiml/fallback, which redirects to SIP.
    """
    if mode not in ANSWERING_MODES:
        raise TwilioError(f"Unknown answering mode: {mode}")

    number = _find_sid(account_sid, phone_number)
    number_sid = number["twilio_sid"]

    if mode == "agent":
        _set_voice_url(account_sid, number_sid, f"{base_url_}/twiml/fallback?number={urllib.parse.quote(phone_number)}")
        print(f"twilio: {phone_number} -> CallDesk directly", flush=True)
        return {"mode": mode, "phone_number": phone_number}

    if not forward_to:
        raise TwilioError("A mobile number is required to ring the owner first.")

    quoted_number = urllib.parse.quote(phone_number)
    fallback_url = f"{base_url_}/twiml/fallback?number={quoted_number}"
    twiml = (
        "<Response>"
        f'<Dial timeout="{HUMAN_RING_SECONDS}" record="false">'
        f"<Number>{forward_to}</Number>"
        "</Dial>"
        f'<Redirect method="GET">{fallback_url}</Redirect>'
        "</Response>"
    )
    bin_sid = _upsert_bin(account_sid, f"CallDesk fallback {phone_number}", twiml)
    if not bin_sid:
        raise TwilioError("Twilio did not return a TwiML Bin SID.")
    _set_voice_url(account_sid, number_sid, _bin_voice_url(account_sid, bin_sid), "GET")
    print(f"twilio: {phone_number} -> ring {forward_to} for {HUMAN_RING_SECONDS}s "
          f"then CallDesk (bin {bin_sid})", flush=True)
    return {"mode": mode, "phone_number": phone_number, "bin_sid": bin_sid}


def fallback_twiml():
    """The TwiML that hands the call to AssemblyAI over SIP.

    A voice URL has to be http(s), so the SIP hand-off happens as a Redirect
    out of a TwiML document. This is what both answering modes end at.
    """
    return f"<Response><Redirect>{ASSEMBLYAI_SIP}</Redirect></Response>"


# --- outbound call-back --------------------------------------------------

def place_outbound_call(account_sid, to_number, from_number, callback_url):
    """Call a missed caller back, letting CallDesk speak first.

    The dial leg's URL is our TwiML, which redirects into the AssemblyAI SIP
    endpoint, so the owner hears the agent rather than a ringing phone.
    """
    url = f"{_twiml_url(account_sid)}/Calls.json"
    form = {
        "To": to_number,
        "From": from_number,
        "Url": callback_url,
    }
    try:
        result = lib.twilio(url, form=form)
    except lib.ApiError as err:
        _fail(f"Could not call {to_number} back", err)
    except OSError as err:
        _fail("Could not reach Twilio", err)
    return (result or {}).get("sid", "")


def demo_numbers():
    """A single fake number so the pages render before Twilio is connected."""
    return [{
        "phone_number": DEMO_NUMBER,
        "friendly_name": "Demo number",
        "twilio_sid": DEMO_SID,
        "voice_url": "",
        "voice_method": "POST",
    }]
