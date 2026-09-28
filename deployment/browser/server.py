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
import google_auth  # noqa: E402
import twilio_connect as tc  # noqa: E402
from lib import (ApiError, aai, load_env, publish_agent, read_agent,  # noqa: E402
                 required, stored_agent_id)

TEMPLATES = ROOT / "templates"
STATIC = HERE / "static"


def resolve_agent() -> dict:
    """A published id means the agent is managed elsewhere, so use it as it is."""
    # CallDesk is the product, so it is the default. The starter's minimal agent
    # is still one env var away: AGENT=minimal python deployment/browser/server.py
    name = os.environ.get("AGENT", "calldesk")
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
       "TWILIO_BANNER", "ERROR", "PAGER"}

# A one-shot message for the toast on the next page. base.html reads the cookie,
# shows it, and clears it, so it never survives to a second page.
FLASH_COOKIE = "calldesk_flash"


def flash_cookie(message: str, kind: str = "ok") -> str:
    """A Set-Cookie value that carries a flash message across a redirect."""
    return f"{FLASH_COOKIE}={urllib.parse.quote(f'{kind}|{message}', safe='')}; Path=/; Max-Age=30; SameSite=Lax"


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
CONNECT_NOT_CONFIGURED = (
    "Twilio Connect not configured. To enable phone integration, an admin "
    "must create a Twilio Connect App (requires an upgraded Twilio account) "
    "and set TWILIO_CONNECT_APP_SID in .env. The code for the full Connect "
    "flow is already implemented and ready to activate."
)


GOOGLE_MARK = (
    '<svg width="18" height="18" viewBox="0 0 48 48" aria-hidden="true">'
    '<path fill="#EA4335" d="M24 9.5c3.5 0 6.6 1.2 9 3.6l6.7-6.7C36.1 2.6 30.5.5 24 .5'
    ' 14.6.5 6.5 5.9 2.6 13.8l7.8 6c1.9-5.6 7-9.3 13.6-9.3z"/>'
    '<path fill="#4285F4" d="M46.98 24.55c0-1.6-.15-3.15-.42-4.65H24v8.8h12.94'
    'c-.58 2.9-2.26 5.36-4.82 7.01l7.73 6c4.51-4.17 7.13-10.32 7.13-17.16z"/>'
    '<path fill="#FBBC05" d="M10.38 28.2A14.5 14.5 0 0 1 9.6 24c0-1.5.26-2.9.78-4.2l-7.8-6'
    'C.87 17.2 0 20.4 0 24c0 3.6.87 6.8 2.56 9.8z"/>'
    '<path fill="#34A853" d="M24 47.5c6.2 0 11.5-2 15.4-5.6l-7.7-6c-2.1 1.4-4.8 2.3-7.7 2.3'
    '-6.6 0-12.2-4.4-14.2-10.4l-7.8 6C6.5 42.1 14.6 47.5 24 47.5z"/>'
    "</svg>"
)


def google_hint():
    """Shown under the Google button when the OAuth client is not configured,
    so the button never leads to a dead end."""
    if google_auth.configured():
        return ""
    return note(
        "Google sign-in is not set up yet. Add GOOGLE_CLIENT_ID and "
        "GOOGLE_CLIENT_SECRET to .env to enable it.", "warn")


def google_login_page(message=""):
    """The login page, carrying a Google failure back to the customer."""
    return page("login.html", "Log in", nav_for(None),
                ERROR=error_box(message) if message else "",
                GOOGLE_MARK=GOOGLE_MARK, GOOGLE_HINT=google_hint())


def urgency_class(value) -> str:
    v = (value or "").strip().lower()
    return v if v in URGENCIES else "medium"


def utc_day(days_ago: int = 0) -> str:
    """A 'YYYY-MM-DD' day string in UTC, matching CURRENT_TIMESTAMP.

    calls.created_at is a SQLite CURRENT_TIMESTAMP, so it is UTC and sorts and
    compares correctly as plain text. `days_ago` counts backwards; the name is
    deliberate, because adding would silently produce a future cutoff and an
    empty result rather than an error.
    """
    import datetime
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=days_ago)).strftime("%Y-%m-%d")


