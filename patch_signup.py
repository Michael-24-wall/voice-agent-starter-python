from pathlib import Path

path = Path(r"C:\Users\Michael\Desktop\calldesk\deployment\browser\server.py")
text = path.read_text(encoding="utf-8")

old = '''        user_id = db.create_user(email, auth.hash_password(password), business, mobile,
                                 business_type)
        token = auth.create_session(user_id)
        print(f"New account: {business} <{email}> ({business_type})", flush=True)
        # Straight to setup, since an account with no rooms or departments
        # cannot answer its first call.
        self._redirect("/setup", {"Set-Cookie": auth.set_cookie(token)}, status=303,
                       flash=f"Welcome to CallDesk, {business}.")'''

new = '''        user_id = db.create_user(email, auth.hash_password(password), business, mobile,
                                 business_type)
        token = auth.create_session(user_id)
        print(f"New account: {business} <{email}> ({business_type})", flush=True)

        # Publish the agent immediately so /talk works on first login.
        try:
            agent_id = agent_manager.publish_user_agent(user_id)
            print(f"Auto-published agent for {business}: {agent_id}", flush=True)
        except Exception as err:
            print(f"Auto-publish failed for {business}: {err}", flush=True)

        # Straight to setup, since an account with no rooms or departments
        # cannot answer its first call.
        self._redirect("/setup", {"Set-Cookie": auth.set_cookie(token)}, status=303,
                       flash=f"Welcome to CallDesk, {business}.")'''

if old not in text:
    print("ERROR: pattern not found.")
    raise SystemExit(1)

path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("Patched _signup: new accounts auto-publish their agent.")
