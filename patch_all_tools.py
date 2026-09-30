"""Patch every tool in server.py to return a message field, then republish.

Run from the project root:
    python patch_all_tools.py
"""
import re
from pathlib import Path

path = Path(r"C:\Users\Michael\Desktop\calldesk\deployment\browser\server.py")
text = path.read_text(encoding="utf-8")

patched = []
skipped = []


def replace_block(old, new, label):
    global text
    if old not in text:
        skipped.append(label)
        return
    text = text.replace(old, new, 1)
    patched.append(label)


# ---------------------------------------------------------------------
# 1. Hotel — check_availability
# ---------------------------------------------------------------------
replace_block(
    '''    def _tool_check_availability(self) -> None:
        def work(args, user):
            rooms = db.find_available_rooms(
                user["id"], args.get("check_in"), args.get("check_out"),
                args.get("room_type"))
            return {
                "available": bool(rooms),
                "room_count": len(rooms),
                "rooms": [{"room_number": r["room_number"], "room_type": r["room_type"],
                           "price_per_night": r["price_per_night"],
                           "capacity": r["capacity"], "amenities": r["amenities"] or ""}
                          for r in rooms],
            }
        self._tool_run("hotel", work)
''',
    '''    def _tool_check_availability(self) -> None:
        def work(args, user):
            rooms = db.find_available_rooms(
                user["id"], args.get("check_in"), args.get("check_out"),
                args.get("room_type"))
            if not rooms:
                return {"message": "No rooms are available for those dates. "
                                   "Offer different dates or a different room type."}
            first = rooms[0]
            return {
                "message": f"{len(rooms)} rooms are available. "
                           f"Offer to book room {first['room_number']} "
                           f"({first['room_type']}, ${first['price_per_night']:.0f}/night, "
                           f"sleeps {first['capacity']}).",
                "rooms": [{"room_number": r["room_number"], "room_type": r["room_type"],
                           "price_per_night": r["price_per_night"],
                           "capacity": r["capacity"], "amenities": r["amenities"] or ""}
                          for r in rooms],
            }
        self._tool_run("hotel", work)
''',
    "check_availability (hotel)",
)


# ---------------------------------------------------------------------
# 2. Hotel — book_room
# ---------------------------------------------------------------------
replace_block(
    '''    def _tool_book_room(self) -> None:
        def work(args, user):
            return db.book_room(
                user["id"], args.get("room_number"), args.get("guest_name"),
                args.get("guest_phone"), args.get("check_in"), args.get("check_out"))
        self._tool_run("hotel", work)
''',
    '''    def _tool_book_room(self) -> None:
        def work(args, user):
            result = db.book_room(
                user["id"], args.get("room_number"), args.get("guest_name"),
                args.get("guest_phone"), args.get("check_in"), args.get("check_out"))
            return {
                "message": f"Booked room {result['room_number']} for "
                           f"{result['guest_name']} from {result['check_in']} "
                           f"to {result['check_out']}. Confirm this to the caller.",
                **result,
            }
        self._tool_run("hotel", work)
''',
    "book_room (hotel)",
)


# ---------------------------------------------------------------------
# 3. Hospital — check_slots
# ---------------------------------------------------------------------
replace_block(
    '''    def _tool_check_slots(self) -> None:
        def work(args, user):
            print(f"check_slots {json.dumps(args, sort_keys=True)}", flush=True)
            department = args.get("department")
            requested_date = args.get("date")
            dept, requested, target = db.schedule_info(user["id"], department, requested_date)
            result = {"slots": [], "date": target, "next_available": target}
            if requested_date and target != requested:
                result["message"] = f"That date is not an operating day. The next available day is {target}."
            count = db.department_daily_count(user["id"], dept["id"], target)
            if count >= int(dept["daily_capacity"] or 16):
                result["message"] = "Department is fully booked on that date."
                result["next_available"] = db.next_available_date(user["id"], department, target)
                print(f"check_slots result {json.dumps(result, sort_keys=True)}", flush=True)
                return result
            slots = db.find_available_slots(user["id"], department, requested_date)
            result["slots"] = [
                {"doctor": s["doctor_name"] or "",
                 "datetime": s["slot_datetime"],
                 "duration_minutes": s["duration_minutes"]}
                for s in slots
            ]
            if not slots:
                result["next_available"] = db.next_available_date(user["id"], department, target)
            print(f"check_slots result {json.dumps(result, sort_keys=True)}", flush=True)
            return result
        self._tool_run("hospital", work)
''',
    '''    def _tool_check_slots(self) -> None:
        def work(args, user):
            print(f"check_slots {json.dumps(args, sort_keys=True)}", flush=True)
            department = args.get("department")
            requested_date = args.get("date")
            dept, requested, target = db.schedule_info(user["id"], department, requested_date)
            slots = db.find_available_slots(user["id"], department, requested_date)

            if not slots:
                next_date = db.next_available_date(user["id"], department, target)
                result = {
                    "message": f"No openings for {dept['name']} on {target}. "
                               f"The next available day is {next_date}. "
                               f"Offer that day to the caller.",
                    "slots": [], "date": target, "next_available": next_date,
                }
                print(f"check_slots result {json.dumps(result, sort_keys=True)}", flush=True)
                return result

            times = [s["slot_datetime"] for s in slots[:3]]
            doctor = slots[0]["doctor_name"] or "the doctor"
            result = {
                "message": f"{len(slots)} openings for {dept['name']} with {doctor}. "
                           f"Offer these times: {', '.join(times)}. "
                           f"Ask which one the caller would like.",
                "slots": [{"doctor": s["doctor_name"] or "",
                           "datetime": s["slot_datetime"],
                           "duration_minutes": s["duration_minutes"]}
                          for s in slots],
                "date": target, "next_available": target,
            }
            print(f"check_slots result {json.dumps(result, sort_keys=True)}", flush=True)
            return result
        self._tool_run("hospital", work)
''',
    "check_slots (hospital)",
)


