# CallDesk — Presentation Brief

## The pitch

CallDesk is a voice AI receptionist for local service businesses — plumbers, HVAC
techs, electricians, dentists. When the owner is under a sink or on another job
site, CallDesk answers the call, has a natural conversation with the caller,
captures their name, callback number, what is wrong and how urgent it is, then
files the whole thing to a dashboard with a transcript. The owner sees the missed
call on their phone and can call back from the same screen, already briefed on
what the problem is. The business keeps its own Twilio account and its own phone
number; Twilio bills them directly, and CallDesk is an application layered on top.

## The problem, and who it is for

Missed calls are the most expensive thing that happens to a small service
business. One caller who hears a ringing machine calls the next company on the
list, and nobody ever finds out. A single missed emergency job can be worth
hundreds of dollars. Receptionists cost money every hour whether or not anyone
calls. Voicemail collects nothing: no name, no number, no urgency, no record.

CallDesk is for owner-operated service businesses with one to five people, where
there is nobody to answer the phone during the working day. It is deliberately
not a contact centre, not an IVR, and not a scheduling system. It answers, it
takes details, and it tells you. Everything else is out of scope.

## Architecture

```
        Caller
          |
          |  inbound call, the business's own Twilio number
          v
        Twilio                      bills the business directly
          |
          |  SIP to the AssemblyAI voice agent
          v
   AssemblyAI Voice Agent
          |  transcribes, decides when to speak, calls a tool when the
          |  conversation ends
          v
    send_summary  (HTTP tool)
          |  POST {caller_name, callback_number, problem, urgency, transcript}
          v
    CallDesk server                 Python stdlib, ThreadingHTTPServer
          |  writes one row, scoped to the owning account
          v
   Owner notification               dashboard + call log
          |
          |  one tap
          v
    Outbound call-back              Twilio calls.create, into the agent again
```

Tenancy is enforced at one place: every number, connection and call row carries a
`user_id`, and every query filters on it. The account that owns the number that
received the call is the account the call is filed under.

## Tech stack

- Python 3.9+ standard library, `ThreadingHTTPServer`, no web framework
- SQLite via the `sqlite3` module, one file at `data/calldesk.db`, no ORM
- AssemblyAI Voice Agent API — realtime speech-to-text, LLM turn-taking, TTS, and
  SIP termination
- `send_summary` as an AssemblyAI HTTP tool, the seam between the agent and the app
- Twilio Connect for bring-your-own-account, SIP trunks, and TwiML Bin
- `twilio` SDK for outbound calls and number configuration
- `google-auth` and `google-auth-oauthlib` for Google OAuth 2.0 sign-in
- PBKDF2-SHA256 password hashing, sessions in SQLite, `HttpOnly` `SameSite=Lax`
  cookies
- Server-rendered HTML with `str.replace` templating, no build step, no client
  framework
- Deployed on a single Vultr VPS behind nginx or Caddy

## Customer flow

1. **Sign up.** Business name, email, password. Or "Sign in with Google".
2. **Connect Twilio.** One click redirects to Twilio. Sign up for a Twilio
   account or log into an existing one, then authorize CallDesk. Twilio sends back
   the account SID, which CallDesk stores against the user. No Twilio password is
   ever seen or stored by CallDesk.
3. **Get a number.** Buy one in the Twilio console, or use a number already there.
4. **Bind it.** `/numbers` lists the account's real numbers. Bind one and CallDesk
   imports it to AssemblyAI and attaches the CallDesk agent to it.
5. **Pick an answering mode.** Mode A sends the caller straight into the agent.
   Mode B rings the owner's mobile for 20 seconds first, then falls through to the
   agent.
6. **Take calls.** The agent answers, collects the details, and the call lands in
   the dashboard with a transcript.
7. **Call back.** One button on the call row places an outbound call from the
   bound number into the agent, so the owner is briefed before they pick up.

## Three-minute demo script

**0:00 — The problem (30s).** Say it plainly: "If a plumber is under a sink and
the phone rings, that call is a job they will never know about." No slides, no
setup talk.

