#!/usr/bin/env python3
"""CallDesk: a voice AI receptionist for local service businesses.

    python deployment/browser/server.py

Three kinds of thing live in this file:

  /talk /token /agent /app.js   the voice UI and the plumbing it needs
  /tool/send_summary            where the agent posts a finished call
  /twiml/*                      TwiML Twilio fetches to route a call
  everything else               the SaaS: signup, numbers, answering, calls

The API key stays in this process; the page only gets 60-second tokens.
"""

import copy
import html
import json
import os
import re
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

import auth  # noqa: E402
import db  # noqa: E402
import twilio_connect as tc  # noqa: E402
from lib import (ApiError, aai, load_env, publish_agent, read_agent,  # noqa: E402
                 required, stored_agent_id)

TEMPLATES = ROOT / "templates"


def resolve_agent() -> dict:
    """A published id means the agent is managed elsewhere, so use it as it is."""
    name = os.environ.get("AGENT", "minimal")
    known = stored_agent_id(name)
    if known:
        try:
            agent = aai(f"/agents/{known}")
        except ApiError as err:
            sys.exit(f"Could not load agent {known}: {err}")
        return {"id": known, "name": agent.get("name") or "Your agent"}
    agent = read_agent(name)
    try:
        result = publish_agent(agent, name=name, reuse_by_name=True)
    except ApiError as err:
        sys.exit(f"Could not publish agents/{name}.jsonc: {err}")
    verb = "Created" if result["created"] else "Updated"
    print(f'{verb} "{agent["name"]}" from agents/{name}.jsonc')
    return {"id": result["id"], "name": agent["name"]}


def public_agent(agent: dict) -> dict:
    """Read-only view of the stored agent. The API keeps header values and llm
    keys write-only; these deletes hold even if that changes. The system prompt
    is in here, so a public deployment shows it to anyone who opens the page."""
    copied = copy.deepcopy(agent)
    for tool in copied.get("tools", []):
        # The API returns "http": null on a tool that has no http block, which
        # is not the same as the key being absent, so `or {}` rather than a
        # dict default.
        for header in (tool.get("http") or {}).get("headers", []):
            header["value"] = "<hidden>"
    for llm in copied.get("llm", []):
        llm.pop("api_key", None)
    return copied


# --- templating ----------------------------------------------------------

def esc(value) -> str:
    """Everything a user typed goes through this before reaching HTML."""
    return html.escape("" if value is None else str(value), quote=True)


_SLOT = re.compile(r"\{\{([A-Z_]+)\}\}")

# Slots holding HTML built in this file; every other slot gets escaped.
RAW = {"CONTENT", "NAV", "NUMBERS", "AVAILABLE", "RECENT_CALLS", "CALLS",
       "TWILIO_BANNER", "ERROR"}


def fill(source: str, **values) -> str:
    """str.replace templating, escaping every leaf.

    Slots whose name is in RAW are inserted as trusted HTML fragments, which is
    how a table body gets built without its cells being escaped twice. Anything
    not supplied becomes an empty string, so a template never leaks {{SLOT}}.
    """
    def swap(match):
        key = match.group(1)
        if key not in values:
            return ""
        return str(values[key]) if key in RAW else esc(values[key])

    return _SLOT.sub(swap, source)


def page(name: str, title: str, nav: str, **values) -> bytes:
    content = fill((TEMPLATES / name).read_text(encoding="utf-8"), **values)
    shell = fill((TEMPLATES / "base.html").read_text(encoding="utf-8"),
                 TITLE=title, NAV=nav, CONTENT=content)
    return shell.encode("utf-8")


# --- view fragments ------------------------------------------------------

URGENCIES = ("high", "medium", "low")
CONNECT_NOT_CONFIGURED = "Twilio Connect not configured. Set TWILIO_CONNECT_APP_SID in .env."


def urgency_class(value) -> str:
    v = (value or "").strip().lower()
    return v if v in URGENCIES else "medium"


