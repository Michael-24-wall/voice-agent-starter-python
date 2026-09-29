from pathlib import Path
import re

path = Path(r"C:\Users\Michael\Desktop\calldesk\agents\restaurant-agent.jsonc")
text = path.read_text(encoding="utf-8")

# Remove the opening-hours sentence we added earlier
text = text.replace(
    " The restaurant is open from 6 PM to 11 PM daily. If a caller asks for a "
    "time before 6 PM, tell them we open at 6 PM and offer the earliest "
    "reservation at 6 PM.",
    ""
)
text = text.replace(
    " The restaurant opens at 6 PM and closes at 11 PM. If a caller asks for a time outside these hours, tell them the earliest available time is 6 PM.",
    ""
)

path.write_text(text, encoding="utf-8")
print("Removed the 6 PM rule.")
