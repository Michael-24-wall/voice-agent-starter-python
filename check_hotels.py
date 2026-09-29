from lib import load_env
load_env()
import db

conn = db.connect()
for r in conn.execute("SELECT id, business_name FROM users WHERE business_type = 'hotel'").fetchall():
    print("Hotel user:", dict(r))
    print("Rooms:")
    for room in conn.execute(
        "SELECT room_number, room_type, price_per_night, capacity FROM hotel_rooms WHERE user_id = ?",
        (r["id"],)
    ).fetchall():
        print(" ", dict(room))
    print()
