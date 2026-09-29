"""Local demo data for the four CallDesk verticals.

Run it after `python -c "import db; db.init_db()"`, or just run this: it calls
init_db itself.

    python seed_business.py

It creates one business of each kind, with the inventory their agent needs to
answer a real call, and prints the logins. Safe to run more than once: an
account that already exists is left alone rather than duplicated, and each
inventory list is only added to when the business has nothing of that kind yet.

This is demo data for a local install. The passwords below are printed here
because they are throwaway, so do not run it against a deployment whose
customers you care about: every seeded account shares one known password.
"""

import db

# Throwaway. Local demo only.
DEMO_PASSWORD = "call-desk-demo"

HOTEL_EMAIL = "hotel@demo.calldesk"
HOSPITAL_EMAIL = "hospital@demo.calldesk"
RESTAURANT_EMAIL = "restaurant@demo.calldesk"

HOTEL = {
    "business_name": "Harbour View Inn",
    "mobile_number": "+15550100001",
    "rooms": [
        ("101", "standard", 110.0, 2, "WiFi, city view"),
        ("102", "standard", 110.0, 3, "WiFi"),
        ("201", "deluxe", 175.0, 3, "WiFi, balcony, harbour view"),
        ("202", "deluxe", 175.0, 4, "WiFi, balcony, kitchenette"),
        ("301", "suite", 260.0, 5, "WiFi, balcony, lounge, kitchenette"),
    ],
}

HOSPITAL = {
    "business_name": "Northside Clinic",
    "mobile_number": "+15550100002",
    "departments": [
        ("Cardiology", "Second floor. Walk-ins welcome."),
        ("Dermatology", "Ground floor, room 4."),
        ("Pediatrics", "Ground floor, children's wing."),
    ],
    # (department, doctor, date, time)
    "slots": [
        ("Cardiology", "Dr Ito", "2026-10-01", ["09:00", "09:30", "10:00", "11:00"]),
        ("Cardiology", "Dr Rao", "2026-10-01", ["10:30", "11:30", "14:00"]),
        ("Dermatology", "Dr Bell", "2026-10-01", ["09:00", "10:00", "15:00"]),
        ("Pediatrics", "Dr Chen", "2026-10-01", ["08:30", "09:30", "10:30", "13:00"]),
    ],
    "bookings": [
        ("Cardiology", "Dr Ito", "2026-10-01 09:00", "Priya Raman", "+15551110001",
         "Follow-up on blood pressure"),
    ],
}

RESTAURANT = {
    "business_name": "The Copper Pot",
    "mobile_number": "+15550100003",
    "tables": [("T1", 2), ("T2", 2), ("T3", 4), ("T4", 4), ("T5", 6), ("T6", 8)],
    "reservations": [
        ("T5", "Sam Whitfield", "+15551110002", 5, "2026-10-01 19:00:00"),
    ],
}


def account(email, business_name, business_type, mobile_number):
    """Create the account, or return the one that already exists."""
    existing = db.get_user_by_email(email)
    if existing:
        print(f"  {email} already exists as {existing['business_type']}, leaving it")
        return existing["id"], False
    # Imported here rather than at the top so the module reads as data first.
    import auth
    user_id = db.create_user(email, auth.hash_password(DEMO_PASSWORD),
                             business_name, mobile_number, business_type)
    print(f"  created {business_type} account {email}")
    return user_id, True


def seed_hotel():
    user_id, fresh = account(HOTEL_EMAIL, HOTEL["business_name"], "hotel",
                             HOTEL["mobile_number"])
    if db.list_rooms(user_id):
        print(f"  {HOTEL_EMAIL} already has rooms, leaving them")
        return user_id
    for number, kind, price, sleeps, amenities in HOTEL["rooms"]:
        db.add_room(user_id, number, kind, price, sleeps, amenities)
    print(f"  {len(HOTEL['rooms'])} rooms")

    # A booking on the first room, so the bookings table is not empty and the
    # availability tool has something to exclude.
    rooms = db.find_available_rooms(user_id, "2026-10-01", "2026-10-03")
    if rooms:
        db.book_room(user_id, rooms[0]["room_number"], "Jonas Berg", "+15551110003",
                     "2026-10-01", "2026-10-03")
        print(f"  1 booking on {rooms[0]['room_number']} for 2026-10-01 to 2026-10-03")
    return user_id


def seed_hospital():
    user_id, fresh = account(HOSPITAL_EMAIL, HOSPITAL["business_name"], "hospital",
                             HOSPITAL["mobile_number"])
    if db.list_departments(user_id):
        print(f"  {HOSPITAL_EMAIL} already has departments, leaving them")
        return user_id
    by_name = {}
    for name, description in HOSPITAL["departments"]:
        by_name[name] = db.add_department(user_id, name, description)
    print(f"  {len(by_name)} departments")

    count = 0
    for department, doctor, date, times in HOSPITAL["slots"]:
        for time in times:
            db.add_slot(user_id, by_name[department], doctor, f"{date} {time}:00", 30)
            count += 1
    print(f"  {count} appointment slots")

    for department, doctor, when, patient, phone, reason in HOSPITAL["bookings"]:
        try:
            db.book_appointment(user_id, department, doctor, patient, phone, when, reason)
        except ValueError as err:
            # A slot already taken just means a previous run got here first.
            print(f"  skipped {department} {when}: {err}")
    print("  1 appointment")
    return user_id


def seed_restaurant():
    user_id, fresh = account(RESTAURANT_EMAIL, RESTAURANT["business_name"],
                             "restaurant", RESTAURANT["mobile_number"])
    if db.list_tables(user_id):
        print(f"  {RESTAURANT_EMAIL} already has tables, leaving them")
        return user_id
    for number, seats in RESTAURANT["tables"]:
        db.add_table(user_id, number, seats)
    print(f"  {len(RESTAURANT['tables'])} tables")

    for table, guest, phone, size, when in RESTAURANT["reservations"]:
        try:
            db.book_table(user_id, table, guest, phone, size, when)
        except ValueError as err:
            print(f"  skipped {table} {when}: {err}")
    print("  1 reservation")
    return user_id


def main():
    db.init_db()
    print(f"Database: {db.DB_PATH}\n")
    print("Seeding CallDesk demo data\n")
    print("Hotel")
    seed_hotel()
    print("\nHospital")
    seed_hospital()
    print("\nRestaurant")
    seed_restaurant()

    print("\nDemo logins. The password is the same for all three:\n")
    for email, name in ((HOTEL_EMAIL, HOTEL["business_name"]),
                        (HOSPITAL_EMAIL, HOSPITAL["business_name"]),
                        (RESTAURANT_EMAIL, RESTAURANT["business_name"])):
        print(f"  {name}")
        print(f"    {email}")
    print(f"\n  password: {DEMO_PASSWORD}")
    print("\nNone of these agents are published yet. Sign in, add anything the")
    print("demo did not cover, then press Activate agent on the setup page.")


if __name__ == "__main__":
    main()
