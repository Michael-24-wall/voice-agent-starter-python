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
- **Sign in with a password or Google.** A Google sign-in whose email already matches a password account links to it rather than creating a duplicate, so call history is never orphaned.

Storage is SQLite at `data/calldesk.db` (gitignored): `users`, `sessions`, `twilio_connections`, `phone_numbers`, `calls`. [auth.py](auth.py) does PBKDF2-SHA256 password hashing; session tokens live in the `sessions` table, so a restart does not log anyone out.

Customers sign in with a password or with Google, and either way they land in the same `users` row. A Google account whose email already matches a password account is linked to it instead of creating a second one, so signing up twice does not orphan call history. [google_auth.py](google_auth.py) handles the handshake.

## Google OAuth Setup

One-time setup, in the Google Cloud Console:

1. Open **APIs & Services -> Credentials**. Create a project first if you have none.
2. **Create credentials -> OAuth client ID**.
3. Choose **Web application** under Application type. The consent screen name is what customers see.
4. Under **Authorized redirect URIs**, add exactly:
   `https://your-app.onrender.com/auth/google/callback`
   The scheme, host, port and path must match character for character. `localhost` is not accepted by Google, so local sign-in needs `http://localhost:3000` added as a second URI and `CALLDESK_BASE_URL` left unset locally.
5. Copy the **Client ID** and **Client secret** into `.env`:
   ```sh
   GOOGLE_CLIENT_ID=...apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=...
   ```
6. Keep `CALLDESK_BASE_URL` set to your public origin. `google_auth.py` derives the redirect URI from it.

If either value is missing, the **Continue with Google** button says so on the page instead of failing silently. Bad state, a declined consent screen, and a failed token exchange all return to the login page with a readable reason. State tokens are single-use and expire after ten minutes.

## Twilio Connect Setup

One-time setup, in the Twilio Console:

1. Go to **Console -> Settings -> Connect applications** and create a Connect app.
2. Set its **Authorize URL** to `{CALLDESK_BASE_URL}/connect/twilio/callback`.
3. Enable the **"Charge account for usage"** permission so the customer is billed for the numbers they use.
4. Copy the Connect App SID into `.env` as `TWILIO_CONNECT_APP_SID`.

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

## Run locally

```sh
cp .env.example .env      # ASSEMBLYAI_API_KEY, plus CALLDESK_BASE_URL to sign in with Google
AGENT=calldesk python publish.py
AGENT=calldesk python deployment/browser/server.py
```

- <http://localhost:3000> &mdash; the site
- <http://localhost:3000/talk> &mdash; the voice agent, browser microphone

There is no demo account and no seeded data. The first account to sign up owns
the database, and every later signup gets its own isolated rows. Create one at
<http://localhost:3000/signup> with an email and password, or with Google if
`GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` are set.

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

No `pip install` is needed to run any of this. `google_auth.py` verifies Google
sign-ins through Google's own token and userinfo endpoints rather than pulling
in `google-auth`, and Twilio REST calls go through `lib.py` rather than the
`twilio` SDK.

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
