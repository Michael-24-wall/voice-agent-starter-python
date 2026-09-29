from pathlib import Path
import re

path = Path(r"C:\Users\Michael\Desktop\calldesk\agents\restaurant-agent.jsonc")
text = path.read_text(encoding="utf-8")

addition = (
    " The check_tables tool returns a JSON object with an 'available' boolean, "
    "a 'table_count' number, and a 'tables' array. When available is true, "
    "pick a table from the tables array and offer to book it. Each table has "
    "'table_number' and 'capacity'. Never say there are no tables when "
    "available is true. If available is false, offer a different time. "
    "The restaurant is open from 6 PM to 11 PM daily. If a caller asks for a "
    "time before 6 PM, tell them we open at 6 PM and offer the earliest "
    "reservation at 6 PM."
)

pattern = re.compile(r'("system_prompt"\s*:\s*")([^"]*)(")', re.DOTALL)
def inject(m):
    return m.group(1) + m.group(2) + addition + m.group(3)

new_text, count = pattern.subn(inject, text, count=1)
if count == 0:
    print("WARNING: system_prompt not found")
else:
    path.write_text(new_text, encoding="utf-8")
    print("Patched restaurant-agent.jsonc system prompt.")
