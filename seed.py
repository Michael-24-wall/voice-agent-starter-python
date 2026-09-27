"""Create the demo account so judges can log in without signing up.

Safe to run repeatedly: an existing demo account is left alone, including its
password, so a judge mid-demo is not logged out by someone else re-seeding.
"""

import db
from auth import hash_password

DEMO_EMAIL = "demo@calldesk.test"
DEMO_PASSWORD = "demo1234"
DEMO_BUSINESS = "Demo Plumbing Co."


def main():
    db.init_db()
    if db.get_user_by_email(DEMO_EMAIL):
        print("Demo user already exists")
        return 0
    db.create_user(DEMO_EMAIL, hash_password(DEMO_PASSWORD), DEMO_BUSINESS)
    user = db.get_user_by_email(DEMO_EMAIL)
    db.add_phone_number(
        user["id"], "+15550000000", agent_id=lib_agent_id(), twilio_sid="PNdemo00000000000000000000000000"
    )
    db.log_call(
        user["id"], "Jordan Reyes", "+15551234567",
        "Water heater is leaking under the sink, water is shut off at the main.",
        "high",
        "Hi, this is Jordan. My water heater is leaking and I already shut the "
        "water off. How soon can someone get here?",
    )
    db.log_call(
        user["id"], "Sam Patel", "+15557654321",
        "Bathroom faucet dripping, would like it looked at this week.",
        "low",
        "Caller called back about the dripping faucet, no urgency.",
    )
    print("Demo user created")
    print(f"  {DEMO_EMAIL} / {DEMO_PASSWORD}  ({DEMO_BUSINESS})")
    return 0


def lib_agent_id():
    from lib import load_env, stored_agent_id

    load_env()
    return stored_agent_id("calldesk") or None


if __name__ == "__main__":
    raise SystemExit(main())