# ---------------------------------------------------------------------
# 4. Hospital — book_appointment
# ---------------------------------------------------------------------
replace_block(
    '''    def _tool_book_appointment(self) -> None:
        def work(args, user):
            result = db.book_appointment(
                user["id"], args.get("department"), args.get("slot_datetime"),
                args.get("patient_name"), args.get("patient_phone"), args.get("reason"),
                patient_email=args.get("patient_email"))
            try:
                email_sender.send_confirmation_email_for_appointment(result["appointment_id"])
            except Exception as err:
                print(f"Confirmation email failed: {err}", flush=True)
            return result
        self._tool_run("hospital", work)
''',
    '''    def _tool_book_appointment(self) -> None:
        def work(args, user):
            result = db.book_appointment(
                user["id"], args.get("department"), args.get("slot_datetime"),
                args.get("patient_name"), args.get("patient_phone"), args.get("reason"),
                patient_email=args.get("patient_email"))
            try:
                email_sender.send_confirmation_email_for_appointment(result["appointment_id"])
            except Exception as err:
                print(f"Confirmation email failed: {err}", flush=True)
            return {
                "message": f"Appointment confirmed for {result['datetime']} with "
                           f"{result['department']}. The patient will receive a "
                           f"confirmation email. Confirm this to the caller.",
                **result,
            }
        self._tool_run("hospital", work)
''',
    "book_appointment (hospital)",
)


# ---------------------------------------------------------------------
# 5. Hospital — triage_symptoms
# ---------------------------------------------------------------------
replace_block(
    '''    def _tool_triage_symptoms(self) -> None:
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
''',
    '''    def _tool_triage_symptoms(self) -> None:
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
''',
    "triage_symptoms (hospital)",
)


# ---------------------------------------------------------------------
# 6. Restaurant — check_tables
# ---------------------------------------------------------------------
replace_block(
    '''    def _tool_check_tables(self) -> None:
        def work(args, user):
            tables = db.find_available_tables(
                user["id"], args.get("datetime"), args.get("party_size"))
            return {
                "available": bool(tables),
                "table_count": len(tables),
                "tables": [{"table_number": t["table_number"], "capacity": t["capacity"]}
                           for t in tables],
            }
        self._tool_run("restaurant", work)
''',
    '''    def _tool_check_tables(self) -> None:
        def work(args, user):
            tables = db.find_available_tables(
                user["id"], args.get("datetime"), args.get("party_size"))
            if not tables:
                return {"message": "No tables are available at that time. "
                                   "Offer a different time."}
            first = tables[0]
            return {
                "message": f"{len(tables)} tables are available. "
                           f"Offer to book table {first['table_number']} "
                           f"(seats {first['capacity']}) at the requested time.",
                "tables": [{"table_number": t["table_number"], "capacity": t["capacity"]}
                           for t in tables],
            }
        self._tool_run("restaurant", work)
''',
    "check_tables (restaurant)",
)


# ---------------------------------------------------------------------
# 7. Restaurant — book_table
# ---------------------------------------------------------------------
replace_block(
    '''    def _tool_book_table(self) -> None:
        def work(args, user):
            return db.book_table(
                user["id"], args.get("table_number"), args.get("guest_name"),
                args.get("guest_phone"), args.get("party_size"),
                args.get("reservation_datetime"))
        self._tool_run("restaurant", work)
''',
    '''    def _tool_book_table(self) -> None:
        def work(args, user):
            result = db.book_table(
                user["id"], args.get("table_number"), args.get("guest_name"),
                args.get("guest_phone"), args.get("party_size"),
                args.get("reservation_datetime"))
            return {
                "message": f"Reserved table {result['table_number']} for "
                           f"{result['party_size']} at {result['reservation_datetime']}. "
                           f"Confirm this to the caller.",
                **result,
            }
        self._tool_run("restaurant", work)
''',
    "book_table (restaurant)",
)


# ---------------------------------------------------------------------
# 8. Service — send_summary
# ---------------------------------------------------------------------
replace_block(
    '''    def _handle_send_summary(self) -> None:''',
    '''    def _handle_send_summary(self) -> None:''',
    "send_summary (service) — no change needed",
)


# ---------------------------------------------------------------------
# Save and report
# ---------------------------------------------------------------------
path.write_text(text, encoding="utf-8")

print("Patched:")
for p in patched:
    print("  OK  ", p)
print()
if skipped:
    print("Skipped (already patched or pattern not found):")
    for s in skipped:
        print("  SKIP", s)
    print()

print("File saved:", path)
print()
print("Now restart the server, then run republish_all.py.")
