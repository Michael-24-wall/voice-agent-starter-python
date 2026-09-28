"""Create the demo account so judges can log in without signing up.

Safe to run repeatedly: an existing demo account is left alone, including its
password, so a judge mid-demo is not logged out by someone else re-seeding.
"""

import db
from auth import hash_password
from lib import load_env, stored_agent_id

DEMO_EMAIL = "demo@calldesk.test"
DEMO_PASSWORD = "demo1234"
DEMO_BUSINESS = "Demo Plumbing Co."
DEMO_MOBILE = "+15551234567"
DEMO_NUMBER = "+15550000000"

SAMPLE_CALLS = [
    ("Jordan Reyes", "+15551234567",
     "Water heater is leaking under the sink, water is shut off at the main.",
     "high",
     "Hi, this is Jordan. My water heater is leaking and I already shut the "
     "water off. How soon can someone get here?"),
    ("Sam Patel", "+15557654321",
     "Bathroom faucet dripping, would like it looked at this week.",
     "low",
     "Caller called back about the dripping faucet, no urgency."),
]


def main():
    db.init_db()
    load_env()
    if db.get_user_by_email(DEMO_EMAIL):
        user = db.get_user_by_email(DEMO_EMAIL)
        # An account created before mobile_number existed has none, and Mode B
        # needs one to ring. Fill it in without touching the password.
        if not (user.get("mobile_number") or "").strip():
            db.update_user_profile(user["id"], mobile_number=DEMO_MOBILE)
        print("Demo user already exists")
        print(f"  {DEMO_EMAIL} / {DEMO_PASSWORD}  ({DEMO_BUSINESS})")
        return 0

    user_id = db.create_user(DEMO_EMAIL, hash_password(DEMO_PASSWORD),
                             DEMO_BUSINESS, DEMO_MOBILE)
    db.add_phone_number(user_id, DEMO_NUMBER,
                        agent_id=stored_agent_id("calldesk") or None)
    for name, number, problem, urgency, transcript in SAMPLE_CALLS:
        db.log_call(user_id, name, number, problem, urgency, transcript)
    print("Demo user created")
    print(f"  {DEMO_EMAIL} / {DEMO_PASSWORD}  ({DEMO_BUSINESS})")
    print(f"  mobile {DEMO_MOBILE}, {len(SAMPLE_CALLS)} sample calls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
