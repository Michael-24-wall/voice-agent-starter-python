from pathlib import Path
import re

path = Path(r"C:\Users\Michael\Desktop\calldesk\agents\restaurant-agent.jsonc")
text = path.read_text(encoding="utf-8")

addition = (
    " Every tool returns a JSON object with a 'message' field written as "
    "plain English. Read that message back to the caller. If it says tables "
    "are available, pick one and offer to book it. If it says no tables are "
    "available, offer a different time. Never say you are having trouble "
    "checking availability, because the tools always return a message."
)

pattern = re.compile(r'("system_prompt"\s*:\s*")([^"]*)(")', re.DOTALL)
new_text, count = pattern.subn(
    lambda m: m.group(1) + m.group(2) + addition + m.group(3),
    text, count=1
)
if count == 0:
    print("WARNING: system_prompt not found")
else:
    path.write_text(new_text, encoding="utf-8")
    print("Patched restaurant-agent.jsonc")
