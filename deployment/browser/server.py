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
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

import agent_manager  # noqa: E402
import auth  # noqa: E402
import db  # noqa: E402
import google_auth  # noqa: E402
import email_sender  # noqa: E402
import reminders  # noqa: E402
import twilio_connect as tc  # noqa: E402
from lib import (ApiError, aai, load_env, publish_agent, read_agent,  # noqa: E402
                 required, stored_agent_id, twilio)

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
       "TWILIO_BANNER", "ERROR", "PAGER", "ROOMS", "BOOKINGS", "DEPARTMENTS",
    "DEPARTMENT_OPTIONS", "SLOT_NOTICE", "SLOTS", "APPOINTMENTS", "TABLES", "RESERVATIONS", "COUNTS",
    "AGENT_BANNER", "VERTICAL", "CHECKLIST", "GUIDE_HINT", "REMINDERS"}

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

URGENCIES = ("emergency", "high", "medium", "low")
CONNECT_NOT_CONFIGURED = (
    "Twilio Connect not configured. To enable phone integration, an admin "
    "must create a Twilio Connect App (requires an upgraded Twilio account) "
    "and set TWILIO_CONNECT_APP_SID in .env. The code for the full Connect "
    "flow is already implemented and ready to activate."
)




def google_login_page(message=""):
    """The login page, carrying a Google failure back to the customer."""
    return page("login.html", "Log in", nav_for(None),
                ERROR=error_box(message) if message else "")


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
            '<a href="/guide">Guide</a>'
            '<a href="/login">Sign in</a>'
            '<a href="/signup">Sign up</a>'
            "</nav>"
            '<div class="header-right">'
            '<a class="btn btn-primary btn-sm" href="/signup">Get started</a>'
            "</div>"
        )
    name = esc(user["business_name"])
    # A service business has nothing to set up, so its setup page is where the
    # agent and its numbers live; the other three need it before a call can be
    # answered, so it sits next to Dashboard.
    setup_link = ('<a href="/setup">Setup</a>' if vertical_of(user) != "service" else "")
    return (
        '<nav class="nav-links" id="primary-nav">'
        '<a href="/dashboard">Dashboard</a>'
        + setup_link +
        '<a href="/numbers">Numbers</a>'
        '<a href="/calls">Calls</a>'
        '<a href="/guide">Guide</a>'
        '<a href="/settings/reminders">Settings</a>'
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
        '<a href="/settings/reminders" role="menuitem">Settings</a>'
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


# --- vertical table rows --------------------------------------------------
#
# One fragment per list on the setup pages. Every cell is escaped here, because
# these slots are inserted as trusted HTML and a guest name typed in by a caller
# is exactly the kind of value that must not reach the page unescaped.

def _row(cells, attrs=""):
    return f"<tr{attrs}>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"


def _delete_form(action, row_id, label):
    """A delete control that posts to its own route.

    A button rather than a link, because the route is POST-only: a GET would
    be a prefetchable link, and a crawler or a browser prefetch would delete
    somebody's room.
    """
    return (
        f'<form method="post" action="{action}" class="inline-form">'
        f'<input type="hidden" name="id" value="{int(row_id)}">'
        f'<button class="btn btn-danger btn-sm" type="submit">{esc(label)}</button>'
        "</form>"
    )


def room_rows(rooms):
    if not rooms:
        return empty_state(
            "&#127968;",
            "No rooms yet",
            "Add a room and the agent can start quoting availability on a call.",
        )
    rows = [
        _row([
            f"<strong>{esc(r['room_number'])}</strong>",
            esc(r["room_type"] or "standard"),
            esc(f"${r['price_per_night']:.0f}") if r["price_per_night"] is not None else "",
            esc(r["capacity"] or ""),
            esc(r["amenities"] or ""),
            _delete_form("/setup/hotel/rooms/delete", r["id"], "Delete"),
        ])
        for r in rooms
    ]
    return ('<div class="table-wrap"><table><thead><tr>'
            "<th>Room</th><th>Type</th><th>Nightly</th><th>Sleeps</th>"
            "<th>Amenities</th><th></th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table></div>")


def booking_rows(bookings):
    if not bookings:
        return empty_state("&#128197;", "No bookings yet",
                           "Bookings land here when the agent books a room on a call.")
    rows = [
        _row([
            esc(b.get("room_number") or ""),
            esc(b.get("guest_name") or ""),
            esc(b.get("guest_phone") or ""),
            f"{esc(b.get('check_in') or '')} to {esc(b.get('check_out') or '')}",
            status_badge(b.get("status") or "confirmed"),
        ])
        for b in bookings
    ]
    return ('<div class="table-wrap"><table><thead><tr>'
            "<th>Room</th><th>Guest</th><th>Phone</th><th>Stay</th><th>Status</th>"
            "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>")


def department_rows(departments):
    if not departments:
        return empty_state("&#128736;", "No departments yet",
                           "Add a department and its operating schedule.")
    rows = [
        _row([
            f"<strong>{esc(d['name'])}</strong><br><span class=\"muted\">{esc(d['description'] or '')}</span>",
            f"{esc(d['opening_time'])} to {esc(d['closing_time'])}<br>"
            f"{esc(d['slot_duration_minutes'])} min, {esc(d['daily_capacity'])} patients/day",
            esc(d['default_doctor'] or "Any doctor"),
            _schedule_form(d),
        ])
        for d in departments
    ]
    return ('<div class="table-wrap"><table><thead><tr>'
            "<th>Department</th><th>Schedule</th><th>Doctor</th><th>Update</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table></div>")


def _schedule_form(department):
    days = set((department.get("working_days") or "").split(","))
    checks = "".join(
        f'<label class="check-inline"><input type="checkbox" name="working_days" '
        f'value="{day}" {"checked" if day in days else ""}>{label}</label>'
        for day, label in (("mon", "Mon"), ("tue", "Tue"), ("wed", "Wed"),
                           ("thu", "Thu"), ("fri", "Fri"), ("sat", "Sat"),
                           ("sun", "Sun")))
    return (
        f'<form method="post" action="/setup/hospital/departments/update" class="schedule-form">'
        f'<input type="hidden" name="id" value="{int(department["id"])}">'
        f'<div class="checks">{checks}</div>'
        f'<input name="opening_time" type="time" value="{esc(department["opening_time"])}" required>'
        f'<input name="closing_time" type="time" value="{esc(department["closing_time"])}" required>'
        f'<input name="slot_duration_minutes" type="number" min="1" value="{int(department["slot_duration_minutes"])}" required>'
        f'<input name="daily_capacity" type="number" min="1" value="{int(department["daily_capacity"])}" required>'
        f'<input name="default_doctor" type="text" value="{esc(department["default_doctor"] or "")}" placeholder="Default doctor">'
        '<output class="schedule-preview">Schedule preview updates as you edit.</output>'
        '<button class="btn btn-secondary btn-sm" type="submit">Save schedule</button></form>'
    )


def hospital_capacity_rows(user_id):
    departments = db.list_departments(user_id)
    rows = []
    today = datetime.now(timezone.utc).date()
    for offset in range(7):
        date = (today + timedelta(days=offset)).strftime("%Y-%m-%d")
        for department in departments:
            if not db._generated_slots(department, date):
                continue
            booked = db.department_daily_count(user_id, department["id"], date)
            rows.append(_row([esc(date), esc(department["name"]),
                              esc(f"{booked} / {department['daily_capacity']}")]))
    if not rows:
        return '<p class="muted">No operating days configured in the next week.</p>'
    return ('<div class="table-wrap"><table><thead><tr><th>Date</th><th>Department</th>'
            '<th>Booked</th></tr></thead><tbody>' + ''.join(rows) +
            '</tbody></table></div>')


def appointment_rows(appointments):
    if not appointments:
        return empty_state("&#128100;", "No appointments yet",
                           "Appointments the agent books will be listed here.")
    rows = [
        _row([
            esc(a.get("patient_name") or ""),
            esc(a.get("department") or ""),
            esc(a.get("doctor_name") or ""),
            esc(a.get("slot_datetime") or ""),
            esc(a.get("patient_phone") or ""),
            esc(a.get("reason") or ""),
            badge("Emergency", "danger") if a.get("urgency") == "emergency" else badge("Routine", "neutral"),
            badge("SMS sent", "success") if a.get("reminder_sent") else badge("SMS pending", "warning"),
            '<span class="muted">No email</span>' if not a.get("patient_email") else
            (badge("Email sent", "success") if a.get("email_reminder_sent") else badge("Email pending", "warning")),
            status_badge(a.get("status") or "confirmed"),
            _confirmation_form(a),
        ])
        for a in appointments
    ]
    return ('<div class="table-wrap"><table><thead><tr>'
            "<th>Patient</th><th>Department</th><th>Doctor</th><th>When</th>"
            "<th>Phone</th><th>Reason</th><th>Urgency</th><th>SMS</th><th>Email</th><th>Status</th><th></th>"
            "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>")


def _confirmation_form(appointment):
    if not appointment.get("patient_email"):
        return '<span class="muted">No email</span>'
    return (
        '<form method="post" action="/appointments/resend-confirmation" class="inline-form">'
        f'<input type="hidden" name="appointment_id" value="{int(appointment["id"])}">'
        '<button class="btn btn-secondary btn-sm" type="submit">Resend confirmation</button>'
        '</form>'
    )


def table_rows(tables):
    if not tables:
        return empty_state("&#127978;", "No tables yet",
                           "Add a table and the agent can start checking availability.")
    rows = [
        _row([
            f"<strong>{esc(t['table_number'])}</strong>",
            esc(t["capacity"] or ""),
            status_badge(t.get("status") or "available"),
            _delete_form("/setup/restaurant/tables/delete", t["id"], "Delete"),
        ])
        for t in tables
    ]
    return ('<div class="table-wrap"><table><thead><tr>'
            "<th>Table</th><th>Seats</th><th>Status</th><th></th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table></div>")


def reservation_rows(reservations):
    if not reservations:
        return empty_state("&#127860;", "No reservations yet",
                           "Reservations the agent books will be listed here.")
    rows = [
        _row([
            esc(v.get("guest_name") or ""),
            esc(v.get("table_number") or ""),
            esc(v.get("party_size") or ""),
            esc(v.get("reservation_datetime") or ""),
            status_badge(v.get("status") or "confirmed"),
        ])
        for v in reservations
    ]
    return ('<div class="table-wrap"><table><thead><tr>'
            "<th>Guest</th><th>Table</th><th>Party</th><th>When</th><th>Status</th>"
            "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>")


def agent_banner(user):
    """Shown on every setup page: whether this account has an agent yet."""
    agent_id = agent_manager.get_user_agent(user["id"])
    if not agent_id:
        return note(
            "Your agent is not activated yet. Add what you sell, then activate it "
            "so calls can be answered.", "warn")
    return note(f"Agent <code>{esc(agent_id)}</code> is live and answering calls "
                f"as {esc(user['business_name'])}.", "ok")


def vertical_summary(user) -> str:
    """The vertical's own section on the dashboard.

    A hotel has rooms and bookings, a clinic has departments and slots, a
    restaurant has tables and reservations, and a service business has none of
    them, so it gets an empty string rather than an empty table.
    """
    vertical = vertical_of(user)
    uid = user["id"]
    counts = db.business_counts(uid)

    if vertical == "hotel":
        stats = [("Rooms", "rooms"), ("Room types", "room_types"), ("Bookings", "bookings")]
        body = (f'<h2 class="section-title">Recent bookings</h2>'
                + booking_rows(db.list_bookings(uid)))
        link = '<a class="btn btn-primary btn-sm" href="/setup/hotel/rooms">Manage rooms</a>'
    elif vertical == "hospital":
        stats = [("Departments", "departments"), ("Appointments", "appointments")]
        body = (f'<h2 class="section-title">Booked counts</h2>'
            + hospital_capacity_rows(uid)
            + f'<h2 class="section-title">Upcoming appointments</h2>'
            + appointment_rows(db.list_appointments(uid)))
        link = '<a class="btn btn-primary btn-sm" href="/setup/hospital">Manage schedules</a>'
    elif vertical == "restaurant":
        stats = [("Tables", "tables"), ("Reservations", "reservations")]
        body = (f'<h2 class="section-title">Recent reservations</h2>'
                + reservation_rows(db.list_reservations(uid)))
        link = ('<a class="btn btn-primary btn-sm" href="/setup/restaurant/tables">'
                "Manage tables</a>")
    else:
        return ""

    return (f'<section class="card vertical-card">'
            f'<div class="card-head"><h2 class="section-title">'
            f'{esc(db.BUSINESS_TYPE_LABELS[vertical])}</h2>{link}</div>'
            + counts_markup(counts, stats) + body + "</section>")


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


URGENCY_BADGE = {"emergency": "danger", "high": "danger", "medium": "warning", "low": "neutral"}

# Status words the vertical tables carry, mapped to badge colours. Anything
# unlisted shows as neutral rather than guessing.
STATUS_BADGE = {
    "available": "success", "confirmed": "success", "booked": "info",
    "occupied": "warning", "pending": "warning", "maintenance": "warning",
    "cancelled": "danger", "no_show": "danger", "out_of_service": "danger",
}


def status_badge(text):
    return badge(text or "", STATUS_BADGE.get((text or "").strip().lower(), "neutral"))


def counts_markup(counts: dict, pairs) -> str:
    """The stat grid on a setup page, from a business_counts() dict.

    Built here rather than in the template so the same three numbers render the
    same way whichever vertical is looking at them.
    """
    cells = "".join(
        f'<div class="card card-hover"><div class="stat-label">{esc(label)}</div>'
        f'<div class="card-value">{esc(counts.get(key, 0))}</div></div>'
        for label, key in pairs
    )
    return f'<div class="stat-grid">{cells}</div>'


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


_TICK = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" '
         'stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>')


