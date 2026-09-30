"""Rewrite every tool handler to return a plain string the LLM can read."""
import re
from pathlib import Path

path = Path(r"C:\Users\Michael\Desktop\calldesk\deployment\browser\server.py")
text = path.read_text(encoding="utf-8")

patched = []

# =============================================================
# HOTEL — check_availability
# =============================================================
old = re.search(
    r"    def _tool_check_availability\(self\) -> None:.*?self\._tool_run\(\"hotel\", work\)",
    text, re.DOTALL
)
if old:
    new = '''    def _tool_check_availability(self) -> None:
        def work(args, user):
            rooms = db.find_available_rooms(
                user["id"], args.get("check_in"), args.get("check_out"),
                args.get("room_type"))
            if not rooms:
                return "No rooms are available for those dates."
            first = rooms[0]
            return (
                f"{len(rooms)} rooms are available. Room "
                f"{first['room_number']} is a {first['room_type']} at "
                f"${first['price_per_night']:.0f} per night. "
                f"Ask the caller if they want to book it."
            )
        self._tool_run("hotel", work)'''
    text = text[:old.start()] + new + text[old.end():]
    patched.append("check_availability (hotel)")

# =============================================================
# HOTEL — book_room
# =============================================================
old = re.search(
    r"    def _tool_book_room\(self\) -> None:.*?self\._tool_run\(\"hotel\", work\)",
    text, re.DOTALL
)
if old:
    new = '''    def _tool_book_room(self) -> None:
        def work(args, user):
            result = db.book_room(
                user["id"], args.get("room_number"), args.get("guest_name"),
                args.get("guest_phone"), args.get("check_in"), args.get("check_out"))
            return (
                f"Booked room {result['room_number']} for {result['guest_name']} "
                f"from {result['check_in']} to {result['check_out']}. "
                f"Confirm this booking to the caller."
            )
        self._tool_run("hotel", work)'''
    text = text[:old.start()] + new + text[old.end():]
    patched.append("book_room (hotel)")

# =============================================================
# HOSPITAL — check_slots
# =============================================================
old = re.search(
    r"    def _tool_check_slots\(self\) -> None:.*?self\._tool_run\(\"hospital\", work\)",
    text, re.DOTALL
)
if old:
    new = '''    def _tool_check_slots(self) -> None:
        def work(args, user):
            print(f"check_slots {json.dumps(args, sort_keys=True)}", flush=True)
            department = args.get("department")
            requested_date = args.get("date")
            dept, requested, target = db.schedule_info(user["id"], department, requested_date)
            slots = db.find_available_slots(user["id"], department, requested_date)

            if not slots:
                next_date = db.next_available_date(user["id"], department, target)
                return (
                    f"No openings for {dept['name']} on {target}. "
                    f"The next available day is {next_date}."
                )

            times = [s["slot_datetime"] for s in slots[:3]]
            doctor = slots[0]["doctor_name"] or "the doctor"
            return (
                f"{len(slots)} openings for {dept['name']} with {doctor}. "
                f"Times: {', '.join(times)}. Ask which one the caller wants."
            )
        self._tool_run("hospital", work)'''
    text = text[:old.start()] + new + text[old.end():]
    patched.append("check_slots (hospital)")

# =============================================================
# HOSPITAL — book_appointment
# =============================================================
old = re.search(
    r"    def _tool_book_appointment\(self\) -> None:.*?self\._tool_run\(\"hospital\", work\)",
    text, re.DOTALL
)
if old:
    new = '''    def _tool_book_appointment(self) -> None:
        def work(args, user):
            result = db.book_appointment(
                user["id"], args.get("department"), args.get("slot_datetime"),
                args.get("patient_name"), args.get("patient_phone"), args.get("reason"),
                patient_email=args.get("patient_email"))
            try:
                email_sender.send_confirmation_email_for_appointment(result["appointment_id"])
            except Exception as err:
                print(f"Confirmation email failed: {err}", flush=True)
            return (
                f"Appointment confirmed for {result['datetime']} with "
                f"{result['department']}. Confirm this to the caller."
            )
        self._tool_run("hospital", work)'''
    text = text[:old.start()] + new + text[old.end():]
    patched.append("book_appointment (hospital)")

