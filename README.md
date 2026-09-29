<img src="assemblyai.png" width="500"/>

---

[![Voice Agent API](https://img.shields.io/badge/docs-Voice%20Agent%20API-2545E6)](https://www.assemblyai.com/docs/voice-agents/voice-agent-api)
[![Python](https://img.shields.io/badge/python-%E2%89%A53.9-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)](requirements.txt)
[![AssemblyAI Twitter](https://img.shields.io/twitter/follow/AssemblyAI?label=%40AssemblyAI&style=social)](https://twitter.com/AssemblyAI)
[![AssemblyAI YouTube](https://img.shields.io/youtube/channel/subscribers/UCtatfZMf-8EkIwASXM4ts0A)](https://www.youtube.com/@AssemblyAI)

# AssemblyAI Voice Agent Starter for Python

Voice agents defined as JSON files. Publish one to your AssemblyAI account, then talk to it in a browser tab or by calling a phone number.

Each file in [agents/](agents/) is the request body for `POST /v1/agents`. The starter sends it unchanged, saves the agent ID it gets back to `.env`, and both deployments connect using that ID. An agent you already have goes the other way, `python import_agent.py <agent-id>` turns it into one of these files. Built on the [AssemblyAI Voice Agent API](https://www.assemblyai.com/products/voice-agent-api). Python 3.9 or later, standard library only, so there is nothing to pip install.

There is a [JS version of this repo](https://github.com/AssemblyAI/voice-agent-starter-js) with the same agents and the same steps.

---

# CallDesk

CallDesk is a voice AI receptionist for local service businesses. It answers calls when you can't, collects caller details, and notifies you.

It is the AssemblyAI starter below plus a multi-tenant SaaS layer: businesses sign up, connect their own Twilio account through Twilio Connect, bind their own numbers, and see their own call logs. Python 3.9 or later, standard library only, so there is nothing to pip install.

## How it works

```
Caller  ->  Twilio  ->  AssemblyAI Voice Agent  ->  send_summary tool
                                                       |
                             owner notification  <-  CallDesk server
```

1. A call comes in on a number the business owns.
2. Twilio routes it to the AssemblyAI Voice Agent. Depending on the answering mode, it either goes straight in, or rings the owner's mobile first and falls back after 20 seconds.
3. The agent talks to the caller, then calls the `send_summary` tool with their name, callback number, problem, and urgency.
4. `send_summary` is an HTTP tool, so AssemblyAI itself makes the request to `POST /tool/send_summary`. That is why it works on a phone call, where there is no browser.
5. CallDesk files the call against the owning account and shows it on that account's dashboard.

The agent's behaviour is entirely in [agents/calldesk.jsonc](agents/calldesk.jsonc). Runtime is in [deployment/browser/server.py](deployment/browser/server.py). Twilio and AssemblyAI calls are in [twilio_connect.py](twilio_connect.py). Shared plumbing is in [lib.py](lib.py).

## Multi-tenant SaaS

- **Users sign up and connect their own Twilio account** through [Twilio Connect](https://www.twilio.com/docs/connect), so CallDesk never stores their Twilio password.
- **Each user binds their own numbers.** `POST /v1/phone-numbers/import` registers a number with AssemblyAI, then `PUT /v1/phone-numbers/{number}/agent` attaches the agent to it.
- **Each user sees only their own calls.** Every `send_summary` call is filed against the account that owns the receiving number, and the dashboard and call log are scoped to that account.
- **Twilio bills the customer directly** for their own number and for call usage. CallDesk is an application on top of it and never touches telecom billing.

Storage is SQLite at `data/calldesk.db` (gitignored): `users`, `sessions`, `twilio_connections`, `phone_numbers`, `calls`, plus the vertical tables below. [auth.py](auth.py) does PBKDF2-SHA256 password hashing; session tokens live in the `sessions` table, so a restart does not log anyone out.

## For Business Owners

CallDesk uses Twilio to receive phone calls. Here's how to connect your number:

1. Create a free Twilio account at [twilio.com/try-twilio](https://www.twilio.com/try-twilio)
2. Buy a phone number in the [Twilio Console](https://console.twilio.com)
3. Click "Connect Twilio" in your CallDesk dashboard
4. Optionally, set up call forwarding from your existing business number
5. Test by calling your new number

Full step-by-step guide with screenshots and troubleshooting: see `/guide` in the app.

### Costs

- Twilio phone number: ~$1/month
- Twilio inbound calls: ~$0.0085/minute
- CallDesk: your subscription

Twilio bills you directly. CallDesk never touches your phone bill.

### Trial Account Limitation

Twilio trial accounts can only receive calls from verified phone numbers. To let your customers reach you, upgrade your Twilio account. The upgrade takes about 2 minutes and requires a credit card.

## Vertical support

An account picks its kind of business at signup, and that choice decides three things: which agent it gets, which setup page it sees, and which tools the agent can call. `users.business_type` is one of `hotel`, `hospital`, `restaurant`, `service`, and `users.agent_id` holds the agent published for that account alone.

| Business | Agent | Setup page | Tools |
| --- | --- | --- | --- |
| Hotel | [agents/hotel-agent.jsonc](agents/hotel-agent.jsonc) | `/setup/hotel/rooms` | `check_availability`, `book_room` |
| Hospital | [agents/hospital-agent.jsonc](agents/hospital-agent.jsonc) | `/setup/hospital/departments`, `/setup/hospital/slots` | `check_slots`, `book_appointment` |
| Restaurant | [agents/restaurant-agent.jsonc](agents/restaurant-agent.jsonc) | `/setup/restaurant/tables` | `check_tables`, `book_table` |
| Service | [agents/calldesk.jsonc](agents/calldesk.jsonc) | `/setup` | `send_summary` |

Availability is answered from the database, not from the prompt. An agent never says a room is free unless a row says it is, and every booking is written before the agent confirms it, so two callers cannot take the same room:

```
Caller  ->  Twilio  ->  their own AssemblyAI agent  ->  check_availability
                          |                               |
                          |                    CallDesk server, a real query
                          |                               |
                          +--------- book_room  <---------+
                                     |
                        hotel_bookings row, then a spoken confirmation
```

- **Hotel** keeps `hotel_rooms` and `hotel_bookings`. Availability is a date-range overlap, half-open, so a guest leaving on the 3rd can check into the room freed on the 3rd. Occupancy comes from the bookings, not from the room's status, because a room occupied for one night is still free the next.
- **Hospital** keeps `hospital_departments`, `hospital_slots`, and `hospital_appointments`. Booking is a single conditional `UPDATE ... WHERE status = 'available'`, so the row is only taken if it was still free when that statement ran. An unknown department comes back as an error the agent can read out rather than as "no appointments".
- **Restaurant** keeps `restaurant_tables` and `restaurant_reservations`. A reservation holds its table for `RESERVATION_HOLD_MINUTES` (120), so 7pm and 8pm on the same table are a conflict even though the timestamps differ.

Every tool answers the same envelope, always with HTTP 200, because a 4xx reads to the agent as a dead endpoint rather than as a reason:

```json
{"ok": true,  "data": {"available": true, "room_count": 2, "rooms": [...]}}
{"ok": false, "error": "room 301 is already booked for those dates"}
```

`POST /setup/publish-agent` publishes the agent for the signed-in account. It reads the base config for that `business_type`, fills in `{business_name}`, and `POST`s it to `/v1/agents`, then stores the returned id on the user row. Each business gets an agent of its own rather than sharing the single agent named by `AGENT`, because a hotel and a clinic need different tools and a greeting that names the business actually calling.

Set `CALLDESK_TOOL_BASE` in `.env` to the public https address of the `/tool` path, for example `https://your-app.onrender.com/tool`. The vertical agents append an endpoint to it. If only `CALLDESK_TOOL_URL` is set, the prefix is derived from it by dropping the last path segment.

To try it with data, run `python seed_business.py` after `python -c "import db; db.init_db()"`. It creates one hotel, one clinic, and one restaurant with the inventory their agents need, and prints the logins. It is safe to run twice: an account that exists is left alone.

### How to add a new vertical

1. **A table per thing you sell.** Add it to `SCHEMA` in [db.py](db.py) with a `user_id` column and a foreign key to `users(id)`, so it is scoped to one tenant. `CREATE TABLE IF NOT EXISTS` in the schema block runs for a new install; add an `ALTER` in `_migrate` for anything that changes an existing table.
2. **A name in `BUSINESS_TYPES`.** Add it to `BUSINESS_TYPES` and `BUSINESS_TYPE_LABELS` in [db.py](db.py), and a `<select>` option in [templates/signup.html](templates/signup.html).
3. **A setup template.** Add `templates/setup_<vertical>.html` and register it in `SETUP_TEMPLATE` in [deployment/browser/server.py](deployment/browser/server.py). Add the page to `GET_ONLY`, and the read and delete routes to the two route tables in `do_GET` and `do_POST`.
4. **A base agent config.** Add `agents/<vertical>-agent.jsonc` as a `POST /v1/agents` body, with one `http` tool per action pointing at `${CALLDESK_TOOL_BASE}/<endpoint>`. A tool with no `http` block is client-executed and cannot answer a phone call. Use `{business_name}` for the account's own name and a voice id from the [voice catalog](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/voices). Add a tool to the agent's system prompt telling it what to do when the tool returns `ok false`, or it will read the error as if it were a result.
5. **Map it to the config.** Add the vertical to `AGENT_FOR_TYPE` in [agent_manager.py](agent_manager.py) so publishing picks the right file.
6. **A tool handler.** Add the endpoint to the `vertical_tools` table in `do_POST` and write a handler that calls the db and returns `{"ok": true, "data": ...}`. Let `db` raise `ValueError` with a message written to be spoken: the shared `_tool_run` turns it into `{"ok": false, "error": ...}` and logs it.
7. **Dashboard stats.** Add a branch to `vertical_summary` so the vertical's counts and recent rows appear on the dashboard.
8. **Document it.** Add a row to the table above.

### Tool calls and the receiving number

A tool call arrives carrying only the arguments the model filled in, so the number that received the call is usually absent. The handler attributes it in this order:

1. If the body carries `to`, look the number up in `phone_numbers` and use that account. A number bound to a different kind of business is refused rather than served the wrong vertical's data.
2. Otherwise the oldest account of that vertical answers, and the fallback is logged every time.

Step 2 is what keeps a single-tenant demo working, and it is wrong the moment two hotels are on the account. For a real deployment, put the account in the tool URL — `https://your-app.onrender.com/tool/h/<user_id>/check_availability` — or sign it, so attribution comes from the request rather than from a guess.

Users sign up with an email and password or with Google. A Google sign-in whose email already matches a password account is linked to it rather than creating a duplicate, so signing up twice never orphans call history. [auth.py](auth.py) hashes with PBKDF2-SHA256 and a per-password random salt, compares in constant time, and stores session tokens in the `sessions` table so a restart does not log anyone out. The session cookie is `calldesk_session`, `HttpOnly`, `SameSite=Lax`, `Path=/`. [google_auth.py](google_auth.py) runs the Google handshake with `google-auth` and `google-auth-oauthlib`.

## Google OAuth Setup

One-time setup, in the Google Cloud Console:

1. Open **APIs & Services -> Credentials**. Create a project first if you have none.
2. **Create credentials -> OAuth client ID**.
3. Choose **Web application** under Application type. The consent screen name is what customers see.
4. Under **Authorized redirect URIs**, add exactly:
   `https://your-vultr-host/auth/google/callback`
   The scheme, host, port and path must match character for character, or Google rejects the exchange. `localhost` is not accepted, so local sign-in needs `http://localhost:3000/auth/google/callback` added as a second URI.
5. Copy the **Client ID** and **Client secret** into `.env`:
   ```sh
   GOOGLE_CLIENT_ID=...apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=...
   ```
6. Keep `CALLDESK_BASE_URL` set to your public origin. `google_auth.py` builds the redirect URI from it, and it has to match what you registered.

If either value is missing, the **Sign in with Google** button says so on the page instead of failing silently. An invalid or expired state token, a declined consent screen, and a failed token exchange all return to the login page with a readable reason. State tokens are single-use and expire after ten minutes.

## Twilio Connect Setup

**This step requires an upgraded Twilio account.** Creating a Connect App is a
paid Twilio feature, so it is the one thing in this project that cannot be done
on a trial account. The code for the whole flow is written and tested, so it
activates the moment an admin sets the SID. Until then the dashboard says so
explicitly and every other part of CallDesk still works.

One-time setup, in the Twilio Console:

1. Go to **Console -> Settings -> Connect applications** and create a Connect app.
2. Set its **Authorize URL** to `{CALLDESK_BASE_URL}/connect/twilio/callback`.
3. Enable the **"Charge account for usage"** permission so the customer is billed for the numbers they use.
4. Copy the Connect App SID into `.env` as `TWILIO_CONNECT_APP_SID`.

### The customer flow, end to end

1. The owner signs up, then clicks **Connect Twilio** on the dashboard.
2. CallDesk redirects to `https://connect.twilio.com/authorize` with the Connect App SID and their user id as state.
3. The owner signs up for Twilio if they do not have an account, or logs in to an existing one, and authorizes CallDesk.
4. Twilio redirects back to `/connect/twilio/callback?AccountSid=AC...&state=<user_id>`. CallDesk saves the account sid against that user.
5. The owner buys a number in the Twilio console, or already has one.
6. Back in CallDesk, `/numbers` lists their real Twilio numbers. They pick one and **Bind**.
7. Binding imports the number to AssemblyAI with `POST /v1/phone-numbers/import`, then attaches the CallDesk agent with `PUT /v1/phone-numbers/{number}/agent`.
8. They choose an answering mode under **Numbers -> Configure**. The call now reaches the agent.

Also in `.env`, for this instance:

```sh
TWILIO_ACCOUNT_SID=AC...            # yours, from the console dashboard
TWILIO_AUTH_TOKEN=...              # yours, reads the customer's number list (see the note below)
CALLDESK_BASE_URL=https://your-app.onrender.com
AGENT_ID_CALLDESK=agent_...        # written by publish.py
TWILIO_TRUNK_DOMAIN=...            # the SIP trunk, gates /numbers/bind
```

`CALLDESK_BASE_URL` must be reachable from Twilio. `localhost` will not do.

**Known limitation.** Reading a customer's number list uses your `TWILIO_AUTH_TOKEN` alongside their `AccountSid`. That authenticates successfully only when the customer is a subaccount of yours. A genuinely unrelated Twilio account rejects it with `20003 Authentication Error`, and that error is shown on the page. Supporting arbitrary accounts means exchanging Twilio Connect's authorization code for an OAuth token and storing it per user; `twilio_connections` currently holds only `account_sid`, so that column is the place it would go.

## Answering Modes

Per number, under **Numbers -> Configure**.

**Mode A &mdash; CallDesk answers directly.** The number's voice URL points at `/twiml/fallback`, which returns `<Response><Redirect>sip:sip.assemblyai.com</Redirect></Response>`. The call goes into the agent with nobody's phone ringing.

**Mode B &mdash; Ring my mobile first, CallDesk if I don't answer.** The number's voice URL points at a TwiML Bin in the customer's Twilio account:

```xml
<Response>
  <Dial timeout="20"><Number>YOUR_MOBILE</Number></Dial>
  <Redirect method="GET">{CALLDESK_BASE_URL}/twiml/fallback?number=+1555...</Redirect>
</Response>
```

So CallDesk is the fallback: 20 seconds for you, then the agent. The bin is created and updated in place, so saving repeatedly does not litter the account.

**Call Back** on the calls page places an outbound call from the bound number. Its URL is `/twiml/outbound`, which also redirects into the agent, so you hear CallDesk rather than a ringing phone.

## The onboarding guide

`GET /guide` is the customer-facing version of all of the above, written for a business owner rather than an integrator: create a Twilio account, buy a number, connect it, optionally forward an existing number, then test. It needs no login, so it is readable before signup, and it is linked from the top nav, the footer, the landing page, `/numbers`, and the dashboard.

The dashboard adapts to the account's actual state rather than to a flag. With no Twilio connection it shows the three-step onboarding card; once `twilio_connections` has a row it collapses to a one-line confirmation. Below that, `setup_checklist` in `server.py` derives five items straight from the database — account, connection, a row in `phone_numbers`, a non-null `answering_mode`, and a row in `calls` — so the widget cannot drift from the state it describes, and it disappears once all five are true.

## Run locally

```sh
cp .env.example .env      # ASSEMBLYAI_API_KEY, plus CALLDESK_BASE_URL for Twilio
AGENT=calldesk python publish.py
AGENT=calldesk python deployment/browser/server.py
```

- <http://localhost:3000> &mdash; the site
- <http://localhost:3000/talk> &mdash; the voice agent, browser microphone

There is no demo account and no seeded data. The first account to sign up owns
the database, and every later signup gets its own isolated rows. Create one at
<http://localhost:3000/signup> with your business name, email, and a password of at
least 8 characters. Mobile number is optional and only used by Mode B.

`send_summary` is an HTTP tool, so its URL must be a public https address AssemblyAI can reach. Put a tunnel URL in `CALLDESK_TOOL_URL` in `.env` and re-run `publish.py` whenever it changes.

## Tech stack

| | |
|---|---|
| Runtime | Python 3.9+, standard library only, no dependencies |
| Database | SQLite via `sqlite3` |
| Voice | [AssemblyAI Voice Agent API](https://www.assemblyai.com/products/voice-agent-api) |
| LLM | AssemblyAI's own model by default; LLM Gateway is opt-in through an `llm` block in an agent file |
| Telephony | [Twilio](https://www.twilio.com) SIP trunks, TwiML Bin, and [Twilio Connect](https://www.twilio.com/docs/connect) |
| Sign-in | Email and password, plus Google OAuth 2.0 |
| Frontend | Server-rendered HTML from `templates/`, no build step |

### Requirements

```sh
pip install google-auth google-auth-oauthlib requests twilio
```

The database is `sqlite3` rather than an ORM. Twilio REST calls go through the
`twilio` SDK. Google sign-in uses `google-auth` to verify the ID token's
signature against Google's public keys.

---

## Deployment

Vultr is a good fit: a plain VPS, no build step, and no external database to
operate.

1. Create a VPS and point an A record at it, so `call.yourdomain.com` resolves.
2. `apt install python3` and clone the repo, then copy `.env.example` to `.env` and fill it in. At minimum: `ASSEMBLYAI_API_KEY`, `CALLDESK_BASE_URL=https://call.yourdomain.com`, `AGENT_ID_CALLDESK`, and the Twilio values above.
3. Keep the process alive across reboots and restarts. A systemd unit is the least surprising option:
   ```ini
   # /etc/systemd/system/calldesk.service
   [Unit]
   Description=CallDesk
   After=network.target

   [Service]
   WorkingDirectory=/opt/calldesk
   Environment=AGENT=calldesk
   ExecStart=/usr/bin/python3 deployment/browser/server.py
   Restart=always

   [Install]
   WantedBy=multi-user.target
   ```
   ```sh
   systemctl enable --now calldesk
   ```
4. Terminate TLS in front of the app with nginx or Caddy. Google refuses any redirect URI that is not `https`, so the public origin must be https. The app itself is plain HTTP on port 3000.
5. Re-run `AGENT=calldesk python publish.py` if `CALLDESK_TOOL_URL` changes, since the `send_summary` tool URL is baked into the published agent.

---

## Quickstart

### 1. Clone

```sh
git clone https://github.com/AssemblyAI/voice-agent-starter-python
cd voice-agent-starter-python
cp .env.example .env
```

### 2. Add your key

From [assemblyai.com/dashboard/api-keys](https://www.assemblyai.com/dashboard/api-keys):

```sh
# .env
ASSEMBLYAI_API_KEY=your_key_here
```

### 3. Get an agent

Publish one of the examples:

```sh
python publish.py                       # agents/minimal.jsonc
# AGENT=http-tools python publish.py    # or any other file in agents/
```

Or import one you already have, shaped in the playground or the dashboard:

```sh
python import_agent.py <agent-id>          # writes agents/<its-name>.jsonc
```

Either way you end up with the same pair: a file in `agents/` and its id in `.env` as `AGENT_ID_<NAME>`. Publishing again updates that agent rather than creating another, and each file keeps its own, so switching with `AGENT=` never overwrites the last one.

### 4. Talk to it

```sh
python deployment/browser/server.py
```

Open http://localhost:3000 and start the call.

### 5. Put it on a phone number

```sh
# .env
TWILIO_ACCOUNT_SID=AC...                          # console.twilio.com, top of the page
TWILIO_AUTH_TOKEN=your_token_here                 # same place, hidden until you click it
TWILIO_PHONE_NUMBER=+15551234567                  # a number already in your account, E.164
TWILIO_TRUNK_DOMAIN=acme-agent.pstn.twilio.com    # a name you invent, must end .pstn.twilio.com
```

The trunk domain does not exist yet. You are naming the SIP trunk that gets created for you, and the name has to be unique across all of Twilio, so put something specific to you in front of `.pstn.twilio.com`. The phone number does have to exist already: buy one under Phone Numbers in the Twilio console first.

```sh
python deployment/telephony/connect.py
```

This creates the trunk, routes it to AssemblyAI, attaches your number to it, and binds the agent. Then call the number. Details in [deployment/telephony](deployment/telephony/).

---

## Core examples

Nine agent files. Four demonstrate a parameter, five demonstrate an integration.

| `AGENT=` | Demonstrates | Requires |
| --- | --- | --- |
| [`minimal`](agents/minimal.jsonc) | the three required fields, and the defaults applied to the rest | |
| [`keyterms`](agents/keyterms.jsonc) | biasing transcription toward names and jargon | |
| [`turn-taking`](agents/turn-taking.jsonc) | silence thresholds and interruption handling | |
| [`byo-llm`](agents/byo-llm.jsonc) | Claude through the AssemblyAI gateway, or your own endpoint | |
| [`http-tools`](agents/http-tools.jsonc) | tools that AssemblyAI calls on the agent's behalf | |
| [`exa-search`](agents/exa-search.jsonc) | web search during a call | `EXA_API_KEY` |
| [`airtable-crm`](agents/airtable-crm.jsonc) | reading a caller record and writing one back | `AIRTABLE_*` |
| [`cal-booking`](agents/cal-booking.jsonc) | checking availability, then booking a slot | `CAL_*` |
| [`dtmf`](agents/dtmf.jsonc) | PCI compliance: card entry on the keypad, never in the transcript, the logs or the model | `DTMF_WEBHOOK_URL` |

```sh
AGENT=exa-search python publish.py
python deployment/browser/server.py
```

To write your own, copy the closest file: `cp agents/http-tools.jsonc agents/my-agent.jsonc`. Every field is commented, with a link to the documentation page that defines it.

## Importing an agent

The playground is the quickest way to shape an agent. This is how it moves into code without being rebuilt by hand:

```sh
python import_agent.py 8f3c1e2a-...
```

It writes `agents/<name>.jsonc`, the live agent as a file, headed with the id it came from. It records `AGENT_ID_<NAME>` in `.env`, so `python publish.py` sends a `PUT` to that same agent instead of creating a second one. It drops `id`, `created_at` and `updated_at`, which are not part of a create request. And it refuses to overwrite an existing file unless you pass `AGENT=<other-name>` or `OVERWRITE=1`.

Credentials are the one thing it cannot recover. Tool header values and `llm[].api_key` are write-only on the API, so they come back blank. The import names the ones to restore, and they belong in `.env`, referenced from the file as `${VARS}`:

```
Header values are write-only and did not come back for: lookup.
Put them in .env and reference them as ${VARS}.
```

From there it behaves like any other file in `agents/`: edit it, publish, call.

## Where it answers

| | | |
| --- | --- | --- |
| [Browser](deployment/browser/) | `python deployment/browser/server.py` | Serves a page with a call button and mints session tokens. The API key stays on the server. |
| [Phone](deployment/telephony/) | `python deployment/telephony/connect.py` | Configures a Twilio SIP trunk and attaches the agent to your number. |

Twilio passes the call to AssemblyAI over SIP, so nothing in this repo sits in the audio path.

## Hosting the browser app

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/AssemblyAI/voice-agent-starter-python)

Render reads [render.yaml](render.yaml) and prompts for exactly one value, `ASSEMBLYAI_API_KEY`, because that is the only variable marked `sync: false`. It sets `PORT` itself. The other two arrive with defaults you can change under Environment on the service:

| Variable | Default | What it does |
| --- | --- | --- |
| `ASSEMBLYAI_API_KEY` | prompted | Stays on the server. Never sent to the page. |
| `AGENT` | `minimal` | Which `agents/<name>.jsonc` the service publishes when it boots. |
| `AGENT_ID` | empty | Paste an id from your `.env` to serve that exact agent, whichever file it came from. |

Leaving `AGENT_ID` empty is fine. The service publishes `AGENT` on boot, and on later restarts it updates the agent of that name rather than creating another one.

## How it works

```
  copy an example                     python import_agent.py <id>
  or write your own                   an agent you already have
           │                                   │
           ▼                                   ▼
agents/exa-search.jsonc     body of POST /v1/agents
        + .env              the ${VARS} it references
           │
           ▼  python publish.py
      AGENT_ID_EXA_SEARCH
           ├──  browser/server.py      browser tab
           └──  telephony/connect.py   phone number
```

The first publish sends `POST /v1/agents` and stores the returned ID in `.env` under a key of its own, `AGENT_ID_EXA_SEARCH` for that file. Later publishes send `PUT /v1/agents/{id}`, so the browser tab and the phone number both pick up the change on the next call, and publishing a different file leaves this one alone. A bare `AGENT_ID` overrides every per-file key.

Values written as `${VAR}` anywhere in an agent file are substituted at publish time from `.env`, or from `agents/<name>.env` for credentials only one agent uses. Both files are gitignored, so the JSON can be committed.

## Build with AI coding agents

This repo includes [AGENTS.md](AGENTS.md), which Claude Code, Cursor and Copilot read for its conventions. The Voice Agent API changes, so point coding tools at the current documentation rather than letting them work from memory:

> Always fetch https://assemblyai.com/docs/llms.txt before writing AssemblyAI code. The API has changed, do not rely on memorized parameter names.

```sh
claude mcp add --transport http --scope user assemblyai-docs https://mcp.assemblyai.com/docs
npx skills add AssemblyAI/assemblyai-skill --global
```

See [Build with AI tools](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/build-with-ai-tools) and [Coding agent prompts](https://www.assemblyai.com/docs/coding-agent-prompts).

## Voice Agent API

Product: [Voice Agent API](https://www.assemblyai.com/products/voice-agent-api) · [Pricing](https://www.assemblyai.com/pricing) · [Dashboard](https://www.assemblyai.com/dashboard)

Start here: [Documentation](https://www.assemblyai.com/docs/voice-agents/voice-agent-api) · [Create an agent](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/create-agent) · [Manage agents](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/manage-agents) · [Prompting guide](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/prompting-guide) · [Best practices](https://www.assemblyai.com/docs/voice-agents/best-practices)

Configuration: [Voices](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/voices) · [Greeting](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/greeting) · [Turn detection](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/turn-detection-and-interruptions) · [Keyterms](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/transcription-prompt) · [Languages](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/supported-languages) · [Noise suppression](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/noise-suppression) · [Custom LLM](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/connect-your-own-llm)

Tools: [Overview](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/tools/overview) · [HTTP tools](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/tools/http-tools) · [Client-side tools](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/tools/client-side-tools)

Deployment: [Deploy](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/deploy) · [Browser integration](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/browser-integration) · [Connect to Twilio](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/connect-to-twilio) · [Use your own number](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/twilio-own-number) · [Webhooks](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/webhooks)

Reference: [Session configuration](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/session-configuration) · [Events](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/events-reference) · [Message sequence](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/message-sequence) · [Session history](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/session-history) · [Troubleshooting](https://www.assemblyai.com/docs/voice-agents/voice-agent-api/troubleshooting)

## Cost

Sessions are billed to the API key that published the agent. Anyone with the deployed URL or the phone number can start a session on that key.