def twilio_banner(user, message="", kind="warn"):
    """The onboarding card a new account needs, or a compact card once connected.

    A flash written by a redirect (a number bound, a connect that failed) still
    wins, because it is the answer to whatever the person just did.
    """
    if message:
        return note(message, kind)

    if db.get_twilio_connection(user["id"]):
        return (
            '<div class="card onboard onboard-done">'
            '<span class="onboard-check" aria-hidden="true">' + _TICK + "</span>"
            '<div class="onboard-head-body">'
            '<div class="card-title" style="margin:0">Twilio connected</div>'
            '<p class="muted" style="margin:0">Your CallDesk number is live. '
            'Calls will appear below. <a href="/guide">View guide</a></p>'
            "</div></div>"
        )

    # Connect can't work on a deployment with no Connect App, so the button is
    # shown inert and the admin note explains why, rather than a link that 400s.
    configured = tc.connect_configured()
    if configured:
        connect = ('<a class="btn btn-primary" href="/connect/twilio">Connect Twilio</a>')
    else:
        connect = ('<span class="btn btn-primary" aria-disabled="true" '
                   'title="Twilio Connect is not configured on this deployment">'
                   "Connect Twilio</span>")
    admin_note = "" if configured else note(esc(CONNECT_NOT_CONFIGURED), "warn")
    steps = "".join(
        f'<li><span class="onboard-num">{i}</span>'
        f'<span class="onboard-step-text">{text}</span></li>'
        for i, text in enumerate((
            "Create a free Twilio account",
            "Buy a phone number",
            "Connect it to CallDesk",
        ), start=1)
    )
    return (
        '<div class="card onboard">'
        '<h2 class="onboard-title">Get your CallDesk number in 5 minutes</h2>'
        '<p class="muted onboard-lede">Connect your Twilio account to start '
        "receiving calls. We'll walk you through it step by step.</p>"
        f'<ol class="onboard-steps">{steps}</ol>'
        '<div class="onboard-actions">'
        '<a class="btn btn-secondary" href="/guide">Read the full guide</a>'
        f"{connect}</div>"
        f"{admin_note}"
        "</div>"
    )


