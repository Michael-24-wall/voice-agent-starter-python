from pathlib import Path

path = Path(r"C:\Users\Michael\Desktop\calldesk\deployment\browser\server.py")
text = path.read_text(encoding="utf-8")

old = '''    def _tool_check_slots(self) -> None:
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
'''

new = '''    def _tool_check_slots(self) -> None:
        def work(args, user):
            print(f"check_slots {json.dumps(args, sort_keys=True)}", flush=True)
            department = args.get("department")
            requested_date = args.get("date")
            dept, requested, target = db.schedule_info(user["id"], department, requested_date)
            count = db.department_daily_count(user["id"], dept["id"], target)
            slots = db.find_available_slots(user["id"], department, requested_date)

            if not slots:
                next_date = db.next_available_date(user["id"], department, target)
                if count >= int(dept["daily_capacity"] or 16):
                    msg = (f"{dept['name']} is fully booked on {target}. "
                           f"The next available day is {next_date}.")
                else:
                    msg = (f"No openings for {dept['name']} on {target}. "
                           f"The next available day is {next_date}.")
                result = {"message": msg, "slots": [], "date": target,
                          "next_available": next_date}
                print(f"check_slots result {json.dumps(result, sort_keys=True)}", flush=True)
                return result

            # Read the first 3 back to the agent as plain text
            times = [s["slot_datetime"] for s in slots[:3]]
            doctor = slots[0]["doctor_name"] or "the doctor"
            readable = ", ".join(times)
            result = {
                "message": f"{len(slots)} openings for {dept['name']} with {doctor}. "
                           f"Offer these times: {readable}. "
                           f"Ask which one the caller would like.",
                "slots": [{"doctor": s["doctor_name"] or "",
                           "datetime": s["slot_datetime"],
                           "duration_minutes": s["duration_minutes"]}
                          for s in slots],
                "date": target,
                "next_available": target,
            }
            print(f"check_slots result {json.dumps(result, sort_keys=True)}", flush=True)
            return result
        self._tool_run("hospital", work)
'''

if old not in text:
    print("ERROR: check_slots pattern not found")
    raise SystemExit(1)

path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("Patched _tool_check_slots")
