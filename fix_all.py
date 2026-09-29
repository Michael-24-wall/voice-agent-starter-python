"""Patch every CallDesk agent: tunnel URL, date, and per-vertical rules."""

import os, re, sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from lib import load_env
load_env()
import agent_manager, db

BASE = (os.environ.get("CALLDESK_TOOL_URL") or "").strip()
if not BASE:
    sys.exit("CALLDESK_TOOL_URL is not set in .env")
BASE = re.sub(r"/tool/.*$", "", BASE)
TODAY = datetime.now().strftime("%B %d, %Y")
DATE_LINE = (f"Today is {TODAY}. When a caller says a date without a year, "
             f"use the current year, or the next year if that date has already passed.")

HOTEL_RULES = (" When check_availability returns available: false or an empty room list, "
               "that is a normal answer, not an error. Tell the caller which room types "
               "ARE available, and offer alternate dates. Never say you are having "
               "trouble checking availability. If the stay is longer than 14 nights, "
               "confirm the dates back to the caller before calling the tool.")

HOSPITAL_RULES = (" You act with the judgement of a triage nurse, never a plain booking clerk. "
                  "On every call: (1) Ask what symptoms the caller has before suggesting a "
                  "department. (2) If the caller mentions chest pain, difficulty breathing, "
                  "severe bleeding, sudden weakness on one side, slurred speech, suicidal "
                  "thoughts, an overdose, or a severe allergic reaction, call the "
                  "triage_symptoms tool immediately. If it returns risk=emergency, tell the "
                  "caller to hang up and call emergency services now, and do NOT book. "
                  "(3) For non-emergency symptoms, suggest the most appropriate department, "
                  "then call check_slots. (4) When check_slots returns an empty list, tell "
                  "the caller there are no openings on that date and offer the next "
                  "available day. Never say you are having trouble accessing the schedule. "
                  "(5) Never diagnose, never prescribe, never give medical advice.")

RESTAURANT_RULES = (" When check_tables returns no available tables, offer the closest "
                    "alternative time or a smaller party size. Never say you are having "
                    "trouble checking availability.")

SERVICE_RULES = (" After collecting name, callback number, problem, and preferred time, "
                 "call send_summary and confirm the owner will call back.")

PATCHES = {
    "hotel-agent.jsonc": HOTEL_RULES,
    "hospital-agent.jsonc": HOSPITAL_RULES,
    "restaurant-agent.jsonc": RESTAURANT_RULES,
    "calldesk.jsonc": SERVICE_RULES,
}

agents_dir = ROOT / "agents"
for filename, extra in PATCHES.items():
    path = agents_dir / filename
    if not path.exists():
        print(f"skip {filename}: not found")
        continue
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"https://[A-Za-z0-9\-]+\.loca\.lt", BASE, text)
    if f"Today is {TODAY}" not in text:
        text = re.sub(r'("system_prompt"\s*:\s*")',
                      lambda m: m.group(1) + DATE_LINE + " ",
                      text, count=1)
    marker = extra.strip().split(".")[0][:40]
    if marker not in text:
        text = re.sub(r'("system_prompt"\s*:\s*")([^"]*)(")',
                      lambda m: m.group(1) + m.group(2) + extra + m.group(3),
                      text, count=1)
    path.write_text(text, encoding="utf-8")
    print(f"patched {filename}")

print()
print("Re-publishing agents...")
for bt, name in [("service","calldesk"),("hotel","hotel-agent"),
                 ("hospital","hospital-agent"),("restaurant","restaurant-agent")]:
    if not (agents_dir / f"{name}.jsonc").exists():
        print(f"  skip {bt}: file missing")
        continue
    user = db.first_user_of_type(bt)
    if not user:
        print(f"  skip {bt}: no account")
        continue
    try:
        new_id = agent_manager.publish_user_agent(user["id"])
        print(f"  {bt:11s} user={user['id']:>3} -> {new_id}")
    except Exception as err:
        print(f"  {bt:11s} FAILED: {err}")

print()
print(f"Tunnel: {BASE}")
print(f"Today:  {TODAY}")
print("Done. Test each vertical in /talk after logging in.")