def reminder_card(user):
    settings = db.get_reminder_settings(user["id"])
    sms = badge("Enabled", "success") if settings["enabled"] else badge("Disabled", "neutral")
    email = badge("Enabled", "success") if settings["email_enabled"] else badge("Disabled", "neutral")
    return (
        '<div class="card reminder-card"><div class="row-between">'
        '<div><div class="card-title">Appointment reminders</div>'
        f'<p class="muted">SMS: {sms} &nbsp; Email: {email} &nbsp; '
        f'Hours before: {int(settings["hours_before"])}</p>'
        f'<p class="muted">Sent this week: {db.reminders_sent_this_week(user["id"])} total</p></div>'
        '<a class="btn btn-secondary btn-sm" href="/settings/reminders">Edit settings</a>'
        '</div></div>'
    )


def _reminder_preview(template, user, appointment=None):
    appointment = appointment or {"patient_name": "Michael", "patient_phone": "0700000000",
                                  "slot_datetime": "2026-10-03 09:00", "department": "Cardiology"}
    slot = str(appointment["slot_datetime"])
    date, _, time = slot.partition(" ")
    values = {"business_name": user["business_name"], "patient_name": appointment["patient_name"],
              "patient_phone": appointment["patient_phone"], "date": date, "time": time,
              "department": appointment["department"]}
    text = str(template or "")
    for key, value in values.items():
        text = text.replace("{" + key + "}", str(value))
    return text


def setup_checklist(user) -> str:
    """How far through setup this account is. Empty once all five are done.

    Each row is derived from what already exists, so the checklist cannot drift
    from the state it describes. The one item with no row of its own is the
    account itself, which is true by definition for anyone reading this page.
    """
    numbers = db.list_phone_numbers(user["id"])
    items = [
        ("Create a CallDesk account", True, "/dashboard"),
        ("Connect Twilio", bool(db.get_twilio_connection(user["id"])), "/numbers"),
        ("Bind a phone number", bool(numbers), "/numbers"),
        ("Set your answering mode",
         any(n.get("answering_mode") for n in numbers), "/settings/answering"),
        ("Test with a call", db.count_calls(user["id"]) > 0, "/talk"),
    ]
    if all(done for _, done, _ in items):
        return ""
    rows = ""
    for label, done, href in items:
        cls = " is-done" if done else ""
        box = "&#10003;" if done else ""
        go = "" if done else f'<a class="check-go" href="{href}">Do it</a>'
        rows += (f'<li class="check-row{cls}">'
                 f'<span class="check-box" aria-hidden="true">{box}</span>'
                 f'<span class="check-text">{label}</span>{go}</li>')
    remaining = sum(1 for _, done, _ in items if not done)
    return (
        '<div class="card onboard">'
        '<div class="row-between" style="margin-bottom:12px">'
        '<div class="card-title" style="margin:0">Finish setting up</div>'
        f'<span class="badge">{remaining} left</span></div>'
        f'<ol class="checklist">{rows}</ol></div>'
    )


