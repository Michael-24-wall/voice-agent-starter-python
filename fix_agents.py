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
             f"use the current year, or the next year if that date has already passed. "
             f"Always confirm the year back to the caller if there is any chance of confusion.")

agents_dir = ROOT / "agents"
for path in sorted(agents_dir.glob("*.jsonc")):
    text = path.read_text(encoding="utf-8")
    if f"Today is {TODAY}" not in text:
        text = re.sub(r'("system_prompt"\s*:\s*")',
                      lambda m: m.group(1) + DATE_LINE + " ",
                      text, count=1)
    text = re.sub(r"https://[A-Za-z0-9\-]+\.loca\.lt", BASE, text)
    path.write_text(text, encoding="utf-8")
    print(f"Patched {path.name}")

for bt, name in [("service","calldesk"),("hotel","hotel-agent"),
                 ("hospital","hospital-agent"),("restaurant","restaurant-agent")]:
    if not (agents_dir / f"{name}.jsonc").exists():
        print(f"  skip {bt}: agents/{name}.jsonc not found")
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
print('Test in /talk: "Do you have a deluxe room available from October 11th to October 12th, 2026?"')
