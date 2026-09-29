"""Patch hotel and hospital agent prompts with clearer failure handling.

Adds two instructions to each vertical's system prompt:
1. When a tool returns available: false or an empty list, say that plainly
   to the caller instead of apologising for a technical problem.
2. If a hotel stay is longer than 14 nights, confirm the dates back.
"""

import os, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from lib import load_env
load_env()
import agent_manager, db

TODAY = __import__("datetime").datetime.now().strftime("%B %d, %Y")

HOTEL_RULES = (
    " When the check_availability tool returns available: false or an empty "
    "room list, that is a normal answer, not a technical problem. Tell the "
    "caller which room types ARE available and offer alternate dates. Never "
    "say you are having trouble checking availability. If the requested stay "
    "is longer than 14 nights, repeat the dates back: 'Just to confirm, that "
    "is from [check_in] to [check_out], correct?' before calling the tool."
)

HOSPITAL_RULES = (
    " When the check_slots tool returns an empty slots list, that is a normal "
    "answer. Tell the caller there are no openings on that date and offer the "
    "next available day. Never say you are having trouble accessing the schedule."
)

PATCHES = {
    "hotel-agent.jsonc": HOTEL_RULES,
    "hospital-agent.jsonc": HOSPITAL_RULES,
}

agents_dir = ROOT / "agents"

for filename, extra in PATCHES.items():
    path = agents_dir / filename
    if not path.exists():
        print(f"skip {filename}: not found")
        continue
    text = path.read_text(encoding="utf-8")
    # Only add once.
    marker = extra.strip().split(".")[0][:40]
    if marker in text:
        print(f"skip {filename}: rules already present")
        continue
    # Insert the rule right before the closing quote of system_prompt.
    def inject(match):
        existing = match.group(2)
        return f'{match.group(1)}{existing}{extra}"'
    new_text, n = re.subn(
        r'("system_prompt"\s*:\s*")([^"]*)(")',
        lambda m: m.group(1) + m.group(2) + extra + m.group(3),
        text, count=1)
    if n == 0:
        print(f"skip {filename}: system_prompt not found")
        continue
    path.write_text(new_text, encoding="utf-8")
    print(f"patched {filename}")

# Re-publish every agent with an account.
for bt, name in [("service","calldesk"),("hotel","hotel-agent"),
                 ("hospital","hospital-agent"),("restaurant","restaurant-agent")]:
    if not (agents_dir / f"{name}.jsonc").exists():
        continue
    user = db.first_user_of_type(bt)
    if not user:
        continue
    try:
        aid = agent_manager.publish_user_agent(user["id"])
        print(f"  {bt:11s} user={user['id']:>3} -> {aid}")
    except Exception as err:
        print(f"  {bt:11s} FAILED: {err}")
print()
print("Done. Test in /talk with a 2-day stay first:")
print('  "Do you have a deluxe room from October 15th to October 17th, 2026?"')