def nav_for(user) -> str:
    if user:
        return (
            '<nav>'
            '<a href="/dashboard">Dashboard</a>'
            '<a href="/numbers">Numbers</a>'
            '<a href="/calls">Calls</a>'
            '<a href="/talk" target="_blank" rel="noopener">Talk</a>'
            f'<span class="muted" style="font-size:13px">{esc(user["business_name"])}</span>'
            '<form method="post" action="/logout">'
            '<button class="btn sm" type="submit">Log out</button>'
            "</form>"
            "</nav>"
        )
    return '<nav><a href="/login">Log in</a><a href="/signup">Sign up</a></nav>'


def note(text, kind=""):
    css = f"note {kind}".strip()
    return f'<div class="{css}">{text}</div>'


def error_box(message):
    return note(f"<b>Something went wrong.</b> {esc(message)}", "err") if message else ""


def numbers_table(user_id):
    rows = db.list_phone_numbers(user_id)
    if not rows:
        return '<p class="empty">No numbers bound yet.</p>'
    cells = []
    for r in rows:
        mode = (r.get("answering_mode") or "agent").strip()
        label = "Mode A" if mode == "agent" else "Mode B"
        pill = "ok" if mode == "agent" else "medium"
        number = esc(r["phone_number"])
        cells.append(
            f'<tr><td class="mono">{number}</td>'
            f'<td><span class="pill {pill}">{label}</span></td>'
            f'<td class="mono muted">{esc((r.get("agent_id") or "-")[:18])}</td>'
            f'<td><a class="btn sm" href="/settings/answering?number={number}">Configure</a></td>'
            "</tr>"
        )
    return ("<table><thead><tr><th>Phone Number</th><th>Answering Mode</th>"
            f"<th>Bound Agent</th><th>Actions</th></tr></thead>"
            f'<tbody>{"".join(cells)}</tbody></table>')


def available_numbers_table(account_sid, bound_numbers, error=""):
    """What the customer's Twilio account actually owns."""
    if not account_sid:
        return ('<p class="muted">Connect your Twilio account to see your numbers.</p>')
    if error:
        return error_box(error)
    try:
        numbers = tc.list_customer_numbers(account_sid)
    except tc.TwilioError as err:
        return error_box(str(err))
    if not numbers:
        return ('<p class="muted">No phone numbers in your Twilio account. '
                '<a href="https://console.twilio.com/phone-numbers/incoming" '
                'target="_blank" rel="noopener">Buy a number on Twilio</a> '
                "and return here to bind it.</p>")
    rows = []
    for n in numbers:
        already = n["phone_number"] in bound_numbers
        action = ('<span class="pill ok">bound</span>' if already else
                  '<form method="post" action="/numbers/bind">'
                  f'<input type="hidden" name="phone_number" value="{esc(n["phone_number"])}">'
                  '<button class="btn sm primary" type="submit">Bind to CallDesk</button>'
                  "</form>")
        rows.append(
            f'<tr><td class="mono">{esc(n["phone_number"])}</td>'
            f'<td class="muted">{esc(n["friendly_name"])}</td>'
            f'<td>{action}</td></tr>'
        )
    return ("<table><thead><tr><th>Phone Number</th><th>Name</th><th>Action</th>"
            f'</tr></thead><tbody>{"".join(rows)}</tbody></table>')


