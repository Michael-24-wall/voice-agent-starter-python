from pathlib import Path

path = Path(r"C:\Users\Michael\Desktop\calldesk\deployment\browser\server.py")
text = path.read_text(encoding="utf-8")

old = '''    def _tool_triage_symptoms(self) -> None:
        def work(args, user):
            symptoms = (args.get("symptoms") or "").strip()
            emergency_words = (
                "chest pain", "can't breathe", "cannot breathe", "severe bleeding",
                "stroke", "suicidal", "overdose", "severe allergic reaction",
            )
            emergency = any(word in symptoms.lower() for word in emergency_words)
            if emergency:
                db.log_call(user["id"], "Emergency triage", "", symptoms, "emergency")
                return {"risk": "emergency",
                        "message": "Tell the caller to hang up and call 911 immediately."}
            return {"risk": "routine"}
        self._tool_run("hospital", work)
'''

new = '''    def _tool_triage_symptoms(self) -> None:
        def work(args, user):
            symptoms = (args.get("symptoms") or "").strip()
            emergency_words = (
                "chest pain", "can't breathe", "cannot breathe", "severe bleeding",
                "stroke", "suicidal", "overdose", "severe allergic reaction",
            )
            emergency = any(word in symptoms.lower() for word in emergency_words)
            if emergency:
                db.log_call(user["id"], "Emergency triage", "", symptoms, "emergency")
                return {
                    "risk": "emergency",
                    "message": "This sounds like a medical emergency. Tell the "
                               "caller to hang up and call 911 immediately. Do NOT "
                               "book an appointment.",
                }
            return {
                "risk": "routine",
                "message": "No emergency indicators. Ask the caller which "
                           "department they would like and proceed with booking.",
            }
        self._tool_run("hospital", work)
'''

if old not in text:
    print("ERROR: triage pattern not found")
    raise SystemExit(1)

path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("Patched _tool_triage_symptoms")
