"""
Real outbound SMS via AWS SNS (spec §8 — SNS/SES "send one real SMS to your phone
live on stage" proof).

When a donor confirms, the patient's phone gets an actual text with the donor's
details. Works with the standard boto3 credential chain (env vars / ~/.aws / IAM role):
  AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION (or AWS_DEFAULT_REGION)

In the SNS SMS sandbox you must first verify the destination number in the AWS
console (or via create_sms_sandbox_phone_number). If creds are missing the call
degrades gracefully to a logged "mock send" so the demo never crashes.
"""
import os
from datetime import datetime
from typing import Optional

AWS_REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"

# Last send result (surfaced to the UI so you can see it worked on stage)
last_send = {"status": "none", "to": None, "ts": None, "detail": None}

# Demo patient phone — set live from the UI (Settings) or via PATIENT_PHONE env var.
_patient_phone: Optional[str] = os.environ.get("PATIENT_PHONE") or None


def set_patient_phone(phone: Optional[str]):
    global _patient_phone
    _patient_phone = (phone or "").strip() or None
    return _patient_phone


def get_patient_phone() -> Optional[str]:
    return _patient_phone


def _have_aws() -> bool:
    return bool(os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"))


def _safe_print(s: str):
    """Print without crashing on emoji on a cp1252 (Windows) console."""
    try:
        print(s)
    except UnicodeEncodeError:
        print(s.encode("ascii", "replace").decode("ascii"))


def send_sms(phone: str, message: str) -> dict:
    """
    Publish an SMS to a single phone number (E.164, e.g. +9198XXXXXXXX) via SNS.
    Returns {status, to, detail}.
    """
    global last_send
    ts = datetime.utcnow().isoformat()
    if not phone:
        last_send = {"status": "skipped", "to": None, "ts": ts, "detail": "no patient phone set"}
        return last_send

    if not _have_aws():
        # Graceful mock — log to console so the demo flow still completes.
        _safe_print(f"[sms:MOCK] -> {phone}\n{message}\n(set AWS creds to send for real)")
        last_send = {"status": "mock", "to": phone, "ts": ts, "detail": "AWS creds not set — logged only"}
        return last_send

    try:
        import boto3
        client = boto3.client("sns", region_name=AWS_REGION)
        resp = client.publish(
            PhoneNumber=phone,
            Message=message,
            MessageAttributes={
                "AWS.SNS.SMS.SMSType": {"DataType": "String", "StringValue": "Transactional"},
            },
        )
        mid = resp.get("MessageId", "")
        _safe_print(f"[sms] Sent to {phone} (MessageId={mid})")
        last_send = {"status": "sent", "to": phone, "ts": ts, "detail": mid}
        return last_send
    except Exception as e:
        print(f"[sms] SNS error: {e}")
        last_send = {"status": "error", "to": phone, "ts": ts, "detail": str(e)}
        return last_send


def compose_patient_message(bridge: dict, donor: dict, date: str) -> str:
    """
    Patient-facing 'donor confirmed' text with the donor's details.
    (The patient may see donor details; the data-minimization firewall is between
     the donor's Proxy and the *Guardian/NGO*, not the patient they're helping.)
    """
    alias = donor.get("alias") or f"Donor-{str(donor.get('user_id',''))[:6]}"
    bg = donor.get("blood_group") or bridge.get("bridge_blood_group") or "compatible"
    dist = donor.get("distance_km")
    dist_s = f"{dist} km away" if dist is not None else "nearby"
    donations = donor.get("donations_till_date")
    when = (date or "")[:10]
    lines = [
        "🩸 RAKTA-SETU — Donor Confirmed!",
        f"A donor is confirmed for your {bridge.get('bridge_blood_group','')} transfusion on {when}.",
        "",
        f"Donor: {alias}",
        f"Blood: {bg} (compatible)",
        f"Distance: {dist_s}",
    ]
    if donations is not None:
        lines.append(f"Lifetime donations: {donations}")
    lines += ["", "No calls needed. Your Guardian agent handled it. — Blood Warriors"]
    return "\n".join(lines)
