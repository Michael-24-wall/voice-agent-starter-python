import os, json
from lib import load_env
load_env()
import agent_manager, db

user = db.first_user_of_type("hospital")
body = agent_manager.build_user_agent(user)

print("=== TOOLS ===")
for tool in body.get("tools", []):
    http = tool.get("http") or {}
    print(tool["name"])
    print("  url: " + str(http.get("url")))
    print("  method: " + str(http.get("method")))
    print("  timeout: " + str(tool.get("timeout_seconds")))
    print("  mode: " + str(tool.get("execution_mode")))

print()
print("=== NAME ===")
print(body.get("name"))
print()
print("=== GREETING ===")
print(body.get("greeting"))
print()
print("=== VOICE ===")
print(body.get("voice"))
print()
print("=== BODY SIZE ===")
print(str(len(json.dumps(body))) + " bytes")