**0:30 — The product, cold (60s).** Open `/` and read the headline. Then `/talk`
and place a real call to the voice agent from the browser: introduce yourself,
describe a burst pipe in a basement, say it is urgent. Watch the transcript build
in the events pane. Hang up. This is the demo's centrepiece — the latency figures
on screen matter, so a wired connection is worth having.

**1:30 — The seam (30s).** Go back to the terminal and show the `send_summary`
tool log line the call just produced. That one HTTP POST is the entire
integration: the agent is a black box that calls a URL. Show the call appear in
the dashboard with its transcript and urgency pill.

**2:00 — Multi-tenancy (30s).** Open the dashboard, then `/numbers` and
`/settings/answering`. Point at the Mode A / Mode B toggle, then at the fact that
the account SID and number list came from the customer's own Twilio. Create a
second account in a private window and show it sees none of the first one's calls.

**2:30 — Honest close (30s).** State the one external dependency plainly: Twilio
Connect needs an upgraded Twilio account, and without it the dashboard says so
and everything else still works. Do not oversell this. Judges trust a founder who
names the gap.

## Talking points

**Business value.** Missed calls are the whole problem and they are measurable.
A business pays per seat today for a receptionist who is idle for most of the
hour. CallDesk is a fixed monthly cost against revenue that would otherwise be
lost outright, and the dashboard turns "you missed something" into "here is the
job, here is the number, call them back". The call-back button is the part that
converts: an owner who sees the transcript already knows the job is worth taking.

**Originality.** Bring-your-own-account is the differentiator. Most products in
this space ask a small business to hand over a phone number, and then the
platform owns the relationship, bills the calls, and holds the customer hostage.
Twilio Connect inverts it: the customer keeps their account, their number, their
billing relationship, and their number portability. The vendor never holds
telecom credentials at all, so there is nothing to lose in a breach. Combined
with per-account call isolation, that is a genuinely different shape from
"sign up and we'll give you a number".

**Technical depth.** A realtime voice model is the hard part, and the interesting
engineering is what surrounds it. Turn-taking has to feel natural, so the agent
must be interrupted mid-sentence. The 30-second tool timeout came out of
measuring, not guessing. There is no build step and no web framework: one SQLite
file, `ThreadingHTTPServer`, and `str.replace` templating. The routing itself is
real telephony — a TwiML Bin in the customer's own account that dials their mobile
for 20 seconds and then redirects into the agent's SIP endpoint, which means the
fallback logic lives in their Twilio account and survives our downtime. Sessions
live in SQLite so a restart does not log anyone out. The interesting constraint
was building all of it under a standard-library-only rule until the OAuth and SDK
dependencies were explicitly allowed.

**The one to be honest about.** Twilio Connect App creation requires an upgraded
Twilio account, so the multi-tenant onboarding path cannot be demonstrated end to
end without one. The code is written and tested, it is not a stub, and the
product degrades honestly: the dashboard states exactly what is missing and who
has to fix it. Saying that out loud is stronger than hiding it.

## Honest note on Twilio Connect

Creating a Connect App is a paid Twilio feature. On a trial account the
admin cannot create one, so `TWILIO_CONNECT_APP_SID` stays empty and the
dashboard shows:

> Twilio Connect not configured. To enable phone integration, an admin must
> create a Twilio Connect App (requires an upgraded Twilio account) and set
> `TWILIO_CONNECT_APP_SID` in .env. The code for the full Connect flow is already
> implemented and ready to activate.

This is a deliberate, coded state, not a placeholder. Nothing else in the product
depends on it: sign-up, sign-in, Google OAuth, the voice agent, the browser UI, the
call log and the outbound call-back all work without it, and a demo that never
touches the Connect path is unaffected.

Two further honest limits, both in the README:

- `list_customer_numbers` authenticates the customer's account with the operator's
  own `TWILIO_AUTH_TOKEN`, which only works when the customer is a subaccount of
  the operator's Twilio account. An unrelated account returns
  `20003 Authentication Error`, which is rendered on the page rather than crashing.
  Supporting arbitrary accounts means storing Twilio's per-customer OAuth token,
  and `twilio_connections` currently has only `account_sid`.
- The AssemblyAI import endpoint takes a `termination_uri`, not a customer
  account SID, so binding requires a SIP trunk and `TWILIO_TRUNK_DOMAIN` to be
  configured.
