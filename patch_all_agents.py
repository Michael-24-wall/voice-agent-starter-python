from pathlib import Path
import re

# The universal rule that tells every agent to read the message field
RULE = (
    " Every tool returns a JSON object with a 'message' field written in "
    "plain English. Always read that message back to the caller. When it "
    "says something is available, offer to book it. When it says nothing is "
    "available, offer an alternative. Never say you cannot check, cannot "
    "access, or are having trouble, because the tools always return a "
    "message."
)

# Map each agent file to its own extra rule, if needed
AGENTS = {
    "restaurant-agent.jsonc": RULE,
    "hotel-agent.jsonc": RULE,
    "hospital-agent.jsonc": RULE,
    "calldesk.jsonc": (
        " Every tool returns a JSON object with a 'message' field written in "
        "plain English. Read it back to the caller."
    ),
}

agents_dir = Path(r"C:\Users\Michael\Desktop\calldesk\agents")

for filename, rule in AGENTS.items():
    path = agents_dir / filename
    if not path.exists():
        print(f"skip {filename}: not found")
        continue

    text = path.read_text(encoding="utf-8")

    # Skip if already patched
    if "returns a JSON object with a 'message' field" in text:
        print(f"skip {filename}: already patched")
        continue

    # Insert the rule at the end of the system_prompt
    pattern = re.compile(r'("system_prompt"\s*:\s*")([^"]*)(")', re.DOTALL)
    new_text, count = pattern.subn(
        lambda m: m.group(1) + m.group(2) + rule + m.group(3),
        text, count=1
    )

    if count == 0:
        print(f"WARNING {filename}: system_prompt not found")
        continue

    path.write_text(new_text, encoding="utf-8")
    print(f"patched {filename}")

print()
print("Now re-publishing all agents...")
