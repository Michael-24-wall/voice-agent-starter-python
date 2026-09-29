"""Background SMS and email reminder processing."""

import os
import smtplib
import urllib.parse
from datetime import datetime, timedelta

import db
from email_sender import send_email_reminder
from lib import ApiError, twilio


def _render_sms(appointment, user, settings):
    slot = str(appointment.get("slot_datetime") or "")
    date, _, time = slot.partition(" ")
    values = {
        "business_name": user.get("business_name") or appointment.get("business_name") or "our clinic",
        "patient_name": appointment.get("patient_name") or "there",
        "date": date,
        "time": time,
        "department": appointment.get("department") or "the clinic",
        "patient_phone": appointment.get("patient_phone") or "",
    }
    text = settings.get("sms_message_template") or db.DEFAULT_SMS_TEMPLATE
    for key, value in values.items():
        text = text.replace("{" + key + "}", str(value))
    return text


def send_reminder(appointment, user, settings):
    account = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    sender = os.environ.get("TWILIO_PHONE_NUMBER", "").strip()
    recipient = (appointment.get("patient_phone") or "").strip()
    if not (account and token and sender and recipient):
        print(f"SMS reminder skipped for appointment {appointment['id']}: Twilio is not configured",
              flush=True)
        return False
    url = (f"https://api.twilio.com/2010-04-01/Accounts/"
           f"{urllib.parse.quote(account, safe='')}/Messages.json")
    try:
        twilio(url, form={"To": recipient, "From": sender,
                          "Body": _render_sms(appointment, user, settings)},
               account=account, token=token)
    except (ApiError, OSError) as err:
        print(f"SMS reminder failed for appointment {appointment['id']}: {err}", flush=True)
        return False
    return True


def _due(appointment, settings, now):
    if int(settings.get("hours_before", 24)) <= 0:
        return True
    try:
        when = datetime.fromisoformat(str(appointment["slot_datetime"]))
    except ValueError:
        return False
    return now >= when - timedelta(hours=max(0, int(settings.get("hours_before", 24))))


def process_reminders():
    now = datetime.now()

    for appointment in db.list_pending_reminders(48):
        settings = db.get_reminder_settings(appointment["user_id"])
        if not settings["enabled"] or not _due(appointment, settings, now):
            continue
        if send_reminder(appointment, appointment["user"], settings):
            db.mark_reminder_sent(appointment["id"])
            print(f"SMS reminder sent for appointment {appointment['id']}", flush=True)

    for appointment in db.list_pending_email_reminders(48):
        settings = db.get_reminder_settings(appointment["user_id"])
        if not settings["email_enabled"] or not _due(appointment, settings, now):
            continue
        if not appointment.get("patient_email"):
            db.mark_email_reminder_sent(appointment["id"])
            continue
        try:
            send_email_reminder(appointment, appointment["user"], settings)
        except (OSError, smtplib.SMTPException) as err:
            print(f"Email reminder failed for appointment {appointment['id']}: {err}", flush=True)
            continue
        db.mark_email_reminder_sent(appointment["id"])
        print(f"Email reminder sent for appointment {appointment['id']}", flush=True)


def reminder_loop():
    import threading
    while True:
        try:
            process_reminders()
        except Exception as err:
            print(f"reminder scheduler failed: {err!r}", flush=True)
        threading.Event().wait(300)
