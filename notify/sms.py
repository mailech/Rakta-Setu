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
    """Detect creds via the FULL boto3 chain (env vars / ~/.aws / IAM role),
    not just env vars — App Runner / EC2 use an instance role with no env keys."""
    try:
        import boto3
        return boto3.Session().get_credentials() is not None
    except Exception:
        return False


def _safe_print(s: str):
    """Print without crashing on emoji on a cp1252 (Windows) console."""
    try:
        print(s)
    except UnicodeEncodeError:
        print(s.encode("ascii", "replace").decode("ascii"))


def _norm(phone: str) -> str:
    """Best-effort E.164 normalization (India default). Mirrors twilio_channel._norm."""
    p = (phone or "").strip().replace(" ", "").replace("-", "")
    if not p:
        return ""
    if p.startswith("+"):
        return p
    digits = "".join(ch for ch in p if ch.isdigit())
    if len(digits) == 10:          # bare Indian mobile
        return "+91" + digits
    return "+" + digits


def _sns():
    import boto3
    return boto3.client("sns", region_name=AWS_REGION)


# ── SNS SMS sandbox awareness ───────────────────────────────────────────────
# In the SNS sandbox, publish() returns a MessageId (looks "sent") for ANY number
# but AWS silently DROPS delivery to numbers that aren't verified. We cache the
# sandbox status + verified set so we can tell the truth instead of faking "sent".
_sandbox = {"checked": False, "in_sandbox": False, "verified": set()}


def sandbox_state(force: bool = False) -> dict:
    """Return {'in_sandbox': bool, 'verified': set(E.164)}. Cached; force=True refreshes."""
    if _sandbox["checked"] and not force:
        return _sandbox
    try:
        c = _sns()
        _sandbox["in_sandbox"] = bool(c.get_sms_sandbox_account_status().get("IsInSandbox", False))
        verified = set()
        if _sandbox["in_sandbox"]:
            token = None
            while True:
                kw = {"NextToken": token} if token else {}
                resp = c.list_sms_sandbox_phone_numbers(**kw)
                for n in resp.get("PhoneNumbers", []):
                    if n.get("Status") == "Verified":
                        verified.add(n["PhoneNumber"])
                token = resp.get("NextToken")
                if not token:
                    break
        _sandbox["verified"] = verified
        _sandbox["checked"] = True
    except Exception as e:
        _safe_print(f"[sms] sandbox-status check failed (assuming production): {e}")
        _sandbox["checked"] = True   # don't hammer the API on repeated failures
    return _sandbox


def verify_number(phone: str) -> dict:
    """Start sandbox verification for a number — AWS texts it a one-time code.
    Follow up with confirm_verification(phone, otp)."""
    phone = _norm(phone)
    try:
        _sns().create_sms_sandbox_phone_number(PhoneNumber=phone, LanguageCode="en-US")
        _safe_print(f"[sms] verification code sent to {phone}")
        return {"status": "otp_sent", "to": phone}
    except Exception as e:
        _safe_print(f"[sms] verify_number error for {phone}: {e}")
        return {"status": "error", "to": phone, "detail": str(e)}


def confirm_verification(phone: str, otp: str) -> dict:
    """Finish sandbox verification with the code AWS texted to the number."""
    phone = _norm(phone)
    try:
        _sns().verify_sms_sandbox_phone_number(PhoneNumber=phone, OneTimePassword=str(otp).strip())
        sandbox_state(force=True)   # refresh the verified cache
        _safe_print(f"[sms] {phone} is now VERIFIED")
        return {"status": "verified", "to": phone}
    except Exception as e:
        _safe_print(f"[sms] confirm_verification error for {phone}: {e}")
        return {"status": "error", "to": phone, "detail": str(e)}


def send_sms(phone: str, message: str) -> dict:
    """
    Publish an SMS to a single phone number (E.164, e.g. +9198XXXXXXXX) via SNS.
    Returns {status, to, detail}. status is one of:
      sent | unverified (sandbox, won't deliver) | mock (no creds) | skipped | error
    """
    global last_send
    ts = datetime.utcnow().isoformat()
    phone = _norm(phone)
    if not phone:
        last_send = {"status": "skipped", "to": None, "ts": ts, "detail": "no phone set"}
        return last_send

    if not _have_aws():
        # Graceful mock — log to console so the demo flow still completes.
        _safe_print(f"[sns:MOCK] AWS creds missing, skipping real SMS to {phone}")
        last_send = {"status": "mock", "to": phone, "ts": ts, "detail": "AWS creds missing"}
        return last_send

    # Sandbox guard: don't pretend a silently-dropped message was delivered.
    sb = sandbox_state()
    if sb["in_sandbox"] and phone not in sb["verified"]:
        detail = (f"SNS sandbox: {phone} is NOT verified, so AWS will NOT deliver this SMS. "
                  f"Verify it (AWS console -> SNS -> Text messaging -> Sandbox, or sms.verify_number) "
                  f"or move the account to SNS production.")
        _safe_print(f"[sms] BLOCKED — {detail}")
        last_send = {"status": "unverified", "to": phone, "ts": ts, "detail": detail}
        return last_send

    try:
        resp = _sns().publish(
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
