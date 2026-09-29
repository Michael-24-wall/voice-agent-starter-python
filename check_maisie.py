from lib import load_env
load_env()
import db, os, json, urllib.request

conn = db.connect()
user = conn.execute(
    "SELECT id, business_name, agent_id FROM users WHERE LOWER(business_name) LIKE '%maisie%'"
).fetchone()
print("Maisie user id:", user["id"])
print("Maisie agent id:", user["agent_id"])
print()
print("Tool URLs on the live agent:")
req = urllib.request.Request(
    f'https://agents.assemblyai.com/v1/agents/{user["agent_id"]}',
    headers={"Authorization": os.environ["ASSEMBLYAI_API_KEY"]}
)
d = json.load(urllib.request.urlopen(req))
for t in d.get("tools", []):
    url = (t.get("http") or {}).get("url")
    print(f"  {t['name']}: {url}")
