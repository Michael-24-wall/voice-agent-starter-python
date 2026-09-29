from lib import load_env
load_env()
import db, agent_manager
conn = db.connect()
for r in conn.execute("SELECT id, business_name FROM users WHERE business_type = 'hotel'").fetchall():
    aid = agent_manager.publish_user_agent(r["id"])
    print("hotel user", r["id"], r["business_name"], "->", aid)