def calls_table(user_id, limit=None, with_callback=False, with_transcript=False,
                from_number=""):
    rows = db.list_calls(user_id, limit=limit)
    if not rows:
        return '<p class="empty">No calls logged yet.</p>'
    out = []
    for r in rows:
        extra = ""
        if with_transcript:
            text = (r.get("transcript") or "").strip()
            body = esc(text) if text else "No transcript was stored for this call."
            extra += (f'<details><summary>View transcript</summary>'
                      f'<div class="transcript">{body}</div></details>')
        if with_callback and (r.get("callback_number") or "").strip():
            extra += (f'<form method="post" action="/calls/callback" style="margin-top:8px">'
                      f'<input type="hidden" name="call_id" value="{esc(r["id"])}">'
                      f'<input type="hidden" name="from_number" value="{esc(from_number)}">'
                      f'<button class="btn sm primary" type="submit">Call Back</button>'
                      f"</form>")
        out.append(
            f"<tr>"
            f'<td>{esc(str(r.get("created_at") or ""))}</td>'
            f'<td>{esc(r.get("caller_name") or "Unknown")}</td>'
            f'<td class="mono">{esc(r.get("callback_number") or "-")}</td>'
            f'<td>{esc(r.get("problem") or "-")}</td>'
            f'<td><span class="pill {urgency_class(r.get("urgency"))}">'
            f'{esc(urgency_class(r.get("urgency")))}</span></td>'
            f"<td>{extra}</td>"
            f"</tr>"
        )
    head = ("<table><thead><tr><th>When</th><th>Caller</th><th>Number</th>"
            "<th>Problem</th><th>Urgency</th>")
    if with_callback or with_transcript:
        head += "<th>Actions</th>"
    head += "</tr></thead>"
    return f'{head}<tbody>{"".join(out)}</tbody></table>'


def twilio_banner(user, message="", kind="warn"):
    """Connect state, or the reason it is not connected."""
    if message:
        return note(message, kind)
    conn = db.get_twilio_connection(user["id"])
    if conn:
        return (note(f'Twilio connected as <span class="mono">'
                     f'{esc(conn["account_sid"])}</span> &middot; '
                     '<a href="/numbers">Manage Numbers</a>'))
    if not tc.connect_configured():
        return note(
            "<b>Connect your Twilio account</b> to receive calls. You'll be "
            "redirected to Twilio to sign up or log in. Twilio bills you directly "
            "for call usage."
            f'<div class="row" style="margin-top:12px">'
            f'<a class="btn" href="/connect/twilio">Connect Twilio</a></div>'
            f'<p class="muted" style="margin-top:12px;font-size:13px">{esc(CONNECT_NOT_CONFIGURED)}</p>',
            kind)
    return note(
        "<b>Connect your Twilio account</b> to receive calls. You'll be "
        "redirected to Twilio to sign up or log in. Twilio bills you directly "
        "for call usage."
        '<div class="row" style="margin-top:12px">'
        '<a class="btn primary" href="/connect/twilio">Connect Twilio</a></div>',
        kind)


AGENT = None
PAGE = ""