def pager(page_no: int, pages: int, total: int, span: str) -> str:
    """Previous/next controls, only once there is more than one page."""
    if pages < 2:
        return ""
    base = "/calls" + (f"?range={span}" if span else "")

    def link(target, label, enabled):
        if not enabled:
            return f'<span class="btn btn-secondary btn-sm" aria-disabled="true">{label}</span>'
        return (f'<a class="btn btn-secondary btn-sm" '
                f'href="{base}&page={target}">{label}</a>')

    return (
        f'<nav class="pager" aria-label="Call history pages">'
        f'{link(page_no - 1, "Previous", page_no > 1)}'
        f'<span class="count">Page {page_no} of {pages} &middot; {total} calls</span>'
        f'{link(page_no + 1, "Next", page_no < pages)}'
        f"</nav>"
    )


def initials(name: str) -> str:
    """Up to two letters for the header avatar."""
    parts = [p for p in (name or "").replace("&", " ").split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


def nav_for(user) -> str:
    """Header nav, a hamburger, and the account menu."""
    if not user:
        return (
            '<nav class="nav-links" id="primary-nav">'
            '<a href="/login">Sign in</a>'
            '<a href="/signup">Sign up</a>'
            "</nav>"
            '<div class="header-right">'
            '<a class="btn btn-primary btn-sm" href="/signup">Get started</a>'
            "</div>"
        )
    name = esc(user["business_name"])
    return (
        '<nav class="nav-links" id="primary-nav">'
        '<a href="/dashboard">Dashboard</a>'
        '<a href="/numbers">Numbers</a>'
        '<a href="/calls">Calls</a>'
        '<a href="/settings/answering">Settings</a>'
        "</nav>"
        '<div class="header-right">'
        '<a class="btn btn-ghost btn-sm" href="/talk" target="_blank" rel="noopener">'
        "Talk to CallDesk</a>"
        '<div class="user-menu">'
        f'<button class="user-button" type="button" aria-haspopup="true" aria-expanded="false">'
        f'<span class="avatar" aria-hidden="true">{initials(user["business_name"])}</span>'
        f'<span class="user-name truncate">{name}</span>'
        "</button>"
        '<div class="dropdown" role="menu">'
        '<div class="dropdown-head">'
        f'<div class="who truncate">{name}</div>'
        f'<div class="mail truncate">{esc(user["email"])}</div>'
        "</div>"
        '<a href="/dashboard" role="menuitem">Dashboard</a>'
        '<a href="/numbers" role="menuitem">Numbers</a>'
        '<a href="/settings/answering" role="menuitem">Settings</a>'
        '<form method="post" action="/logout">'
        '<button class="danger" type="submit" role="menuitem">Log out</button>'
        "</form>"
        "</div></div>"
        "</div>"
        '<button class="hamburger" id="nav-toggle" type="button" '
        'aria-label="Menu" aria-expanded="false" aria-controls="primary-nav">'
        '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="2" stroke-linecap="round" aria-hidden="true">'
        '<path d="M3 6h18M3 12h18M3 18h18"/></svg>'
        "</button>"
    )


def note(text, kind=""):
    """A callout. `kind` is a legacy note kind, mapped onto the alert variants."""
    css = {"err": "alert-danger", "danger": "alert-danger",
           "warn": "alert-warning", "warning": "alert-warning",
           "info": "alert-info", "ok": "alert-info"}.get(kind, "")
    return f'<div class="alert {css}">{text}</div>'.replace("  ", " ")


def error_box(message):
    return note(esc(message), "err") if message else ""


def empty_state(icon, title, body, action=""):
    """The centered placeholder shown when a list has nothing in it."""
    return (
        '<div class="empty-state">'
        f'<div class="icon">{icon}</div>'
        f"<h3>{esc(title)}</h3>"
        f"<p>{body}</p>"
        f'<div class="row">{action}</div>'
        "</div>"
    )


ICON_EMPTY = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6'
    'A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 1.9.7 2.8'
    'a2 2 0 0 1-.5 2.1L8.1 9.9a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 2.8.7'
    'a2 2 0 0 1 1.7 2z"/></svg>'
)


