"""Re-publish every user's agent with the current tunnel URL.

Run from the project root:
    python republish_all.py
"""
from lib import load_env
load_env()
import db
import agent_manager

conn = db.connect()
users = conn.execute(
    "SELECT id, business_name, business_type FROM users"
).fetchall()

ok = 0
fail = 0
for u in users:
    user_id = u["id"]
    name = u["business_name"]
    btype = u["business_type"]
    try:
        aid = agent_manager.publish_user_agent(user_id)
        print("  OK   %3d  %-10s  %-30s  ->  %s" % (user_id, btype, name, aid))
        ok += 1
    except Exception as err:
        print("  FAIL %3d  %-10s  %-30s  ->  %s" % (user_id, btype, name, err))
        fail += 1

print()
print("Re-published %d agents, %d failed." % (ok, fail))
print("Done. Open http://localhost:3000/talk in Firefox and test.")
