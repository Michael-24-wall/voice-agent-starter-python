from pathlib import Path
import re

# ============================================================
# 1. server.py — remove GOOGLE_MARK, google_hint, and their calls
# ============================================================
sp = Path(r"C:\Users\Michael\Desktop\calldesk\deployment\browser\server.py")
text = sp.read_text(encoding="utf-8")

# --- Remove the GOOGLE_MARK constant ---
# It starts with "GOOGLE_MARK = (" and ends with the closing ")"
text = re.sub(
    r"\nGOOGLE_MARK = \(.*?\n\)\n",
    "\n",
    text,
    count=1,
    flags=re.DOTALL,
)

# --- Remove the google_hint function ---
# It starts with "def google_hint():" and ends before the next "def "
text = re.sub(
    r"\ndef google_hint\(\):.*?\n(?=def )",
    "\n",
    text,
    count=1,
    flags=re.DOTALL,
)

# --- Remove google_hint from google_login_page ---
text = text.replace(
    "    return page(\"login.html\", \"Log in\", nav_for(None),\n"
    "                ERROR=error_box(message) if message else \"\",\n"
    "                GOOGLE_MARK=GOOGLE_MARK, GOOGLE_HINT=google_hint())",
    "    return page(\"login.html\", \"Log in\", nav_for(None),\n"
    "                ERROR=error_box(message) if message else \"\")",
)

# --- Remove GOOGLE_MARK and GOOGLE_HINT from the /login handler ---
text = text.replace(
    "            self._html(page(\"login.html\", \"Log in\", nav_for(None),\n"
    "                            EMAIL=self._one(\"email\"),\n"
    "                            GOOGLE_MARK=GOOGLE_MARK,\n"
    "                            GOOGLE_HINT=google_hint()))",
    "            self._html(page(\"login.html\", \"Log in\", nav_for(None),\n"
    "                            EMAIL=self._one(\"email\")))",
)

# --- Remove GOOGLE_MARK and GOOGLE_HINT from the /signup handler ---
text = text.replace(
    "            self._html(page(\"signup.html\", \"Sign up\", nav_for(None),\n"
    "                            EMAIL=self._one(\"email\"),\n"
    "                            BUSINESS_NAME=self._one(\"business_name\"),\n"
    "                            MOBILE_NUMBER=self._one(\"mobile_number\"),\n"
    "                            GOOGLE_MARK=GOOGLE_MARK,\n"
    "                            GOOGLE_HINT=google_hint(),\n"
    "                            **type_options(self._one(\"business_type\"))))",
    "            self._html(page(\"signup.html\", \"Sign up\", nav_for(None),\n"
    "                            EMAIL=self._one(\"email\"),\n"
    "                            BUSINESS_NAME=self._one(\"business_name\"),\n"
    "                            MOBILE_NUMBER=self._one(\"mobile_number\"),\n"
    "                            **type_options(self._one(\"business_type\"))))",
)

sp.write_text(text, encoding="utf-8")
print("server.py cleaned")

# ============================================================
# 2. templates — remove the Google button block
# ============================================================
for name in ("login.html", "signup.html"):
    tp = Path(r"C:\Users\Michael\Desktop\calldesk\templates") / name
    if not tp.exists():
        print(f"{name}: not found")
        continue
    t = tp.read_text(encoding="utf-8")

    # Remove the divider + button + hint block. Matches a variety of orders.
    t = re.sub(
        r'\s*<div class="divider">[^<]*</div>\s*'
        r'<button[^>]*google[^>]*>.*?</button>\s*'
        r'(?:\{\{GOOGLE_HINT\}\})?',
        "",
        t,
        flags=re.DOTALL | re.IGNORECASE,
    )
    # Also strip any standalone GOOGLE slots left behind
    t = t.replace("{{GOOGLE_MARK}}", "").replace("{{GOOGLE_HINT}}", "")

    tp.write_text(t, encoding="utf-8")
    print(f"{name} cleaned")

print("Done. Restart the server to see the change.")
