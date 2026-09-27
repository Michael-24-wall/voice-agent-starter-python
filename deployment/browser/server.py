#!/usr/bin/env python3
"""CallDesk: talk to your agent from a browser tab, and manage the business
around it.

    python deployment/browser/server.py

Three things live in this file:

  /talk, /token, /agent, /app.js   the voice UI and the plumbing it needs
  /tool/send_summary                where the agent posts a finished call
  everything else                   the SaaS: signup, dashboard, numbers, calls

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
RAW = {"CONTENT", "NAV", "NUMBERS", "RECENT_CALLS", "CALLS", "BIND_FORM",
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
    return ('<nav><a href="/login">Log in</a><a href="/signup">Sign up</a></nav>')


def numbers_table(user_id, compact=False):
    rows = db.list_phone_numbers(user_id)
    if not rows:
        return '<p class="empty">No numbers bound yet.</p>'
    if compact:
        cells = "".join(
            f'<tr><td class="mono">{esc(r["phone_number"])}</td>'
            f'<td><span class="pill {esc("ok" if r["agent_id"] else "off")}">'
            f'{"bound" if r["agent_id"] else "not bound"}</span></td></tr>'
            for r in rows
        )
        return f"<table><tbody>{cells}</tbody></table>"
    cells = "".join(
        f'<tr><td class="mono">{esc(r["phone_number"])}</td>'
        f'<td class="mono muted">{esc(r["twilio_sid"] or "-")}</td>'
        f'<td><span class="pill {esc("ok" if r["agent_id"] else "off")}">'
        f'{"bound" if r["agent_id"] else "not bound"}</span></td>'
        f'<td class="mono muted">{esc(str(r["created_at"] or ""))}</td></tr>'
        for r in rows
    )
    return ("<table><thead><tr><th>Number</th><th>Twilio SID</th><th>Status</th>"
            f"<th>Added</th></tr></thead><tbody>{cells}</tbody></table>")


def calls_table(user_id, limit=None, with_transcript=False):
    rows = db.list_calls(user_id, limit=limit)
    if not rows:
        return '<p class="empty">No calls logged yet.</p>'
    out = []
    for r in rows:
        transcript = (r.get("transcript") or "").strip()
        expand = ""
        if with_transcript:
            body = esc(transcript) if transcript else "No transcript was stored for this call."
            expand = (f"<details><summary>View transcript</summary>"
                      f'<div class="transcript">{body}</div></details>')
        out.append(
            f'<tr>'
            f'<td>{esc(str(r.get("created_at") or ""))}</td>'
            f'<td>{esc(r.get("caller_name") or "Unknown")}</td>'
            f'<td class="mono">{esc(r.get("callback_number") or "-")}</td>'
            f'<td>{esc(r.get("problem") or "-")}</td>'
            f'<td><span class="pill {urgency_class(r.get("urgency"))}">'
            f'{esc(urgency_class(r.get("urgency")))}</span></td>'
            f"<td>{expand}</td>"
            f"</tr>"
        )
    head = ("<table><thead><tr><th>When</th><th>Caller</th><th>Number</th>"
            "<th>Problem</th><th>Urgency</th>")
    if with_transcript:
        head += "<th>Transcript</th>"
    head += "</tr></thead>"
    return f'{head}<tbody>{"".join(out)}</tbody></table>'


def twilio_banner(user):
    """Connect state, or the demo-mode note from PART 4."""
    conn = db.get_twilio_connection(user["id"])
    if conn:
        return (f'<div class="note">Twilio connected as '
                f'<span class="mono">{esc(conn["account_sid"])}</span></div>')
    if tc.demo_mode():
        return ('<div class="note warn"><b>Twilio Connect not configured. '
                "Using demo number.</b><br>Set <span class=\"mono\">"
                "TWILIO_CONNECT_APP_SID</span> in <span class=\"mono\">.env</span> "
                "to connect a real Twilio account.</div>")
    return ('<div class="note warn"><b>No Twilio account connected yet.</b><br>'
            '<a href="/connect/twilio">Connect Twilio</a> to list and bind your '
            "numbers.</div>")


def bind_form(user, conn, error=""):
    """A plain number field, plus the Connect shortcut when it is missing."""
    note = f'<p class="err" style="margin-bottom:14px">{error}</p>' if error else ""
    if not conn:
        return (note + '<p class="muted">Connect your Twilio account first, then '
                'you can bind any number it owns.</p><div class="row">'
                '<a class="btn primary" href="/connect/twilio">Connect Twilio</a></div>')
    known = "".join(
        f'<option value="{esc(n["phone_number"])}">{esc(n["phone_number"])}'
        f'{" &middot; " + esc(n["friendly_name"]) if n["friendly_name"] else ""}</option>'
        for n in tc.available_numbers(user, conn["account_sid"], conn.get("auth_token") or "")
    ) or '<option value="">No numbers found on this account</option>'
    warn = ""
    if not tc.trunk_domain():
        warn = ('<div class="note err" style="margin:16px 0"><b>Cannot bind yet.</b> '
                '<span class="mono">TWILIO_TRUNK_DOMAIN</span> is not set, so '
                "AssemblyAI has nowhere to send the call. See "
                '<span class="mono">deployment/telephony/connect.py</span>.</div>')
    return (
        f'{note}{warn}'
        '<form method="post" action="/numbers/bind" class="panel">'
        '<div class="field"><label for="phone_number">Phone number (E.164)</label>'
        f'<input type="text" id="phone_number" name="phone_number" required '
        f'list="known_numbers" placeholder="+15551234567">'
        f'<datalist id="known_numbers">{known}</datalist></div>'
        '<div class="row"><button class="btn primary" type="submit">Bind to CallDesk</button>'
        '<span class="muted" style="font-size:13px">Registers the number with '
        "AssemblyAI and attaches your agent.</span></div></form>"
    )


AGENT = None
PAGE = ""


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

    def _redirect(self, location: str, extra: Optional[dict] = None) -> None:
        self.send_response(302)
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
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        one = lambda k: (query.get(k) or [""])[0]  # noqa: E731

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

        # --- SaaS pages ---
        if path == "/":
            user = self._user()
            if user:
                self._redirect("/dashboard")
                return
            self._html(page("landing.html", "Voice AI receptionist", nav_for(None)))
            return

        if path == "/login":
            if self._user():
                self._redirect("/dashboard")
                return
            self._html(page("login.html", "Log in", nav_for(None), EMAIL=one("email")))
            return

        if path == "/signup":
            if self._user():
                self._redirect("/dashboard")
                return
            self._html(page("signup.html", "Sign up", nav_for(None),
                            EMAIL=one("email"), BUSINESS_NAME=one("business_name")))
            return

        if path == "/dashboard":
            user = self._require_user()
            if not user:
                return
            calls = db.list_calls(user["id"])
            numbers = db.list_phone_numbers(user["id"])
            urgent = sum(1 for c in calls if urgency_class(c.get("urgency")) == "high")
            notice = one("notice")
            banner = twilio_banner(user)
            if notice == "demo":
                banner = ('<div class="note"><b>Demo mode.</b> Twilio Connect is '
                          "not configured, so a demo number was created.</div>") + banner
            elif notice == "error":
                banner = ('<div class="note err"><b>Could not connect Twilio.</b> '
                          f"{esc(one('message'))}</div>") + banner
            summary = (f"{len(numbers)} bound, answering with CallDesk" if numbers
                       else "none bound yet")
            self._html(page(
                "dashboard.html", "Dashboard", nav_for(user),
                BUSINESS_NAME=user["business_name"], EMAIL=user["email"],
                TWILIO_BANNER=banner,
                NUMBER_COUNT=len(numbers), NUMBER_SUMMARY=summary,
                CALL_COUNT=len(calls), URGENT_COUNT=urgent,
                NUMBERS=numbers_table(user["id"], compact=True),
                RECENT_CALLS=calls_table(user["id"], limit=10),
            ))
            return

        if path == "/numbers":
            user = self._require_user()
            if not user:
                return
            conn = db.get_twilio_connection(user["id"])
            self._html(page(
                "numbers.html", "Phone numbers", nav_for(user),
                TWILIO_BANNER=twilio_banner(user),
                NUMBERS=numbers_table(user["id"]),
                BIND_FORM=bind_form(user, conn, one("error")),
            ))
            return

        if path == "/calls":
            user = self._require_user()
            if not user:
                return
            calls = db.list_calls(user["id"])
            self._html(page(
                "calls.html", "Calls", nav_for(user),
                BUSINESS_NAME=user["business_name"], CALL_COUNT=len(calls),
                CALLS=calls_table(user["id"], with_transcript=True),
            ))
            return

        if path == "/connect/twilio":
            user = self._require_user()
            if not user:
                return
            if tc.demo_mode():
                # No Connect app to authorise against, so record a demo
                # AccountSid and let the rest of the flow run end to end.
                tc.handle_callback(tc.DEMO_ACCOUNT_SID, user)
                self._redirect("/dashboard?notice=demo")
                return
            url = tc.get_connect_url(user, state_token=self._session_token())
            if not url:
                self._redirect("/dashboard?notice=error&message="
                               "Could not build the Twilio Connect URL.")
                return
            self._redirect(url)
            return

        if path == "/connect/twilio/callback":
            user = self._user()
            state = one("state")
            if not user and state:
                user = auth.get_session_user(state)
            if not user:
                self._redirect("/login")
                return
            account_sid = one("AccountSid") or one("account_sid")
            if not account_sid:
                account_sid = tc.exchange_code_for_account(one("code")) or ""
            if not account_sid:
                self._redirect("/dashboard?notice=error&message="
                               "Twilio did not return an AccountSid.")
                return
            tc.handle_callback(account_sid, user)
            self._redirect("/dashboard")
            return

        self._html(b"<!doctype html><title>404</title><h1>404</h1>"
                   b'<p><a href="/">Home</a></p>', status=404)

    # --- POST -----------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]

        # Where the send_summary tool lands. Parses the body, files the call
        # against the owning account, prints the details, and confirms.
        if path == "/tool/send_summary":
            self._handle_send_summary()
            return

        form = self._form()

        if path == "/signup":
            email = (form.get("email") or "").strip().lower()
            password = form.get("password") or ""
            business = (form.get("business_name") or "").strip()
            if not (email and password and business):
                self._html(page("signup.html", "Sign up", nav_for(None),
                                ERROR='<div class="note err">All three fields are required.</div>',
                                EMAIL=email, BUSINESS_NAME=business), status=400)
                return
            if len(password) < 8:
                self._html(page("signup.html", "Sign up", nav_for(None),
                                ERROR='<div class="note err">Password must be at least 8 characters.</div>',
                                EMAIL=email, BUSINESS_NAME=business), status=400)
                return
            if db.get_user_by_email(email):
                self._html(page("signup.html", "Sign up", nav_for(None),
                                ERROR='<div class="note err">That email is already registered. Try logging in.</div>',
                                EMAIL=email, BUSINESS_NAME=business), status=400)
                return
            user_id = db.create_user(email, auth.hash_password(password), business)
            user = db.get_user_by_id(user_id)
            token = auth.create_session(user_id)
            print(f"New account: {business} <{email}>", flush=True)
            self._redirect("/dashboard", {"Set-Cookie": auth.set_cookie(token)})
            return

        if path == "/login":
            email = (form.get("email") or "").strip().lower()
            password = form.get("password") or ""
            user = db.get_user_by_email(email)
            if not user or not auth.verify_password(password, user.get("password_hash")):
                self._html(page("login.html", "Log in", nav_for(None),
                                ERROR='<div class="note err">Wrong email or password.</div>',
                                EMAIL=email), status=401)
                return
            token = auth.create_session(user["id"])
            self._redirect("/dashboard", {"Set-Cookie": auth.set_cookie(token)})
            return

        if path == "/logout":
            auth.delete_session(self._session_token())
            self._redirect("/", {"Set-Cookie": auth.clear_cookie()})
            return

        if path == "/numbers/bind":
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
                tc.bind_number_to_agent(conn["account_sid"], number, AGENT["id"],
                                        conn.get("auth_token") or "")
            except ValueError as err:
                self._redirect("/numbers?error=" + urllib.parse.quote(str(err)[:200]))
                return
            except ApiError as err:
                self._redirect("/numbers?error=" + urllib.parse.quote(str(err)[:200]))
                return
            db.add_phone_number(user["id"], number, agent_id=AGENT["id"])
            print(f"Bound {number} to {AGENT['id']} for {user['business_name']}", flush=True)
            self._redirect("/numbers")
            return

        self._send(404, b'{"error":"not found"}', "application/json")

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

    AGENT = resolve_agent()
    print(f"Agent: {AGENT['id']}")
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

    print(f"Database: {db.DB_PATH}")
    print(f"Site:     http://localhost:{port}")
    print(f"Voice UI: http://localhost:{port}/talk")
    print("Demo:     demo@calldesk.test / demo1234")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
