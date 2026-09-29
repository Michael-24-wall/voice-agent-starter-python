"""Publish one AssemblyAI agent per CallDesk account.

Each business gets an agent of its own, built from the base config in
`agents/<name>.jsonc` that matches its `business_type`. Publishing happens once,
from the setup page, and the resulting id is stored on the user row, so two
hotels never share one agent and its prompt.

The alternative, letting every call share the single agent named by AGENT, was
the behaviour before multi-vertical support: one prompt, one greeting, one tool
set, for every account on the account. That cannot work once a hotel and a
clinic are on the same deployment, because they need different tools and the
greeting has to name the business that is actually calling.
"""

import os
import re
import sys
import urllib.parse
from typing import Any, Optional

import db
import lib


class AgentError(Exception):
    """Something the setup page should show the owner, not a traceback."""


# The vertical decides the base config. Service businesses keep the original
# agent so an existing account carries on working untouched.
AGENT_FOR_TYPE = {
    "service": "calldesk",
    "hotel": "hotel-agent",
    "hospital": "hospital-agent",
    "restaurant": "restaurant-agent",
}

PLACEHOLDER = re.compile(r"\{business_name\}")


def agent_name_for(user: dict) -> str:
    return AGENT_FOR_TYPE.get(db.normalize_business_type(user.get("business_type")),
                              "calldesk")


def _ensure_tool_base() -> None:
    """Make both tool URL variables available before the config is read.

    `lib.read_agent` exits the process on a missing ${VAR}, which is right for
    publish.py but would take the whole server down from a setup page, so the
    variables are resolved and checked here instead.

    CALLDESK_TOOL_BASE is the /tool prefix the vertical agents append an
    endpoint to. A single-variable setup only has CALLDESK_TOOL_URL, the full
    URL of send_summary, so the prefix is derived from it by dropping the last
    path segment.
    """
    base = (os.environ.get("CALLDESK_TOOL_BASE") or "").strip().rstrip("/")
    url = (os.environ.get("CALLDESK_TOOL_URL") or "").strip()

    if not base and url:
        base = url.rsplit("/", 1)[0] if "/" in url else ""
    if base and not url:
        # calldesk.jsonc uses the full send_summary URL, not the prefix.
        url = f"{base}/send_summary"
    if not base:
        raise AgentError(
            "No tool URL is configured. Set CALLDESK_TOOL_BASE in .env to the public "
            "https address of this deployment's /tool path, for example "
            "https://your-app.onrender.com/tool, then activate again.")

    os.environ["CALLDESK_TOOL_BASE"] = base
    os.environ["CALLDESK_TOOL_URL"] = url


def _fill(value: Any, business_name: str) -> Any:
    """Replace {business_name} everywhere it appears in the config."""
    if isinstance(value, str):
        return PLACEHOLDER.sub(business_name.replace("{", "(").replace("}", ")"),
                               value)
    if isinstance(value, list):
        return [_fill(item, business_name) for item in value]
    if isinstance(value, dict):
        return {key: _fill(item, business_name) for key, item in value.items()}
    return value


def _stamp_account(body: dict, user_id: int) -> dict:
    """Put the owning account on every tool URL.

    A tool call arrives from AssemblyAI's servers with no cookie and no session,
    so the only way the endpoint knows which account is asking is what this
    writes into the URL it was published with. Without it the endpoint has to
    guess from the called number, and a call that carries no number falls back
    to the oldest account of that vertical, which is how one hotel ends up
    reading another hotel's rooms.
    """
    for tool in body.get("tools", []):
        url = (tool.get("http") or {}).get("url")
        if not url or not url.startswith(("http://", "https://")):
            continue
        parts = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        # Replace rather than append, so a re-publish cannot stack two of these.
        query = [(k, v) for k, v in query if k != "account"]
        query.append(("account", str(int(user_id))))
        tool["http"]["url"] = urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path,
             urllib.parse.urlencode(query), parts.fragment))
    return body


def build_user_agent(user: dict) -> dict:
    """The request body for POST /v1/agents, for one account."""
    _ensure_tool_base()
    config = lib.read_agent(agent_name_for(user))
    name = (user.get("business_name") or "").strip() or "your business"
    body = _fill(config, name)
    # A duplicated name in the dashboard is how you find your agent again.
    body["name"] = name
    if not body.get("tools"):
        raise AgentError(f"the {agent_name_for(user)} config has no tools")
    return _stamp_account(body, user["id"])


def publish_user_agent(user_id: int) -> str:
    """Create or update this account's agent. Returns its id.

    Updates in place when the account already has one, because re-publishing
    from the setup page is a normal thing to do and a create every time would
    strand the previous agent and its prompt in the dashboard.

    The id lives on the user row rather than in .env: a hosted deployment has
    no writable .env, and every account needs its own agent anyway.
    """
    user = db.get_user_by_id(int(user_id))
    if not user:
        raise AgentError("that account no longer exists")

    body = build_user_agent(user)
    existing = (user.get("agent_id") or "").strip()
    try:
        if existing:
            agent = lib.aai(f"/agents/{existing}", method="PUT", body=body)
            verb = "updated"
        else:
            agent = lib.aai("/agents", method="POST", body=body)
            verb = "published"
    except lib.ApiError as err:
        # The API answers with the reason, which is usually a missing key or a
        # tool URL the API cannot reach.
        raise AgentError(f"AssemblyAI rejected the agent: {err}") from None

    agent_id = (agent.get("id") or "").strip()
    if not agent_id:
        raise AgentError("AssemblyAI returned an agent without an id")
    db.set_user_agent(user["id"], agent_id)
    print(f'agent: {verb} "{body["name"]}" as {agent_id}', flush=True)
    return agent_id


def get_user_agent(user_id: int) -> Optional[str]:
    """The agent id stored for this account, or None before they activate."""
    return db.get_user_agent(int(user_id))


if __name__ == "__main__":
    lib.load_env()
    db.init_db()
    if len(sys.argv) < 2:
        sys.exit("usage: python agent_manager.py <user_id> [user_id ...]")
    for arg in sys.argv[1:]:
        print(publish_user_agent(arg))
