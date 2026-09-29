from lib import load_env
load_env()
import db

conn = db.connect()
user = conn.execute(
    "SELECT id, business_name FROM users WHERE LOWER(business_name) LIKE '%maisie%'"
).fetchone()

if not user:
    print("No user with 'maisie' in the name")
else:
    print("User:", dict(user))
    print()
    print("Tables:")
    for t in conn.execute(
        "SELECT table_number, capacity, status FROM restaurant_tables WHERE user_id = ?",
        (user["id"],)
    ).fetchall():
        print(dict(t))
    print()
    print("Reservations:")
    for r in conn.execute(
        "SELECT table_id, guest_name, reservation_datetime FROM restaurant_reservations WHERE user_id = ?",
        (user["id"],)
    ).fetchall():
        print(dict(r))