# =============================================================
# HOSPITAL — triage_symptoms
# =============================================================
old = re.search(
    r"    def _tool_triage_symptoms\(self\) -> None:.*?self\._tool_run\(\"hospital\", work\)",
    text, re.DOTALL
)
if old:
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
                return (
                    "This sounds like a medical emergency. Tell the caller to "
                    "hang up and call 911 immediately. Do NOT book an appointment."
                )
            return (
                "No emergency indicators. Ask the caller which department they "
                "would like and proceed with booking."
            )
        self._tool_run("hospital", work)'''
    text = text[:old.start()] + new + text[old.end():]
    patched.append("triage_symptoms (hospital)")

# =============================================================
# HOSPITAL — end_call
# =============================================================
old = re.search(
    r"    def _tool_end_call\(self\) -> None:.*?self\._tool_json\(\{.*?\}\)\n",
    text, re.DOTALL
)
if old:
    new = '''    def _tool_end_call(self) -> None:
        args = self._tool_args()
        if args is None:
            return
        user = self._tool_user(args, "hospital")
        if user is None:
            return
        reason = args.get("reason", "completed")
        print(f"end_call reason={reason}", flush=True)
        try:
            email_sender.send_confirmation_email_for_latest_appointment(user["id"])
        except Exception as err:
            print(f"Confirmation email failed: {err}", flush=True)
        self._tool_json("Say goodbye now and end the call.")
'''
    text = text[:old.start()] + new + text[old.end():]
    patched.append("end_call (hospital)")

# =============================================================
# RESTAURANT — check_tables
# =============================================================
old = re.search(
    r"    def _tool_check_tables\(self\) -> None:.*?self\._tool_run\(\"restaurant\", work\)",
    text, re.DOTALL
)
if old:
    new = '''    def _tool_check_tables(self) -> None:
        def work(args, user):
            tables = db.find_available_tables(
                user["id"], args.get("datetime"), args.get("party_size"))
            if not tables:
                return "No tables are available at that time."
            first = tables[0]
            return (
                f"{len(tables)} tables are available. Table "
                f"{first['table_number']} seats {first['capacity']}. "
                f"Ask the caller if they want to book it."
            )
        self._tool_run("restaurant", work)'''
    text = text[:old.start()] + new + text[old.end():]
    patched.append("check_tables (restaurant)")

# =============================================================
# RESTAURANT — book_table
# =============================================================
old = re.search(
    r"    def _tool_book_table\(self\) -> None:.*?self\._tool_run\(\"restaurant\", work\)",
    text, re.DOTALL
)
if old:
    new = '''    def _tool_book_table(self) -> None:
        def work(args, user):
            result = db.book_table(
                user["id"], args.get("table_number"), args.get("guest_name"),
                args.get("guest_phone"), args.get("party_size"),
                args.get("reservation_datetime"))
            return (
                f"Reserved table {result['table_number']} for "
                f"{result['party_size']} at {result['reservation_datetime']}. "
                f"Confirm this to the caller."
            )
        self._tool_run("restaurant", work)'''
    text = text[:old.start()] + new + text[old.end():]
    patched.append("book_table (restaurant)")

# =============================================================
# Adjust _tool_run to wrap a plain string in {"result": ...}
# so the LLM always sees one field named 'result'
# =============================================================
old = re.search(
    r"    def _tool_run\(self, business_type: str, work\) -> None:.*?print\(f\"tool: \{business_type\} failed: \{err!r\}\", flush=True\)\n            self\._tool_json\(\{\"error\": \"our system could not complete that \"\n                             \"just now\. Please call back in a moment\.\"\}\)",
    text, re.DOTALL
)
if old:
    new = '''    def _tool_run(self, business_type: str, work) -> None:
        """Run one tool body and return the result as {"result": "..."} .

        The LLM reads a single result string. No nested JSON, no arrays.
        """
        args = self._tool_args()
        if args is None:
            return
        user = self._tool_user(args, business_type)
        if user is None:
            return
        try:
            result = work(args, user)
            if isinstance(result, str):
                self._tool_json({"result": result})
            else:
                self._tool_json(result)
        except ValueError as err:
            print(f"tool: {business_type} rejected a call: {err}", flush=True)
            self._tool_json({"result": str(err)})
        except Exception as err:
            print(f"tool: {business_type} failed: {err!r}", flush=True)
            self._tool_json({"result": "our system could not complete that just now."})'''
    text = text[:old.start()] + new + text[old.end():]
    patched.append("_tool_run (wraps strings in result)")

path.write_text(text, encoding="utf-8")
print("Patched:")
for p in patched:
    print("  OK  ", p)
if not patched:
    print("  nothing matched — the file may already be patched")