AGENT = None
PAGE = ""

# Routes a browser only ever GETs, and so have no POST handler. Used to answer
# a stray POST with 405 instead of a misleading 404. /signup, /login, /logout,
# /numbers/bind, /settings/answering, /settings/reminders, /calls/callback and /tool/send_summary
# are deliberately absent: they are real POST routes.
GET_ONLY = {"/", "/dashboard", "/numbers", "/calls", "/talk", "/guide",
            "/setup", "/setup/hospital", "/setup/hotel/rooms",
            "/setup/hospital/departments",
            "/setup/restaurant/tables", "/settings/reminders",
            "/connect/twilio", "/connect/twilio/callback",
            "/auth/google", "/auth/google/callback",
            "/twiml/fallback", "/twiml/outbound", "/app.js",
            "/token", "/agent", "/static/style.css"}


# --- verticals ------------------------------------------------------------

# Which setup page an account's business_type gets. There is no cross-vertical
# navigation, so a hotel cannot reach the restaurant table form.
SETUP_TEMPLATE = {
    "service": "setup_service.html",
    "hotel": "setup_hotel.html",
    "hospital": "setup_hospital.html",
    "restaurant": "setup_restaurant.html",
}

# The vertical an account is, always normalized, so a row written before the
# column existed still gets a page.
def vertical_of(user) -> str:
    return db.normalize_business_type((user or {}).get("business_type"))


def type_options(selected="service") -> dict:
    """One 'selected' attribute on the right signup option, none on the rest.

    A form that failed validation has to come back showing what the person
    chose, which is the whole point of keeping the value across the redirect.
    """
    chosen = db.normalize_business_type(selected)
    return {f"TYPE_{name.upper()}": "selected" if name == chosen else ""
            for name in db.BUSINESS_TYPES}


def setup_template_for(user) -> str:
    return SETUP_TEMPLATE[vertical_of(user)]


