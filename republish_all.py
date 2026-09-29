from lib import load_env
load_env()
import db, agent_manager
conn = db.connect()
for r in conn.execute("SELECT id, business_name FROM users").fetchall():
    try:
        aid = agent_manager.publish_user_agent(r["id"])
        print("user", r["id"], r["business_name"], "->", aid)
    except Exception as e:
        print("user", r["id"], r["business_name"], "FAILED:", e)
