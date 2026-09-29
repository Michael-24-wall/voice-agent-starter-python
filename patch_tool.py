from pathlib import Path

path = Path(r"C:\Users\Michael\Desktop\calldesk\deployment\browser\server.py")
text = path.read_text(encoding="utf-8")

old = '''    def _tool_run(self, business_type: str, work) -> None:
        """Run one tool body with the shared envelope around it."""
        args = self._tool_args()
        if args is None:
            return
        user = self._tool_user(args, business_type)
        if user is None:
            return
        try:
            self._tool_ok(work(args, user))
        except ValueError as err:
            # db raises these with a message meant to be spoken.
            print(f"tool: {business_type} rejected a call: {err}", flush=True)
            self._tool_fail(str(err))
        except Exception as err:  # never leak a traceback to the agent
            print(f"tool: {business_type} failed: {err!r}", flush=True)
            self._tool_fail("our system could not complete that just now. "
                            "Please call back in a moment.")
'''

new = '''    def _tool_run(self, business_type: str, work) -> None:
        """Run one tool body and return the result directly.

        The agent reads the raw JSON body, so the data is not wrapped in an
        envelope. An error is returned as {"error": "..."} which the agent
        can speak.
        """
        args = self._tool_args()
        if args is None:
            return
        user = self._tool_user(args, business_type)
        if user is None:
            return
        try:
            self._tool_json(work(args, user))
        except ValueError as err:
            print(f"tool: {business_type} rejected a call: {err}", flush=True)
            self._tool_json({"error": str(err)})
        except Exception as err:
            print(f"tool: {business_type} failed: {err!r}", flush=True)
            self._tool_json({"error": "our system could not complete that "
                             "just now. Please call back in a moment."})
'''

if old not in text:
    print("ERROR: pattern not found. The function may have different whitespace.")
    raise SystemExit(1)

path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("Patched _tool_run: responses are now unwrapped.")