# POST /tool/<name> -> the db call behind one agent tool. The handler wraps
# whatever these return in {"ok": true, "data": ...} and turns a ValueError
# into {"ok": false, "error": ...}, so a tool can raise a message written for
# the caller and the agent will read it out.
TOOL_CHECK_AVAILABILITY = "check_availability"
TOOL_BOOK_ROOM = "book_room"
TOOL_CHECK_SLOTS = "check_slots"
TOOL_BOOK_APPOINTMENT = "book_appointment"
TOOL_CHECK_TABLES = "check_tables"
TOOL_BOOK_TABLE = "book_table"



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
        return {k: v if k == "working_days" else v[0] for k, v in parsed.items()}

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
            self._agent_for_user()
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
            self._talk_page()
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
                            EMAIL=self._one("email")))
            return

        if path == "/signup":
            if self._user():
                self._redirect("/dashboard")
                return
            self._html(page("signup.html", "Sign up", nav_for(None),
                            EMAIL=self._one("email"),
                            BUSINESS_NAME=self._one("business_name"),
                            MOBILE_NUMBER=self._one("mobile_number"),
                            **type_options(self._one("business_type"))))
            return

        if path == "/auth/google":
            self._google_start()
            return

        # Public on purpose: a business owner has to be able to read what this
        # involves before deciding to sign up or hand over a Twilio account.
        if path == "/guide":
            self._html(page("guide.html", "Get connected", nav_for(self._user())))
            return

        if path == "/auth/google/callback":
            self._google_callback()
            return

        if path == "/dashboard":
            self._dashboard()
            return
        # Every setup page. /setup picks the one this account's vertical uses;
        # the others are the same page reached by an explicit sub-path.
        if path in ("/setup", "/setup/hospital", "/setup/hotel/rooms", "/setup/hospital/departments",
                    "/setup/restaurant/tables"):
            setup_user = self._require_user()
            if not setup_user:
                return
            self._setup_page(setup_user, path)
            return
        if path == "/numbers":
            self._numbers()
            return
        if path == "/settings/answering":
            self._answering_form()
            return
        if path == "/settings/reminders":
            self._reminder_settings_page()
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

    NO_AGENT_NOTICE = (
        "You haven't set up your business yet. Complete your setup to get a "
        "personalized agent."
    )

    def _resolve_voice_agent(self):
        """Which agent this session talks to, and what to tell them about it.

        The per-account agent wins. A hotel on its own published agent must
        never be handed the deployment-wide default, which is a service
        business and would answer a caller as one.

        Three states, and they are genuinely different:
          own      the account has activated an agent
          pending  signed in, nothing published yet, so the global one answers
          visitor  not signed in, so there is nobody to personalise for
        """
        user = self._user()
        own = (user["agent_id"] or "").strip() if user else ""
        if own:
            return {"id": own, "own": True, "notice": None}
        return {"id": AGENT["id"], "own": False,
                "notice": self.NO_AGENT_NOTICE if user else None}

    def _agent_for_user(self) -> None:
        """The agent the voice page will open a session with.

        app.js takes the id straight off this payload and puts it in
        session.update, so this is the one place that decides whose voice an
        inbound call reaches.
        """
        chosen = self._resolve_voice_agent()
        try:
            agent = aai(f"/agents/{chosen['id']}")
        except ApiError as err:
            print(err)
            self._send(502, b'{"error":"could not load the agent"}', "application/json")
            return
        payload = public_agent(agent)
        # The starter's app.js only reads `id`, so an extra key is inert there
        # and this is where the page learns whether it is on the right agent.
        payload["notice"] = chosen["notice"]
        payload["personalized"] = chosen["own"]
        self._send(200, json.dumps(payload).encode(), "application/json")

    def _talk_page(self) -> None:
        """/talk, with the agent named for whoever is signed in.

        The page is the starter's, so only the two agent slots and the notice
        change between accounts. app.js reads window.AGENT for `id` and `name`
        and nothing else, so those two are all that gets injected: the system
        prompt has no business being in the page source on every page load.
        """
        chosen = self._resolve_voice_agent()
        user = self._user()
        name = AGENT["name"]
        if chosen["own"]:
            name = f"{user['business_name']} agent"
        banner = note(esc(chosen["notice"]), "warn") if chosen["notice"] else ""

        injected = json.dumps({"id": chosen["id"], "name": name}).replace("<", "\\u003c")
        self._html(PAGE.replace("{{AGENT_NAME}}", esc(name))
                       .replace("{{AGENT_JSON}}", injected)
                       .replace("{{AGENT_NOTICE}}", banner)
                       .encode())

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
            BUSINESS_TYPE_LABEL=db.BUSINESS_TYPE_LABELS[vertical_of(user)],
            TWILIO_BANNER=twilio_banner(user, message, kind),
            REMINDERS=reminder_card(user),
            NUMBERS=numbers_table(user["id"]),
            RECENT_CALLS=calls_table(user["id"], limit=10),
            CALLS_TODAY=db.count_calls(user["id"], since=utc_day()),
            CALLS_WEEK=db.count_calls(user["id"], since=utc_day(days_ago=7)),
            NUMBERS_COUNT=len(numbers),
            VERTICAL=vertical_summary(user),
            CHECKLIST=setup_checklist(user),
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
        # Only when there is nothing to read on this page yet, which is the
        # whole point of the link.
        guide = "" if conn else (
            '<p class="muted" style="margin:0 0 16px">'
            'Not sure how? <a href="/guide">Read the guide</a></p>')
        self._html(page(
            "numbers.html", "Phone numbers", nav_for(user),
            ERROR=error_box(self._one("error")) if not conn else "",
            TWILIO_BANNER=banner,
            GUIDE_HINT=guide,
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

    def _reminder_settings_page(self) -> None:
        user = self._require_user()
        if not user:
            return
        settings = db.get_reminder_settings(user["id"])
        email_subject = settings["email_subject_template"]
        email_body = settings["email_body_template"]
        self._html(page(
            "reminders.html", "Appointment reminders", nav_for(user),
            ERROR=error_box(self._one("error")),
            SMS_ENABLED="checked" if settings["enabled"] else "",
            EMAIL_ENABLED="checked" if settings["email_enabled"] else "",
            HOURS_BEFORE=settings["hours_before"],
            SMS_TEMPLATE=settings["sms_message_template"],
            EMAIL_SUBJECT=email_subject,
            EMAIL_BODY=email_body,
            SMS_PREVIEW=_reminder_preview(settings["sms_message_template"], user),
            EMAIL_PREVIEW_SUBJECT=_reminder_preview(email_subject, user),
            EMAIL_PREVIEW_BODY=_reminder_preview(email_body, user),
        ))

    def _save_reminder_settings(self, form) -> None:
        user = self._require_user()
        if not user:
            return
        try:
            hours = max(0, int(form.get("hours_before") or 24))
            db.update_reminder_settings(user["id"], {
                "enabled": "enabled" in form,
                "hours_before": hours,
                "sms_message_template": form.get("sms_message_template"),
                "email_enabled": "email_enabled" in form,
                "email_subject_template": form.get("email_subject_template"),
                "email_body_template": form.get("email_body_template"),
            })
        except (TypeError, ValueError) as err:
            self._redirect("/settings/reminders?error=" + urllib.parse.quote(str(err)))
            return
        self._redirect("/settings/reminders", flash="Reminder settings saved.")

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
        # The vertical tools. All of them are POST-only, are called by
        # AssemblyAI rather than a browser, and share one envelope.
        vertical_tools = {
            "/tool/check_availability": self._tool_check_availability,
            "/tool/book_room": self._tool_book_room,
            "/tool/check_slots": self._tool_check_slots,
            "/tool/book_appointment": self._tool_book_appointment,
            "/tool/triage_symptoms": self._tool_triage_symptoms,
            "/tool/end_call": self._tool_end_call,
            "/tool/check_tables": self._tool_check_tables,
            "/tool/book_table": self._tool_book_table,
        }
        if path in vertical_tools:
            vertical_tools[path]()
            return

        # The inventory pages are both a form to read and a form to post, so
        # they are exempt from the GET_ONLY 405 below. /setup itself is not in
        # this list, which keeps it read-only.
        setup_posts = {
            "/setup/hotel/rooms": self._add_room,
            "/setup/hotel/rooms/delete": self._delete_room,
            "/setup/hospital/departments": self._add_department,
            "/setup/hospital/departments/update": self._update_department,
            "/setup/restaurant/tables": self._add_table,
            "/setup/restaurant/tables/delete": self._delete_table,
        }
        if path in setup_posts:
            setup_posts[path](self._form())
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
        if path == "/settings/reminders":
            self._save_reminder_settings(form)
            return
        if path == "/calls/callback":
            self._call_back(form)
            return
        if path == "/appointments/resend-confirmation":
            self._resend_confirmation(form)
            return

        if path == "/setup/publish-agent":
            self._publish_agent()
            return

        self._send(404, b'{"error":"not found"}', "application/json")

    def _resend_confirmation(self, form) -> None:
        user = self._require_user()
        if not user:
            return
        appointment = db.get_hospital_appointment(form.get("appointment_id"), user["id"])
        if not appointment:
            self._redirect("/dashboard?error=That+appointment+was+not+found.")
            return
        try:
            sent = email_sender.send_confirmation_email_for_appointment(
                appointment["id"], force=True)
        except Exception as err:
            self._redirect("/dashboard?error=" + urllib.parse.quote(str(err)[:200]))
            return
        if not sent:
            self._redirect("/dashboard?error=That+appointment+has+no+email+address.")
            return
        self._redirect("/dashboard", flash="Confirmation email sent.")

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
        # A public form, so an unknown value falls back to a service business
        # rather than being rejected: normalize_business_type decides.
        business_type = db.normalize_business_type(form.get("business_type"))
        keep = {"EMAIL": email, "BUSINESS_NAME": business, "MOBILE_NUMBER": mobile}
        keep.update(type_options(business_type))
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
        user_id = db.create_user(email, auth.hash_password(password), business, mobile,
                                 business_type)
        token = auth.create_session(user_id)
        print(f"New account: {business} <{email}> ({business_type})", flush=True)

        # Publish the agent immediately so /talk works on first login.
        try:
            agent_id = agent_manager.publish_user_agent(user_id)
            print(f"Auto-published agent for {business}: {agent_id}", flush=True)
        except Exception as err:
            print(f"Auto-publish failed for {business}: {err}", flush=True)

        # Straight to setup, since an account with no rooms or departments
        # cannot answer its first call.
        self._redirect("/setup", {"Set-Cookie": auth.set_cookie(token)}, status=303,
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
        agent_id = agent_manager.get_user_agent(user["id"])
        if not agent_id:
            self._redirect(
                "/setup?error=Activate+your+agent+before+binding+a+number.")
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
            tc.bind_number_to_agent(conn["account_sid"], number, agent_id)
        except tc.TwilioError as err:
            self._redirect("/numbers?error=" + urllib.parse.quote(str(err)[:250]))
            return
        db.add_phone_number(user["id"], number, agent_id=agent_id)
        print(f"Bound {number} to {agent_id} for {user['business_name']}", flush=True)
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

    # --- agent tools for the vertical availability endpoints --------------
    #
    # Every one of these answers the same envelope, because the agent has to
    # tell a caller either that it worked or why it did not:
    #
    #     {"ok": true,  "data": {...}}
    #     {"ok": false, "error": "a sentence the agent can read out"}
    #
    # The error is always HTTP 200. A tool that answers 4xx is treated as a
    # dead endpoint, and the agent then tells the caller the system is down
    # instead of reading the reason.

    def _tool_json(self, payload: dict, status: int = 200) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json")

    def _tool_ok(self, data: dict) -> None:
        self._tool_json({"ok": True, "data": data})

    def _tool_fail(self, message: str) -> None:
        self._tool_json({"ok": False, "error": message or "something went wrong"})

    def _tool_args(self) -> Optional[dict]:
        """The JSON body AssemblyAI posted, or None after answering."""
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            args = json.loads(raw)
        except json.JSONDecodeError:
            self._tool_fail("I could not read that request. Please try again.")
            return None
        if not isinstance(args, dict):
            self._tool_fail("I could not read that request. Please try again.")
            return None
        return args

    def _tool_user(self, args: dict, business_type: str) -> Optional[dict]:
        """The account a tool call belongs to.

        Three ways to know, best first:

        1. The `account` query parameter the agent was published with. That is
           the agent's own owner, so it is always right, and it is the only one
           that still works when the call carries no number.
        2. The number that received the call, when the model passed one along.
        3. The oldest account of that vertical, which is what keeps a
           single-tenant demo working and is logged every time so the fallback
           is never silent.

        Whatever identifies the account, it has to be the right vertical: a
        number bound to a different kind of business must not read or write
        this vertical's tables.
        """
        claimed = self._one("account")
        if claimed and str(claimed).isdigit():
            owner = db.get_user_by_id(int(claimed))
            if owner and vertical_of(owner) == business_type:
                return owner
            # A published agent pointing at an account of another vertical is a
            # publishing bug, not a caller error, so say nothing to the caller.
            print(f"tool: agent claims account {claimed} for {business_type}, "
                  "which it is not; ignoring it", flush=True)
            self._tool_fail("this number is not set up for that service")
            return None

        called = ""
        for key in ("to", "to_number", "phone_number", "called_number"):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                called = value.strip()
                break

        owner = db.owner_of_number(called) if called else None
        user = db.get_user_by_id(owner["user_id"]) if owner else None
        if user:
            if vertical_of(user) != business_type:
                # A number bound to a different kind of business must not read
                # or write this vertical's tables.
                self._tool_fail("this number is not set up for that service")
                return None
            return user

        user = db.first_user_of_type(business_type)
        if not user:
            self._tool_fail(f"no {business_type} account has been set up yet")
            return None
        print(f"tool: no {called or 'to number'} on the call, answered for "
              f"{user['business_name']} (first {business_type} account)", flush=True)
        return user

    def _tool_run(self, business_type: str, work) -> None:
        """Run one tool body and return the result directly.

        The agent reads the raw JSON body, so the data is not wrapped in an
        envelope. An error is returned as {"error": "..."} which the agent
        can speak.
        """
        args = self._tool_args()
        if args is None:
            return
        user = self._tool_user(args, business_type)
        if user is None:
            return
        try:
            self._tool_json(work(args, user))
        except ValueError as err:
            print(f"tool: {business_type} rejected a call: {err}", flush=True)
            self._tool_json({"error": str(err)})
        except Exception as err:
            print(f"tool: {business_type} failed: {err!r}", flush=True)
            self._tool_json({"error": "our system could not complete that "
                             "just now. Please call back in a moment."})

    # hotel

    def _tool_check_availability(self) -> None:
        def work(args, user):
            rooms = db.find_available_rooms(
                user["id"], args.get("check_in"), args.get("check_out"),
                args.get("room_type"))
            return {
                "available": bool(rooms),
                "room_count": len(rooms),
                "rooms": [{"room_number": r["room_number"], "room_type": r["room_type"],
                           "price_per_night": r["price_per_night"],
                           "capacity": r["capacity"], "amenities": r["amenities"] or ""}
                          for r in rooms],
            }
        self._tool_run("hotel", work)

    def _tool_book_room(self) -> None:
        def work(args, user):
            return db.book_room(
                user["id"], args.get("room_number"), args.get("guest_name"),
                args.get("guest_phone"), args.get("check_in"), args.get("check_out"))
        self._tool_run("hotel", work)

    # hospital

    def _tool_check_slots(self) -> None:
        def work(args, user):
            print(f"check_slots {json.dumps(args, sort_keys=True)}", flush=True)
            department = args.get("department")
            requested_date = args.get("date")
            dept, requested, target = db.schedule_info(user["id"], department, requested_date)
            result = {"slots": [], "date": target, "next_available": target}
            if requested_date and target != requested:
                result["message"] = f"That date is not an operating day. The next available day is {target}."
            count = db.department_daily_count(user["id"], dept["id"], target)
            if count >= int(dept["daily_capacity"] or 16):
                result["message"] = "Department is fully booked on that date."
                result["next_available"] = db.next_available_date(user["id"], department, target)
                print(f"check_slots result {json.dumps(result, sort_keys=True)}", flush=True)
                return result
            slots = db.find_available_slots(user["id"], department, requested_date)
            result["slots"] = [
                {"doctor": s["doctor_name"] or "",
                 "datetime": s["slot_datetime"],
                 "duration_minutes": s["duration_minutes"]}
                for s in slots
            ]
            if not slots:
                result["next_available"] = db.next_available_date(user["id"], department, target)
            print(f"check_slots result {json.dumps(result, sort_keys=True)}", flush=True)
            return result
        self._tool_run("hospital", work)

    def _tool_book_appointment(self) -> None:
        def work(args, user):
            result = db.book_appointment(
                user["id"], args.get("department"), args.get("slot_datetime"),
                args.get("patient_name"), args.get("patient_phone"), args.get("reason"),
                patient_email=args.get("patient_email"))
            try:
                email_sender.send_confirmation_email_for_appointment(result["appointment_id"])
            except Exception as err:
                print(f"Confirmation email failed: {err}", flush=True)
            return result
        self._tool_run("hospital", work)

    def _tool_triage_symptoms(self) -> None:
        def work(args, user):
            symptoms = (args.get("symptoms") or "").strip()
            emergency_words = (
                "chest pain", "can't breathe", "cannot breathe", "severe bleeding",
                "stroke", "suicidal", "overdose", "severe allergic reaction",
            )
            emergency = any(word in symptoms.lower() for word in emergency_words)
            if emergency:
                db.log_call(user["id"], "Emergency triage", "", symptoms, "emergency")
                return {"risk": "emergency",
                        "message": "Tell the caller to hang up and call 911 immediately."}
            return {"risk": "routine"}
        self._tool_run("hospital", work)

    def _tool_end_call(self) -> None:
        args = self._tool_args()
        if args is None:
            return
        user = self._tool_user(args, "hospital")
        if user is None:
            return
        reason = args.get("reason", "completed")
        print(f"end_call reason={reason}", flush=True)
        try:
            email_sender.send_confirmation_email_for_latest_appointment(user["id"])
        except Exception as err:
            print(f"Confirmation email failed: {err}", flush=True)
        self._tool_ok({"message": "Goodbye"})

    # restaurant

    def _tool_check_tables(self) -> None:
        def work(args, user):
            tables = db.find_available_tables(
                user["id"], args.get("datetime"), args.get("party_size"))
            if not tables:
                return {"message": "No tables are available at that time. "
                                   "Offer a different time."}
            first = tables[0]
            return {
                "message": f"{len(tables)} tables are available. "
                           f"Offer to book table {first['table_number']} "
                           f"(seats {first['capacity']}) at the requested time.",
                "tables": [{"table_number": t["table_number"], "capacity": t["capacity"]}
                           for t in tables],
            }
        self._tool_run("restaurant", work)

    def _tool_book_table(self) -> None:
        def work(args, user):
            return db.book_table(
                user["id"], args.get("table_number"), args.get("guest_name"),
                args.get("guest_phone"), args.get("party_size"),
                args.get("reservation_datetime"))
        self._tool_run("restaurant", work)

    def _setup_page(self, user, path) -> None:
        """Render the setup page for this account's vertical.

        One method for all four, because an account only ever sees its own: the
        template comes from business_type, and /setup/hotel/rooms on a
        restaurant account is refused rather than rendered.
        """
        vertical = vertical_of(user)
        if path != "/setup" and vertical != path.split("/")[2]:
            self._redirect("/setup", flash="That setup page belongs to a different "
                           "kind of business.", kind="err")
            return

        title = "Setup"
        agent_id = agent_manager.get_user_agent(user["id"])
        values = {
            "AGENT_BANNER": agent_banner(user),
            "AGENT_ID": esc(agent_id or ""),
            "BUSINESS_NAME": user["business_name"],
            "BUSINESS_TYPE_LABEL": db.BUSINESS_TYPE_LABELS[vertical],
            # The same button publishes again, so an owner who changes a price
            # or a room can refresh the agent without hunting for a second one.
            "ACTIVATE_LABEL": "Update agent" if agent_id else "Activate agent",
        }

        if vertical == "hotel":
            title = "Rooms and bookings"
            values.update(
                COUNTS=counts_markup(db.business_counts(user["id"]),
                                      [("Rooms", "rooms"), ("Room types", "room_types"),
                                       ("Bookings", "bookings")]),
                ROOMS=room_rows(db.list_rooms(user["id"])),
                BOOKINGS=booking_rows(db.list_bookings(user["id"])),
            )
        elif vertical == "hospital":
            title = "Departments and schedules"
            departments = db.list_departments(user["id"])
            values.update(
                COUNTS=counts_markup(db.business_counts(user["id"]),
                                     [("Departments", "departments"),
                                      ("Appointments", "appointments")]),
                DEPARTMENTS=department_rows(departments),
                APPOINTMENTS=appointment_rows(db.list_appointments(user["id"])),
            )
        elif vertical == "restaurant":
            title = "Tables and reservations"
            values.update(
                COUNTS=counts_markup(db.business_counts(user["id"]),
                                     [("Tables", "tables"),
                                      ("Reservations", "reservations")]),
                TABLES=table_rows(db.list_tables(user["id"])),
                RESERVATIONS=reservation_rows(db.list_reservations(user["id"])),
            )
        else:
            # A service business has no inventory, so the page is the agent
            # itself: the send_summary tools and where its calls go.
            self._html(page(
                "setup_service.html", "Setup", nav_for(user),
                **values,
                NUMBERS=numbers_table(user["id"]),
                RECENT_CALLS=calls_table(user["id"], limit=5),
            ))
            return

        self._html(page(setup_template_for(user), title, nav_for(user), **values))

    def _setup_back(self, message, kind="ok", path="/setup"):
        """Back to the account's own setup page with a toast.

        Everything on these pages is a POST/redirect/GET, so a refresh does not
        resubmit, and the flash survives the round trip through the cookie.
        """
        self._redirect(path, flash=message, kind=kind)

    def _publish_agent(self) -> None:
        user = self._require_user()
        if not user:
            return
        try:
            agent_id = agent_manager.publish_user_agent(user["id"])
        except agent_manager.AgentError as err:
            self._setup_back(str(err), "err")
            return
        self._setup_back(f"Your agent is live. Agent id {agent_id}.")

    def _add_room(self, form) -> None:
        user = self._require_user()
        if not user:
            return
        if vertical_of(user) != "hotel":
            self._setup_back("That is a hotel setup page.", "err")
            return
        number = (form.get("room_number") or "").strip()
        if not number:
            self._setup_back("Give the room a number.", "err")
            return
        try:
            capacity = int(form.get("capacity") or 1)
            price = float(form.get("price_per_night") or 0)
        except ValueError:
            self._setup_back("Price and sleepers have to be numbers.", "err")
            return
        db.add_room(user["id"], number, form.get("room_type") or "standard",
                    price, capacity, form.get("amenities"))
        self._setup_back(f"Room {number} added.")

    def _delete_room(self, form) -> None:
        user = self._require_user()
        if not user:
            return
        if vertical_of(user) != "hotel":
            self._setup_back("That is a hotel setup page.", "err")
            return
        try:
            db.delete_room(user["id"], form.get("id"))
        except (ValueError, TypeError) as err:
            self._setup_back(str(err) or "That room could not be removed.", "err")
            return
        self._setup_back("Room removed.")

    def _add_department(self, form) -> None:
        user = self._require_user()
        if not user:
            return
        if vertical_of(user) != "hospital":
            self._setup_back("That is a hospital setup page.", "err")
            return
        name = (form.get("name") or "").strip()
        if not name:
            self._setup_back("Give the department a name.", "err", "/setup/hospital")
            return
        try:
            db.add_department(
                user["id"], name, form.get("description"), form.get("opening_time"),
                form.get("closing_time"), form.get("slot_duration_minutes"),
                form.get("working_days"), form.get("daily_capacity"),
                form.get("default_doctor"),
            )
        except (ValueError, TypeError) as err:
            self._setup_back(str(err), "err", "/setup/hospital")
            return
        self._setup_back(f"Department {name} added.", path="/setup/hospital")

    def _update_department(self, form) -> None:
        user = self._require_user()
        if not user:
            return
        if vertical_of(user) != "hospital":
            self._setup_back("That is a hospital setup page.", "err")
            return
        try:
            db.update_department_schedule(
                user["id"], form.get("id"), form.get("opening_time"),
                form.get("closing_time"), form.get("slot_duration_minutes"),
                form.get("working_days"), form.get("daily_capacity"),
                form.get("default_doctor"),
            )
        except (ValueError, TypeError) as err:
            self._setup_back(str(err), "err", "/setup/hospital")
            return
        self._setup_back("Department schedule saved.", path="/setup/hospital")

    def _add_table(self, form) -> None:
        user = self._require_user()
        if not user:
            return
        if vertical_of(user) != "restaurant":
            self._setup_back("That is a restaurant setup page.", "err")
            return
        number = (form.get("table_number") or "").strip()
        if not number:
            self._setup_back("Give the table a number.", "err")
            return
        try:
            capacity = int(form.get("capacity") or 2)
        except ValueError:
            self._setup_back("Seats has to be a number.", "err")
            return
        if capacity < 1:
            self._setup_back("A table has to seat at least one.", "err")
            return
        db.add_table(user["id"], number, capacity)
        self._setup_back(f"Table {number} added.")

    def _delete_table(self, form) -> None:
        user = self._require_user()
        if not user:
            return
        if vertical_of(user) != "restaurant":
            self._setup_back("That is a restaurant setup page.", "err")
            return
        try:
            db.delete_table(user["id"], form.get("id"))
        except (ValueError, TypeError) as err:
            self._setup_back(str(err) or "That table could not be removed.", "err")
            return
        self._setup_back("Table removed.")

    def _file_call(self, args: dict) -> None:
        """Attribute the call to an account, then store it.

        The tool posts only the arguments the model filled in, so the number
        that received the call is not normally present. Account-stamped agent
        URLs take precedence; the number and first-user fallbacks keep older
        agents and a seeded single-tenant demo working.
        """
        claimed = self._one("account")
        user = None
        if claimed and str(claimed).isdigit():
            user = db.get_user_by_id(int(claimed))
            if not user:
                print(f"send_summary: unknown account {claimed}, call not stored", flush=True)
                return

        called = ""
        for key in ("to", "to_number", "phone_number", "called_number"):
            if isinstance(args.get(key), str) and args[key].strip():
                called = args[key].strip()
                break

        owner = db.owner_of_number(called) if called else None
        if user is None:
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


def send_appointment_reminder(appointment: dict) -> bool:
    account = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    sender = os.environ.get("TWILIO_PHONE_NUMBER", "").strip()
    recipient = (appointment.get("patient_phone") or "").strip()
    if not (account and token and sender and recipient):
        print(f"reminder skipped for appointment {appointment['id']}: Twilio SMS is not configured",
              flush=True)
        return False
    url = (f"https://api.twilio.com/2010-04-01/Accounts/"
           f"{urllib.parse.quote(account, safe='')}/Messages.json")
    body = (f"Reminder: your appointment is scheduled for "
            f"{appointment['slot_datetime']} at {appointment['business_name']}.")
    try:
        twilio(url, form={"To": recipient, "From": sender, "Body": body},
               account=account, token=token)
    except (ApiError, OSError) as err:
        print(f"reminder failed for appointment {appointment['id']}: {err}", flush=True)
        return False
    print(f"reminder sent for appointment {appointment['id']} to {recipient}", flush=True)
    return True


def run_appointment_reminders() -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for appointment in db.due_hospital_reminders(now):
        if send_appointment_reminder(appointment):
            db.mark_reminder_sent(appointment["id"])


def appointment_reminder_loop() -> None:
    while True:
        try:
            run_appointment_reminders()
        except Exception as err:  # keep the server alive if one scan fails
            print(f"reminder scheduler failed: {err!r}", flush=True)
        threading.Event().wait(300)


def main() -> None:
    global AGENT, PAGE
    load_env()
    required("ASSEMBLYAI_API_KEY", "get one at https://www.assemblyai.com/dashboard/api-keys")

    db.init_db()
    db.purge_expired_sessions()
    threading.Thread(target=reminders.reminder_loop,
                     name="appointment-reminders", daemon=True).start()

    AGENT = resolve_agent()
    # Left as the raw template on purpose. Which agent a session gets depends
    # on who is signed in, so the agent name and the JSON app.js reads are
    # filled in per request by _talk_page, not once here.
    PAGE = (HERE / "index.html").read_text()

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