def badge(text, kind):
    return f'<span class="badge badge-{kind}">{esc(text)}</span>'


URGENCY_BADGE = {"high": "danger", "medium": "warning", "low": "neutral"}


def numbers_table(user_id):
    rows = db.list_phone_numbers(user_id)
    if not rows:
        return empty_state(
            ICON_EMPTY, "No numbers bound yet",
            "Bind a number from your Twilio account and CallDesk starts answering it.",
            '<a class="btn btn-primary" href="/numbers">Choose a number</a>')
    cells = []
    for r in rows:
        mode = (r.get("answering_mode") or "agent").strip()
        number = esc(r["phone_number"])
        if mode == "agent":
            mode_badge = badge("CallDesk answers", "success")
        else:
            mode_badge = badge("Ring mobile first", "warning")
        bound = (r.get("agent_id") or "").strip()
        agent_cell = (f'<span class="badge badge-neutral badge-plain mono">'
                      f'{esc(bound[:18])}</span>' if bound else
                      '<span class="muted">Not set</span>')
        cells.append(
            f'<tr><td data-label="Number"><span class="mono">{number}</span></td>'
            f'<td data-label="Answering mode">{mode_badge}</td>'
            f'<td data-label="Bound agent">{agent_cell}</td>'
            f'<td data-label="Actions"><a class="btn btn-secondary btn-sm" '
            f'href="/settings/answering?number={urllib.parse.quote(number)}">Configure</a></td>'
            "</tr>"
        )
    return ('<table class="table"><thead><tr><th>Number</th><th>Answering mode</th>'
            "<th>Bound agent</th><th>Actions</th></tr></thead>"
            f'<tbody>{"".join(cells)}</tbody></table>')


def available_numbers_table(account_sid, bound_numbers, error=""):
    """What the customer's Twilio account actually owns."""
    if not account_sid:
        return empty_state(
            ICON_EMPTY, "Connect your Twilio account first",
            "CallDesk needs a Twilio account to see the numbers you can hand to it.",
            '<a class="btn btn-primary" href="/connect/twilio">Connect Twilio</a>')
    if error:
        return error_box(error)
    try:
        numbers = tc.list_customer_numbers(account_sid)
    except tc.TwilioError as err:
        return error_box(str(err))
    if not numbers:
        return empty_state(
            ICON_EMPTY, "No phone numbers in your Twilio account",
            'Buy a number on Twilio, then come back and bind it. '
            '<a href="https://console.twilio.com/phone-numbers/incoming" '
            'target="_blank" rel="noopener">Buy a number on Twilio</a>.',
            '<a class="btn btn-secondary" href="https://console.twilio.com/'
            'phone-numbers/incoming" target="_blank" rel="noopener">Open Twilio</a>')
    rows = []
    for n in numbers:
        already = n["phone_number"] in bound_numbers
        if already:
            action = badge("Bound", "success")
        else:
            action = ('<form method="post" action="/numbers/bind">'
                      f'<input type="hidden" name="phone_number" value="{esc(n["phone_number"])}">'
                      '<button class="btn btn-primary btn-sm" type="submit">'
                      "Bind to CallDesk</button></form>")
        rows.append(
            f'<tr><td data-label="Number"><span class="mono">{esc(n["phone_number"])}</span></td>'
            f'<td data-label="Name"><span class="muted">{esc(n["friendly_name"])}</span></td>'
            f'<td data-label="Action">{action}</td></tr>'
        )
    return ('<table class="table"><thead><tr><th>Number</th><th>Name</th><th>Action</th>'
            f'</tr></thead><tbody>{"".join(rows)}</tbody></table>')


