from pathlib import Path

path = Path(r"C:\Users\Michael\Desktop\calldesk\deployment\browser\server.py")
text = path.read_text(encoding="utf-8")

old = '''    def _tool_check_availability(self) -> None:
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
'''

new = '''    def _tool_check_availability(self) -> None:
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
'''

if old not in text:
    print("Already patched, or pattern not found.")
else:
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    print("Patched _tool_check_availability.")
