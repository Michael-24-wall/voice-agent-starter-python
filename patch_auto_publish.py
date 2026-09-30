from pathlib import Path
import re

path = Path(r"C:\Users\Michael\Desktop\calldesk\deployment\browser\server.py")
text = path.read_text(encoding="utf-8")

# ============================================================
# 1. Auto-publish after every inventory save
# ============================================================

# _add_room — re-publish after adding a room
old = '''        db.add_room(user["id"], number, form.get("room_type") or "standard",
                    price, capacity, form.get("amenities"))
        self._setup_back(f"Room {number} added.")'''
new = '''        db.add_room(user["id"], number, form.get("room_type") or "standard",
                    price, capacity, form.get("amenities"))
        try:
            agent_manager.publish_user_agent(user["id"])
        except Exception as err:
            print(f"auto-publish after add_room failed: {err}", flush=True)
        self._setup_back(f"Room {number} added.")'''
text = text.replace(old, new, 1)

# _add_department — re-publish
old = '''        self._setup_back(f"Department {name} added.", path="/setup/hospital")'''
new = '''        try:
            agent_manager.publish_user_agent(user["id"])
        except Exception as err:
            print(f"auto-publish after add_department failed: {err}", flush=True)
        self._setup_back(f"Department {name} added.", path="/setup/hospital")'''
text = text.replace(old, new, 1)

# _update_department — re-publish
old = '''        self._setup_back("Department schedule saved.", path="/setup/hospital")'''
new = '''        try:
            agent_manager.publish_user_agent(user["id"])
        except Exception as err:
            print(f"auto-publish after update_department failed: {err}", flush=True)
        self._setup_back("Department schedule saved.", path="/setup/hospital")'''
text = text.replace(old, new, 1)

# _add_table — re-publish
old = '''        db.add_table(user["id"], number, capacity)
        self._setup_back(f"Table {number} added.")'''
new = '''        db.add_table(user["id"], number, capacity)
        try:
            agent_manager.publish_user_agent(user["id"])
        except Exception as err:
            print(f"auto-publish after add_table failed: {err}", flush=True)
        self._setup_back(f"Table {number} added.")'''
text = text.replace(old, new, 1)

# ============================================================
# 2. Add an auto-republish on the setup page view
#    if the tool base has changed since publish
# ============================================================

# Insert a helper that checks whether the agent needs re-publishing
old = '''    def _setup_page(self, user, path) -> None:'''
new = '''    def _ensure_agent_is_current(self, user) -> None:
        """Re-publish the user's agent if its tool base is stale.

        Runs on every setup page load. If the published agent's tool URL
        does not match the current CALLDESK_TOOL_URL, it is re-published
        so new signups always point at the live tunnel.
        """
        agent_id = agent_manager.get_user_agent(user["id"])
        if not agent_id:
            # No agent yet — publish one now.
            try:
                agent_manager.publish_user_agent(user["id"])
            except Exception as err:
                print(f"setup auto-publish failed for {user['id']}: {err}", flush=True)
            return

        # Check the published agent's tool URL against the current base.
        try:
            base = os.environ.get("CALLDESK_TOOL_BASE") or ""
            if not base:
                url = os.environ.get("CALLDESK_TOOL_URL") or ""
                if url:
                    base = url.rsplit("/", 1)[0] if "/" in url else ""
            if not base:
                return
            live = aai(f"/agents/{agent_id}")
            for tool in live.get("tools", []):
                url = (tool.get("http") or {}).get("url") or ""
                if url and not url.startswith(base):
                    # URL is stale — re-publish
                    agent_manager.publish_user_agent(user["id"])
                    return
        except Exception as err:
            print(f"setup URL check failed for {user['id']}: {err}", flush=True)

    def _setup_page(self, user, path) -> None:'''
text = text.replace(old, new, 1)

# Call it at the top of _setup_page
old = '''        vertical = vertical_of(user)
        if path != "/setup" and vertical != path.split("/")[2]:'''
new = '''        self._ensure_agent_is_current(user)
        vertical = vertical_of(user)
        if path != "/setup" and vertical != path.split("/")[2]:'''
text = text.replace(old, new, 1)

# ============================================================
# 3. Show "Activate agent" prominently on setup pages
# ============================================================

# Change ACTIVATE_LABEL to be more visible
old = '''            # The same button publishes again, so an owner who changes a price
            # or a room can refresh the agent without hunting for a second one.
            "ACTIVATE_LABEL": "Update agent" if agent_id else "Activate agent",'''
new = '''            # The button publishes on demand. After the initial activation,
            # it stays as "Refresh agent" so an owner who changes a price or
            # a room can push the update without hunting for a second button.
            "ACTIVATE_LABEL": "Refresh agent" if agent_id else "Activate agent",'''
text = text.replace(old, new, 1)

path.write_text(text, encoding="utf-8")
print("Patched server.py: setup pages now auto-publish agents.")