def calls_table(user_id, limit=None, offset=0, since=None, with_callback=False,
                with_transcript=False, from_number=""):
    rows = db.list_calls(user_id, limit=limit, offset=offset, since=since)
    if not rows:
        if since:
            return empty_state(
                ICON_EMPTY, "No calls in this range",
                "Nothing was logged in the period you picked.")
        return empty_state(
            ICON_EMPTY, "No calls yet",
            "Once someone calls your CallDesk number, they'll show up here.",
            '<a class="btn btn-secondary" href="/numbers">Bind a number</a>')
    out = []
    for r in rows:
        extra = ""
        if with_transcript:
            text = (r.get("transcript") or "").strip()
            body = esc(text) if text else "No transcript was stored for this call."
            extra += ('<details><summary>View transcript</summary>'
                      f'<div class="transcript-box">{body}</div></details>')
        if with_callback and (r.get("callback_number") or "").strip():
            extra += ('<form method="post" action="/calls/callback">'
                      f'<input type="hidden" name="call_id" value="{esc(r["id"])}">'
                      f'<input type="hidden" name="from_number" value="{esc(from_number)}">'
                      '<button class="btn btn-secondary btn-sm" type="submit">Call back</button>'
                      "</form>")
        urgency = urgency_class(r.get("urgency"))
        caller = esc(r.get("caller_name") or "Unknown")
        number = r.get("callback_number") or ""
        when = esc(str(r.get("created_at") or ""))
        out.append(
            "<tr>"
            f'<td data-label="Caller">{caller}</td>'
            f'<td data-label="Number"><span class="mono">'
            f'{esc(number) if number else "-"}</span></td>'
            f'<td data-label="Problem">{esc(r.get("problem") or "-")}</td>'
            f'<td data-label="Urgency">{badge(urgency, URGENCY_BADGE[urgency])}</td>'
            f'<td data-label="When"><span class="nowrap muted">{when}</span></td>'
            + (f'<td data-label="Actions">{extra}</td>' if extra else "")
            + "</tr>"
        )
    head = ('<table class="table"><thead><tr><th>Caller</th><th>Number</th>'
            "<th>Problem</th><th>Urgency</th><th>When</th>")
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
        return note(
            f'Connected as <span class="mono">{esc(conn["account_sid"])}</span> &middot; '
            '<a href="/numbers">Manage numbers</a>', "ok")
    if not tc.connect_configured():
        return note(
            "<b>Twilio Connect isn't configured on this deployment yet.</b> "
            "An admin has to create a Twilio Connect App, which needs an upgraded "
            "Twilio account, and set TWILIO_CONNECT_APP_SID in .env. "
            "The full Connect flow is already coded and ready to activate."
            '<div class="row"><a class="btn btn-primary" href="/numbers">'
            "See your numbers</a></div>"
            f'<p>{esc(CONNECT_NOT_CONFIGURED)}</p>',
            kind)
    return note(
        "<b>Connect your Twilio account</b> to start receiving calls. You'll be "
        "redirected to Twilio to sign up or log in, and Twilio bills you directly "
        "for call usage."
        '<div class="row"><a class="btn btn-primary" href="/connect/twilio">'
        "Connect Twilio</a></div>",
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
            "/auth/google", "/auth/google/callback",
            "/twiml/fallback", "/twiml/outbound", "/app.js",
            "/token", "/agent", "/static/style.css"}


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
                  status: int = 302, flash: str = "", kind: str = "ok") -> None:
        """A redirect, optionally carrying a one-shot toast message.

        The flash rides a second Set-Cookie, so it can travel alongside the
        session cookie in `extra`.
        """
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        if flash:
            self.send_header("Set-Cookie", flash_cookie(flash, kind))
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
        if path == "/static/style.css":
            css = STATIC / "style.css"
            if not css.exists():
                self._send(404, b"/* missing */", "text/css")
                return
            self._send(200, css.read_bytes(), "text/css; charset=utf-8",
                       {"Cache-Control": "public, max-age=300"})
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
            self._html(page("login.html", "Log in", nav_for(None),
                            EMAIL=self._one("email"),
                            GOOGLE_MARK=GOOGLE_MARK,
                            GOOGLE_HINT=google_hint()))
            return

        if path == "/signup":
            if self._user():
                self._redirect("/dashboard")
                return
            self._html(page("signup.html", "Sign up", nav_for(None),
                            EMAIL=self._one("email"),
                            BUSINESS_NAME=self._one("business_name"),
                            MOBILE_NUMBER=self._one("mobile_number"),
                            GOOGLE_MARK=GOOGLE_MARK,
                            GOOGLE_HINT=google_hint()))
            return

        if path == "/auth/google":
            self._google_start()
            return

        if path == "/auth/google/callback":
            self._google_callback()
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
        message = ""
        kind = "warn"
        flash = ""
        if self._one("error"):
            message, kind = f'<b>Could not bind that number.</b> {esc(self._one("error"))}', "err"
        elif self._one("ok"):
            message = f'<b>Bound.</b> {esc(self._one("ok"))}'
            kind = "info"
        self._html(page(
            "dashboard.html", "Dashboard", nav_for(user),
            BUSINESS_NAME=user["business_name"], EMAIL=user["email"],
            TWILIO_BANNER=twilio_banner(user, message, kind),
            NUMBERS=numbers_table(user["id"]),
            RECENT_CALLS=calls_table(user["id"], limit=10),
            CALLS_TODAY=db.count_calls(user["id"], since=utc_day()),
            CALLS_WEEK=db.count_calls(user["id"], since=utc_day(days_ago=7)),
            NUMBERS_COUNT=len(numbers),
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
            banner = note(
                f'Connected as <span class="mono">{esc(conn["account_sid"])}</span> '
                f'&middot; {len(bound)} number{"s" if len(bound) != 1 else ""} bound',
                "ok")
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
        if not number:
            # The dashboard links here without a number, so fall back to the
            # first bound one instead of bouncing to /numbers with an error.
            bound = db.list_phone_numbers(user["id"])
            if not bound:
                self._redirect(
                    "/numbers?error=Bind+a+number+before+setting+an+answering+mode.")
                return
            number = bound[0]["phone_number"]
        row = db.get_phone_number(user["id"], number)
        if not row:
            self._redirect("/numbers?error=That+number+is+not+on+your+account.")
            return
        mode = (row.get("answering_mode") or "agent").strip()
        forward = row.get("forward_to") or user.get("mobile_number") or ""
        self._html(page(
            "answering.html", "Answering settings", nav_for(user),
            PHONE_NUMBER=number, FORWARD_TO=forward,
            SEL_AGENT='checked' if mode == "agent" else '',
            SEL_HUMAN='checked' if mode == "human_first" else '',
            MODE_A_PILL="success" if mode == "agent" else "neutral",
            MODE_B_PILL="warning" if mode == "human_first" else "neutral",
            ERROR=error_box(self._one("error")),
        ))

    def _calls(self) -> None:
        user = self._require_user()
        if not user:
            return
        conn = db.get_twilio_connection(user["id"])
        numbers = [r["phone_number"] for r in db.list_phone_numbers(user["id"])]

        # All / Today / This week, then a page of 25.
        span = self._one("range")
        since = utc_day() if span == "today" else (
            utc_day(days_ago=7) if span == "week" else None)
        total = db.count_calls(user["id"], since=since)
        page_no = max(1, int(self._one("page") or 1) or 1)
        per_page = 25
        pages = max(1, (total + per_page - 1) // per_page)
        page_no = min(page_no, pages)
        offset = (page_no - 1) * per_page

        self._html(page(
            "calls.html", "Calls", nav_for(user),
            BUSINESS_NAME=user["business_name"], CALL_COUNT=total,
            ERROR=self._call_message(conn, numbers, self._one("error"), self._one("ok")),
            # Callers are called back from the first bound number.
            CALLS=calls_table(user["id"], limit=per_page, offset=offset, since=since,
                              with_callback=True, with_transcript=True,
                              from_number=numbers[0] if numbers else ""),
            FILTER_ALL="" if span else "btn-primary",
            FILTER_TODAY="btn-primary" if span == "today" else "",
            FILTER_WEEK="btn-primary" if span == "week" else "",
            PAGER=pager(page_no, pages, total, span),
        ))

    def _call_message(self, conn, numbers, error, ok):
        if error:
            return error_box(error)
        if ok:
            return note(f'<b>Call placed.</b> {esc(ok)}', "ok")
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
            self._redirect("/", {"Set-Cookie": auth.clear_cookie()}, status=303,
                           flash="Signed out.")
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

    def _google_start(self) -> None:
        """Send the customer to Google's consent screen.

        Someone already signed in carries their user id through as the state,
        so the callback knows who is arriving. A new customer gets an empty
        state and is created from their Google profile on the way back.
        """
        try:
            user = self._user()
            url = google_auth.get_google_auth_url(
                str(user["id"]) if user else "")
        except google_auth.GoogleAuthError as err:
            self._html(google_login_page(str(err)), status=400)
            return
        self._redirect(url)

    def _google_callback(self) -> None:
        """Google sends ?code= and ?state= back. _one() because _query()
        hands back every value as a list, and state has to be a string to
        look up."""
        if self._one("error"):
            reason = (self._one("error_description")
                      or "Google sign-in was cancelled.")[:200]
            self._html(google_login_page(f"Google sign-in failed. {reason}"),
                       status=400)
            return
        try:
            # Burn the state before anything else, so a replayed callback
            # cannot mint a second session.
            google_auth.consume_state(self._one("state"))
            profile = google_auth.exchange_code_for_user(self._one("code"))
            user = google_auth.find_or_create_google_user(
                profile["email"], profile["google_id"], profile.get("name", ""))
        except google_auth.GoogleAuthError as err:
            self._html(google_login_page(str(err)), status=400)
            return
        except Exception as err:  # noqa: BLE001 - never 500 on a bad callback
            print(f"google callback failed: {err!r}", flush=True)
            self._html(google_login_page(
                "Google sign-in could not be completed. Please try again."),
                status=400)
            return
        if not user:
            self._html(google_login_page(
                "That Google account could not be set up. Please try again."),
                status=400)
            return
        self._redirect(
            "/dashboard",
            {"Set-Cookie": auth.set_cookie(auth.create_session(user["id"]))},
            status=303)

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
        self._redirect("/dashboard", {"Set-Cookie": auth.set_cookie(token)}, status=303,
                       flash=f"Welcome to CallDesk, {business}.")

    def _login(self, form) -> None:
        email = (form.get("email") or "").strip().lower()
        password = form.get("password") or ""
        user = db.get_user_by_email(email)
        if not user or not auth.verify_password(password, user.get("password_hash")):
            self._fail_form("login.html", "Log in", nav_for(None),
                            "Wrong email or password.", {"EMAIL": email}, status=401)
            return
        token = auth.create_session(user["id"])
        self._redirect("/dashboard", {"Set-Cookie": auth.set_cookie(token)}, status=303,
                       flash="Signed in.")

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
        self._redirect(f"/dashboard?ok={urllib.parse.quote('Bound ' + number)}",
                       flash=f"{number} is now answered by CallDesk.")

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
        label = ("CallDesk answers directly" if mode == "agent"
                 else "Ring mobile first, then CallDesk")
        self._redirect(f"/settings/answering?number={urllib.parse.quote(number)}"
                       f"&ok={urllib.parse.quote('Saved ' + label)}",
                       flash=f"Answering mode saved for {number}.")

    def _own_number(self, user, requested=""):
        """The Twilio number to place a call from.

        The posted value is only a hint. Anything not bound to this account is
        discarded, because otherwise a customer could dial out from a number
        they do not own by editing a hidden form field.
        """
        bound = [row["phone_number"] for row in db.list_phone_numbers(user["id"])]
        if not bound:
            return ""
        wanted = (requested or "").strip()
        return wanted if wanted in bound else bound[0]

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
        from_number = self._own_number(user, form.get("from_number"))
        if not from_number:
            self._redirect("/calls?error=Bind+a+phone+number+before+calling+back.")
            return
        callback = f"{self._base_url()}/twiml/outbound?caller={urllib.parse.quote(target)}"
        try:
            sid = tc.place_outbound_call(conn["account_sid"], target, from_number, callback)
        except tc.TwilioError as err:
            self._redirect("/calls?error=" + urllib.parse.quote(str(err)[:250]))
            return
        self._redirect(
            f"/calls?ok={urllib.parse.quote('Calling ' + target + ' back (call ' + (sid or 'placed') + ')')}",
            flash=f"Calling {target} back.")

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
