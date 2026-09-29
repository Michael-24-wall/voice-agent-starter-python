"""SMTP email reminders for hospital appointments."""

import os
import smtplib
from email.message import EmailMessage


def send_email(to_address, subject, body, user_id):
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com").strip()
    port = int(os.environ.get("SMTP_PORT", "587"))
    username = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    from_name = os.environ.get("SMTP_FROM_NAME", "CallDesk").strip() or "CallDesk"
    if not username or not password:
        print(f"EMAIL (would send): {subject} to {to_address}", flush=True)
        return True

    message = EmailMessage()
    message["To"] = to_address
    message["From"] = f"{from_name} <{username}>"
    message["Subject"] = subject
    message.set_content(body)
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        smtp.starttls()
        smtp.login(username, password)
        smtp.send_message(message)
    return True


def send_email_reminder(appointment, user, settings):
    business_name = user.get("business_name") or appointment.get("business_name") or "our clinic"
    slot = str(appointment.get("slot_datetime") or "")
    date, _, time = slot.partition(" ")
    values = {
        "business_name": business_name,
        "patient_name": appointment.get("patient_name") or "there",
        "date": date,
        "time": time,
        "department": appointment.get("department") or "the clinic",
        "patient_phone": appointment.get("patient_phone") or "",
    }

    def render(template):
        text = str(template or "")
        for key, value in values.items():
            text = text.replace("{" + key + "}", str(value))
        return text

    subject = render(settings.get("email_subject_template"))
    body = render(settings.get("email_body_template"))
    return send_email(appointment.get("patient_email"), subject, body, user.get("id"))


def send_confirmation_email(appointment, user):
    business_name = user.get("business_name") or appointment.get("business_name") or "our clinic"
    slot = str(appointment.get("slot_datetime") or "")
    date, _, time = slot.partition(" ")
    body = (
        f"Hi {appointment.get('patient_name') or 'there'},\n\n"
        "Your appointment has been confirmed.\n\n"
        f"Department: {appointment.get('department') or 'the clinic'}\n"
        f"Date: {date}\n"
        f"Time: {time}\n"
        f"Doctor: {appointment.get('doctor_name') or 'the care team'}\n"
        f"Location: {business_name}\n\n"
        "If you need to reschedule or cancel, please call us back.\n\n"
        f"Thank you for choosing {business_name}."
    )
    subject = f"Your appointment is confirmed \u2014 {business_name}"
    return send_email(appointment.get("patient_email"), subject, body, user.get("id"))


def send_confirmation_email_for_appointment(appointment_id, force=False):
    import db

    appointment = db.get_hospital_appointment(appointment_id)
    if not appointment:
        return False
    if not appointment.get("patient_email"):
        print(f"No email on file for appointment {appointment['id']}", flush=True)
        return False
    if appointment.get("confirmation_sent") and not force:
        return True
    user = db.get_user_by_id(appointment["user_id"])
    if not user:
        return False
    sent = send_confirmation_email(appointment, user)
    if sent:
        db.mark_confirmation_sent(appointment["id"])
    return bool(sent)


def send_confirmation_email_for_latest_appointment(user_id):
    import db

    with db.connect() as conn:
        row = conn.execute(
            "SELECT id FROM hospital_appointments WHERE user_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1", (int(user_id),)
        ).fetchone()
    if not row:
        return False
    return send_confirmation_email_for_appointment(row["id"])
