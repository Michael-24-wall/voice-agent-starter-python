from pathlib import Path
import re

# The one rule every agent needs
RULE = (
    " Every tool returns a JSON object with a single field named 'result' "
    "written as plain English. Read the value of 'result' back to the "
    "caller word for word. Never say you cannot check, cannot access, or "
    "are having trouble, because the tools always return a result."
)

agents_dir = Path(r"C:\Users\Michael\Desktop\calldesk\agents")

for name in ("hotel-agent.jsonc", "hospital-agent.jsonc",
             "restaurant-agent.jsonc", "calldesk.jsonc"):
    path = agents_dir / name
    if not path.exists():
        print("skip", name)
        continue
    text = path.read_text(encoding="utf-8")

    if "single field named 'result'" in text:
        print("already patched:", name)
        continue

    # Remove any old message-field rule that may already be in the prompt
    text = text.replace(
        " Every tool returns a JSON object with a 'message' field written in "
        "plain English. Always read that message back to the caller.",
        ""
    )
    text = text.replace(
        " Every tool returns a JSON object with a 'message' field written in "
        "plain English. Read it back to the caller.",
        ""
    )
    text = text.replace(
        " Every tool returns a JSON object with a 'result' field written in "
        "plain English. Always read that result back to the caller.",
        ""
    )

    # Insert the new rule
    pattern = re.compile(r'("system_prompt"\s*:\s*")([^"]*)(")', re.DOTALL)
    new_text, count = pattern.subn(
        lambda m: m.group(1) + m.group(2) + RULE + m.group(3),
        text, count=1
    )
    if count == 0:
        print("WARN system_prompt not found:", name)
        continue
    path.write_text(new_text, encoding="utf-8")
    print("patched prompt:", name)

print()
print("Now refresh every agent on AssemblyAI.")
