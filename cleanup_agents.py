import os, json, urllib.request
from lib import load_env
load_env()
key = os.environ["ASSEMBLYAI_API_KEY"]

# Agents to keep. Anything not in this dict gets deleted.
keep = {
    "agent_3439a3b54b7447098eccd43532483bd1",  # Northside Clinic (current)
    "agent_77e09f71954a4d0aa46b83a96a9d4659",  # Harbour View Inn
    "agent_4845e47b112d46dcbe81f41961f27e16",  # The Copper Pot
    "agent_dbb4ad7b8baf4d09b21ccef8e88990d2",  # qeueless (service)
}

req = urllib.request.Request(
    "https://agents.assemblyai.com/v1/agents",
    headers={"Authorization": key})
agents = json.load(urllib.request.urlopen(req))["agents"]

for a in agents:
    if a["id"] in keep:
        print("keep   ", a["id"], a["name"])
        continue
    del_req = urllib.request.Request(
        "https://agents.assemblyai.com/v1/agents/" + a["id"],
        headers={"Authorization": key},
        method="DELETE")
    try:
        urllib.request.urlopen(del_req)
        print("deleted", a["id"], a["name"])
    except Exception as err:
        print("failed ", a["id"], a["name"], err)