# Routes a browser only ever GETs, and so have no POST handler. Used to answer
# a stray POST with 405 instead of a misleading 404. /signup, /login, /logout,
# /numbers/bind, /settings/answering, /calls/callback and /tool/send_summary
# are deliberately absent: they are real POST routes.
GET_ONLY = {"/", "/dashboard", "/numbers", "/calls", "/talk",
            "/connect/twilio", "/connect/twilio/callback",
            "/twiml/fallback", "/twiml/outbound", "/app.js",
            "/token", "/agent"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # --- plumbing -------------------------------------------------------

    def _send(self, status: int, body: bytes, content_type: str,
              extra: Optional[dict] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body: bytes, status: int = 200, extra: Optional[dict] = None) -> None:
        self._send(status, body, "text/html; charset=utf-8", extra)

    def _twiml(self, document: str) -> None:
        self._send(200, document.encode("utf-8"), "text/xml; charset=utf-8")

    def _redirect(self, location: str, extra: Optional[dict] = None,
                  status: int = 302) -> None:
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _form(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        parsed = urllib.parse.parse_qs(raw.decode("utf-8", "replace"))
        return {k: v[0] for k, v in parsed.items()}

    def _query(self):
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def _one(self, key):
        return (self._query().get(key) or [""])[0]

    def _base_url(self) -> str:
        return tc.resolve_base_url(self.headers.get("Host") or "")

    def _session_token(self) -> str:
        return auth.token_from_cookie(self.headers.get("Cookie"))

    def _user(self):
        return auth.get_session_user(self._session_token())

    def _require_user(self):
        user = self._user()
        if not user:
            self._redirect("/login?next=1")
            return None
        return user

    # --- GET ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path

        # --- unchanged voice-agent plumbing ---
        if path == "/token":
            try:
                token = aai("/token?product=voice_agent&expires_in_seconds=60")
                self._send(200, json.dumps(token).encode(), "application/json")
            except ApiError as err:
                print(err)
                self._send(502, b'{"error":"token request failed"}', "application/json")
            return
        if path == "/agent":
            try:
                agent = aai(f"/agents/{AGENT['id']}")
                self._send(200, json.dumps(public_agent(agent)).encode(), "application/json")
            except ApiError as err:
                print(err)
                self._send(502, b'{"error":"could not load the agent"}', "application/json")
            return
        if path == "/app.js":
            self._send(200, (HERE / "app.js").read_bytes(), "text/javascript")
            return
        if path == "/talk":
            self._html(PAGE.encode())
            return

        # --- TwiML that Twilio fetches ---
        if path == "/twiml/fallback":
            self._twiml(tc.fallback_twiml())
            return
        if path == "/twiml/outbound":
            self._twiml(tc.fallback_twiml())
            return

        # --- SaaS pages ---
        if path == "/":
            if self._user():
                self._redirect("/dashboard")
                return
            self._html(page("landing.html", "Voice AI receptionist", nav_for(None)))
            return

        if path == "/login":
            if self._user():
                self._redirect("/dashboard")
                return
            self._html(page("login.html", "Log in", nav_for(None), EMAIL=self._one("email")))
            return

        if path == "/signup":
            if self._user():
                self._redirect("/dashboard")
                return
            self._html(page("signup.html", "Sign up", nav_for(None),
                            EMAIL=self._one("email"),
                            BUSINESS_NAME=self._one("business_name"),
                            MOBILE_NUMBER=self._one("mobile_number")))
            return

        if path == "/dashboard":
            self._dashboard()
            return
        if path == "/numbers":
            self._numbers()
            return
        if path == "/settings/answering":
            self._answering_form()
            return
        if path == "/calls":
            self._calls()
            return
        if path == "/connect/twilio":
            self._connect_twilio()
            return
        if path == "/connect/twilio/callback":
            self._connect_callback()
            return

        self._html(b"<!doctype html><title>404</title><h1>404</h1>"
                   b'<p><a href="/">Home</a></p>', status=404)

    # --- page handlers --------------------------------------------------

    def _dashboard(self) -> None:
        user = self._require_user()
        if not user:
            return
        numbers = db.list_phone_numbers(user["id"])
        calls = db.list_calls(user["id"])
        message = ""
        kind = "warn"
        if self._one("error"):
            message, kind = f'<b>Could not bind that number.</b> {esc(self._one("error"))}', "err"
        elif self._one("ok"):
            message = f'<b>Bound.</b> {esc(self._one("ok"))}'
            kind = ""
        self._html(page(
            "dashboard.html", "Dashboard", nav_for(user),
            BUSINESS_NAME=user["business_name"], EMAIL=user["email"],
            TWILIO_BANNER=twilio_banner(user, message, kind),
            NUMBERS=numbers_table(user["id"]),
            RECENT_CALLS=calls_table(user["id"], limit=10),
        ))

    def _numbers(self) -> None:
        user = self._require_user()
        if not user:
            return
        conn = db.get_twilio_connection(user["id"])
        bound = {r["phone_number"] for r in db.list_phone_numbers(user["id"])}
        available = available_numbers_table(
            conn["account_sid"] if conn else "", bound, self._one("error")
        )
        banner = ""
        if conn:
            banner = (note(f'Twilio connected as <span class="mono">'
                           f'{esc(conn["account_sid"])}</span>'))
        elif not tc.connect_configured():
            banner = note(esc(CONNECT_NOT_CONFIGURED), "warn")
        self._html(page(
            "numbers.html", "Phone numbers", nav_for(user),
            ERROR=error_box(self._one("error")) if not conn else "",
            TWILIO_BANNER=banner,
            NUMBERS=numbers_table(user["id"]),
            AVAILABLE=available,
        ))

    def _answering_form(self) -> None:
        user = self._require_user()
        if not user:
            return
        number = self._one("number")
        row = db.get_phone_number(user["id"], number)
        if not row:
            self._redirect("/numbers?error=That+number+is+not+on+your+account.")
            return
        mode = (row.get("answering_mode") or "agent").strip()
        forward = row.get("forward_to") or user.get("mobile_number") or ""
        self._html(page(
            "answering.html", "Answering settings", nav_for(user),
            PHONE_NUMBER=number, FORWARD_TO=forward,
            SEL_AGENT='selected' if mode == "agent" else '',
            SEL_HUMAN='selected' if mode == "human_first" else '',
            MODE_A_PILL="ok" if mode == "agent" else "off",
            MODE_B_PILL="medium" if mode == "human_first" else "off",
            ERROR=error_box(self._one("error")),
        ))

    def _calls(self) -> None:
        user = self._require_user()
        if not user:
            return
        conn = db.get_twilio_connection(user["id"])
        numbers = [r["phone_number"] for r in db.list_phone_numbers(user["id"])]
        calls = db.list_calls(user["id"])
        self._html(page(
            "calls.html", "Calls", nav_for(user),
            BUSINESS_NAME=user["business_name"], CALL_COUNT=len(calls),
            ERROR=self._call_message(conn, numbers, self._one("error"), self._one("ok")),
            # Callers are called back from the first bound number.
            CALLS=calls_table(user["id"], with_callback=True, with_transcript=True,
                              from_number=numbers[0] if numbers else ""),
        ))

    def _call_message(self, conn, numbers, error, ok):
        if error:
            return error_box(error)
        if ok:
            return note(f'<b>Call placed.</b> {esc(ok)}')
        if not conn:
            return note("Connect Twilio before calling callers back.", "warn")
        if not numbers:
            return note("Bind a phone number before calling callers back.", "warn")
        return ""

    def _connect_twilio(self) -> None:
        user = self._require_user()
        if not user:
            return
        if not tc.connect_configured():
            # Nothing to redirect to. The dashboard already says why.
            self._redirect("/dashboard")
            return
        url = tc.get_connect_url(user["id"])
        if not url:
            self._redirect("/dashboard?error=Could+not+build+the+Twilio+Connect+URL.")
            return
        self._redirect(url)

    def _connect_callback(self) -> None:
        """Twilio Connect sends the customer back here with their AccountSid."""
        account_sid = self._one("AccountSid") or self._one("account_sid")
        state = self._one("state")
        try:
            tc.handle_authorize_callback(account_sid, state)
        except tc.TwilioError as err:
            self._redirect(f"/login?error={urllib.parse.quote(str(err)[:200])}")
            return
        self._redirect("/dashboard")

    # --- POST -----------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path

        if path == "/tool/send_summary":
            self._handle_send_summary()
            return

        if path in GET_ONLY:
            self.send_response(405)
            self.send_header("Allow", "GET, HEAD")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        form = self._form()

        if path == "/signup":
            self._signup(form)
            return
        if path == "/login":
            self._login(form)
            return
        if path == "/logout":
            auth.delete_session(self._session_token())
            self._redirect("/", {"Set-Cookie": auth.clear_cookie()}, status=303)
            return
        if path == "/numbers/bind":
            self._bind(form)
            return
        if path == "/settings/answering":
            self._save_answering(form)
            return
        if path == "/calls/callback":
            self._call_back(form)
            return

        self._send(404, b'{"error":"not found"}', "application/json")

    # --- POST handlers --------------------------------------------------

    def _fail_form(self, template, title, nav, message, values, status=400):
        self._html(page(template, title, nav, ERROR=error_box(message), **values),
                   status=status)

    def _signup(self, form) -> None:
        email = (form.get("email") or "").strip().lower()
        password = form.get("password") or ""
        business = (form.get("business_name") or "").strip()
        mobile = (form.get("mobile_number") or "").strip()
        keep = {"EMAIL": email, "BUSINESS_NAME": business, "MOBILE_NUMBER": mobile}
        if not (email and password and business):
            self._fail_form("signup.html", "Sign up", nav_for(None),
                            "All of business name, email, and password are required.",
                            keep)
            return
        if len(password) < 8:
            self._fail_form("signup.html", "Sign up", nav_for(None),
                            "Password must be at least 8 characters.", keep)
            return
        if db.get_user_by_email(email):
            self._fail_form("signup.html", "Sign up", nav_for(None),
                            "That email is already registered. Try logging in.", keep)
            return
        user_id = db.create_user(email, auth.hash_password(password), business, mobile)
        token = auth.create_session(user_id)
        print(f"New account: {business} <{email}>", flush=True)
        self._redirect("/dashboard", {"Set-Cookie": auth.set_cookie(token)}, status=303)

    def _login(self, form) -> None:
        email = (form.get("email") or "").strip().lower()
        password = form.get("password") or ""
        user = db.get_user_by_email(email)
        if not user or not auth.verify_password(password, user.get("password_hash")):
            self._fail_form("login.html", "Log in", nav_for(None),
                            "Wrong email or password.", {"EMAIL": email}, status=401)
            return
        token = auth.create_session(user["id"])
        self._redirect("/dashboard", {"Set-Cookie": auth.set_cookie(token)}, status=303)

    def _bind(self, form) -> None:
        user = self._require_user()
        if not user:
            return
        number = (form.get("phone_number") or "").strip()
        if not number:
            self._redirect("/numbers?error=Enter+a+phone+number.")
            return
        conn = db.get_twilio_connection(user["id"])
        if not conn:
            self._redirect("/connect/twilio")
            return
        try:
            tc.bind_number_to_agent(conn["account_sid"], number, AGENT["id"])
        except tc.TwilioError as err:
            self._redirect("/numbers?error=" + urllib.parse.quote(str(err)[:250]))
            return
        db.add_phone_number(user["id"], number, agent_id=AGENT["id"])
        print(f"Bound {number} to {AGENT['id']} for {user['business_name']}", flush=True)
        self._redirect(f"/dashboard?ok={urllib.parse.quote('Bound ' + number)}")

    def _save_answering(self, form) -> None:
        user = self._require_user()
        if not user:
            return
        number = (form.get("phone_number") or "").strip()
        mode = (form.get("answering_mode") or "agent").strip()
        forward = (form.get("forward_to") or "").strip()
        if not db.get_phone_number(user["id"], number):
            self._redirect("/numbers?error=That+number+is+not+on+your+account.")
            return
        if mode not in tc.ANSWERING_MODES:
            self._redirect("/settings/answering?number="
                           + urllib.parse.quote(number) + "&error=Unknown+answering+mode.")
            return
        conn = db.get_twilio_connection(user["id"])
        if not conn:
            self._redirect("/connect/twilio")
            return
        forward_to = forward or (user.get("mobile_number") or "")
        try:
            tc.set_number_answering_mode(conn["account_sid"], number, mode,
                                         forward_to, self._base_url())
        except tc.TwilioError as err:
            self._redirect("/settings/answering?number="
                           + urllib.parse.quote(number) + "&error="
                           + urllib.parse.quote(str(err)[:250]))
            return
        db.update_phone_number_mode(user["id"], number, mode, forward_to or None)
        label = "Mode A" if mode == "agent" else "Mode B"
        self._redirect(f"/settings/answering?number={urllib.parse.quote(number)}"
                       f"&ok={urllib.parse.quote('Saved ' + label)}")

    def _call_back(self, form) -> None:
        """Call a missed caller back, letting CallDesk speak first."""
        user = self._require_user()
        if not user:
            return
        call = db.get_call_by_id(form.get("call_id"), user_id=user["id"])
        if not call:
            self._redirect("/calls?error=That+call+was+not+found.")
            return
        target = (call.get("callback_number") or "").strip()
        if not target:
            self._redirect("/calls?error=That+call+has+no+callback+number.")
            return
        conn = db.get_twilio_connection(user["id"])
        if not conn:
            self._redirect("/calls?error=Connect+Twilio+before+calling+callers+back.")
            return
        from_number = (form.get("from_number") or "").strip()
        if not from_number:
            numbers = db.list_phone_numbers(user["id"])
            from_number = numbers[0]["phone_number"] if numbers else ""
        if not from_number:
            self._redirect("/calls?error=Bind+a+phone+number+before+calling+back.")
            return
        callback = f"{self._base_url()}/twiml/outbound?caller={urllib.parse.quote(target)}"
        try:
            sid = tc.place_outbound_call(conn["account_sid"], target, from_number, callback)
        except tc.TwilioError as err:
            self._redirect("/calls?error=" + urllib.parse.quote(str(err)[:250]))
            return
        self._redirect(f"/calls?ok={urllib.parse.quote('Calling ' + target + ' back (call ' + (sid or 'placed') + ')')}")

    # --- send_summary ---------------------------------------------------

    def _handle_send_summary(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            args = json.loads(raw)
        except json.JSONDecodeError:
            self._send(400, b'{"error":"expected a json body"}', "application/json")
            return
        if not isinstance(args, dict):
            self._send(400, b'{"error":"expected a json object"}', "application/json")
            return

        print("send_summary " + json.dumps(args, indent=2, sort_keys=True), flush=True)

        try:
            self._file_call(args)
        except Exception as err:  # a logging failure must not break the call
            print(f"send_summary logging failed: {err}", flush=True)

        self._send(200, json.dumps({"sent": True}).encode(), "application/json")

    def _file_call(self, args: dict) -> None:
        """Attribute the call to an account, then store it.

        The tool posts only the arguments the model filled in, so the number
        that received the call is not normally present. When it is missing the
        call is attributed to the first account in the database, which keeps a
        seeded single-tenant demo working.
        """
        called = ""
        for key in ("to", "to_number", "phone_number", "called_number"):
            if isinstance(args.get(key), str) and args[key].strip():
                called = args[key].strip()
                break

        owner = db.owner_of_number(called) if called else None
        user = db.get_user_by_id(owner["user_id"]) if owner else db.first_user()
        if not user:
            print("send_summary: no user in the database, call not stored", flush=True)
            return

        db.log_call(
            user["id"],
            args.get("caller_name"),
            args.get("callback_number"),
            args.get("problem_description") or args.get("problem"),
            args.get("urgency"),
            args.get("transcript"),
        )
        print(f"send_summary: filed under {user['business_name']} "
              f"(matched={'yes' if owner else 'fallback first user'})", flush=True)

    def log_message(self, *args) -> None:  # quiet; errors are printed above
        pass


def main() -> None:
    global AGENT, PAGE
    load_env()
    required("ASSEMBLYAI_API_KEY", "get one at https://www.assemblyai.com/dashboard/api-keys")

    db.init_db()
    db.purge_expired_sessions()

    AGENT = resolve_agent()
    PAGE = ((HERE / "index.html").read_text()
            .replace("{{AGENT_NAME}}", AGENT["name"])
            .replace("{{AGENT_JSON}}", json.dumps(AGENT).replace("<", "\\u003c")))

    # PORT when set, otherwise 3000 and up until one is free.
    fixed = os.environ.get("PORT")
    port = int(fixed) if fixed else 3000
    while True:
        try:
            server = ThreadingHTTPServer(("", port), Handler)
            break
        except OSError:
            if fixed or port >= 3010:
                raise
            port += 1

    print(f"Database:  {db.DB_PATH}")
    print(f"Agent:     {AGENT['id']}")
    print(f"Site:      http://localhost:{port}")
    print(f"Voice UI:  http://localhost:{port}/talk")
    base = tc.resolve_base_url()
    print(f"Base URL:  {base}")
    if not tc.connect_configured():
        print(f"Connect:   {CONNECT_NOT_CONFIGURED}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
