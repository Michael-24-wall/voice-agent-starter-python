import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from lib import load_env
load_env()
import db
import agent_manager

url = os.environ.get("CALLDESK_TOOL_URL") or ""
if not url:
    print("CALLDESK_TOOL_URL not set")
    sys.exit(0)

base = url.rsplit("/", 1)[0] if "/" in url else url
print("Refreshing agents with base URL: " + base)

count = 0
for row in db.connect().execute("SELECT id FROM users").fetchall():
    try:
        agent_manager.publish_user_agent(row["id"])
        count += 1
    except Exception as err:
        print("  user " + str(row["id"]) + " failed: " + str(err))

print("Refreshed " + str(count) + " agents")
