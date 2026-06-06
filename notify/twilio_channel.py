"""
Interactive, can't-ignore donor alert via Twilio (spec §5.4 escalation ladder).

Flow when a negotiation needs a donor to commit:
  1. WhatsApp message to the donor's phone asking Confirm / Decline (they reply
     YES/NO or tap a quick-reply button).
  2. If no answer within ESCALATE_AFTER seconds, the donor's phone RINGS — a voice
     call reads the request and collects "press 1 to confirm, 2 to decline."
  3. Either response feeds straight back into the live negotiation (human_confirm),
     so the patient then gets their confirmation.

Two-way needs a PUBLIC webhook (Twilio must reach your machine). Run a tunnel
(ngrok / cloudflared) to port 8000 and set PUBLIC_BASE_URL. Config can come from
env vars or be set live from the UI:
  TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
  TWILIO_WHATSAPP_FROM (default sandbox 'whatsapp:+14155238886'),
  TWILIO_CALL_FROM (a Twilio voice number, for the escalation call),
  PUBLIC_BASE_URL (https tunnel to this server), DONOR_PHONE (E.164, +91…).
"""
import os
import threading
from datetime import datetime
from typing import Optional, Dict

import json
from pathlib import Path

ESCALATE_AFTER = int(os.environ.get("ESCALATE_AFTER_SECS", "20"))
_CFG_FILE = Path(__file__).parent.parent / "data" / "twilio_cfg.json"
_PHONES_FILE = Path(__file__).parent.parent / "data" / "live_phones.json"

cfg = {
    "account_sid":   os.environ.get("TWILIO_ACCOUNT_SID", ""),
    "auth_token":    os.environ.get("TWILIO_AUTH_TOKEN", ""),
    "whatsapp_from": os.environ.get("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886"),
    "call_from":     os.environ.get("TWILIO_CALL_FROM", ""),
    "public_base":   os.environ.get("PUBLIC_BASE_URL", ""),
    "donor_phone":   os.environ.get("DONOR_PHONE", ""),
    "patient_phone": os.environ.get("PATIENT_PHONE", "+917416470528"),
    "enabled":       False,
    "call_mode":     os.environ.get("CALL_MODE", "conversational"),  # 'conversational' | 'dtmf'
    "call_language": os.environ.get("CALL_LANGUAGE", "te"),          # te/hi/en/kn/ta
}

# Load persisted config if it exists (survives restarts)
if _CFG_FILE.exists():
    try:
        _saved = json.loads(_CFG_FILE.read_text(encoding="utf-8"))
        for k, v in _saved.items():
            if k in cfg and v:
                cfg[k] = v
    except Exception:
        pass

# phone (E.164, no 'whatsapp:' prefix) -> {neg_id, user_id, ts, escalated}
pending: Dict[str, dict] = {}
last_event = {"status": "none", "detail": None, "ts": None}


def _p(s: str):
    """Console print that won't crash on emoji on a cp1252 (Windows) terminal."""
    try:
        print(s)
    except UnicodeEncodeError:
        print(s.encode("ascii", "replace").decode("ascii"))


def set_config(**kw):
    for k, v in kw.items():
        if k not in cfg or v is None:
            continue
        # Never let a blank field wipe an existing secret (the UI redacts SID/token,
        # so it posts empty strings on save) — only update with real values.
        if isinstance(v, str):
            v = v.strip()
            if v == "" and k in ("account_sid", "auth_token", "call_from", "whatsapp_from"):
                continue
        cfg[k] = v
    # Persist so restarts don't wipe the config
    try:
        _CFG_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CFG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass
    return public_config()


def public_config() -> dict:
    """Config safe to show in the UI (auth token redacted)."""
    return {
        "enabled":       cfg["enabled"],
        "configured":    bool(cfg["account_sid"] and cfg["auth_token"]),
        "whatsapp_from": cfg["whatsapp_from"],
        "call_from":     cfg["call_from"],
        "public_base":   cfg["public_base"],
        "donor_phone":   cfg["donor_phone"],
        "patient_phone": cfg["patient_phone"],
        "call_mode":     cfg["call_mode"],
        "call_language": cfg["call_language"],
        "escalate_after": ESCALATE_AFTER,
        "last_event":    last_event,
    }


def get_patient_phone() -> str:
    return cfg.get("patient_phone") or "+917416470528"


def autodetect_ngrok() -> dict:
    """Read ngrok's local API for the active public https URL and set public_base —
    so you never have to copy/paste the rotating ngrok URL again."""
    import urllib.request
    try:
        raw = urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=3).read()
        tunnels = json.loads(raw).get("tunnels", [])
        url = next((t["public_url"] for t in tunnels if t.get("public_url", "").startswith("https")), None)
        if not url:
            return {"ok": False, "reason": "no https tunnel found (is ngrok running?)"}
        set_config(public_base=url)
        sync_inbound_webhook()
        return {"ok": True, "public_base": url}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


def _client():
    from twilio.rest import Client
    return Client(cfg["account_sid"], cfg["auth_token"])


def sync_inbound_webhook() -> dict:
    """Point the Twilio number's inbound-SMS webhook at our current public URL,
    so donor replies (YES/NO) reach the app. Safe no-op if not fully configured."""
    base = cfg.get("public_base", "").strip().rstrip("/")
    num = cfg.get("call_from", "").strip()
    if not (cfg.get("account_sid") and cfg.get("auth_token") and base and num):
        return {"synced": False, "reason": "incomplete config"}
    try:
        c = _client()
        found = c.incoming_phone_numbers.list(phone_number=num)
        if not found:
            return {"synced": False, "reason": f"{num} not in account"}
        url = f"{base}/twilio/whatsapp/inbound"
        c.incoming_phone_numbers(found[0].sid).update(sms_url=url, sms_method="POST")
        _p(f"[twilio] inbound SMS webhook -> {url}")
        return {"synced": True, "sms_url": url}
    except Exception as e:
        return {"synced": False, "reason": str(e)}


def _norm(p: str) -> str:
    return (p or "").replace("whatsapp:", "").strip()


# ──────────────────────────────────────────────
# OUTBOUND: Real SMS confirm request
# ──────────────────────────────────────────────

_used_phones = {}  # Map user_id to phone number


def load_live_phones() -> list:
    """The 'sensitive file' of real numbers (data/live_phones.json). Add numbers
    there (or via the UI) and they'll receive the SMS + escalation call too."""
    if not _PHONES_FILE.exists():
        return [p for p in [cfg.get("donor_phone")] if p]
    try:
        phones = json.loads(_PHONES_FILE.read_text(encoding="utf-8"))
        return [p for p in phones if p]
    except Exception:
        return [p for p in [cfg.get("donor_phone")] if p]


def get_next_live_phone(user_id: str) -> str:
    """Assigns the next available live phone number to a donor proxy."""
    if user_id in _used_phones:
        return _used_phones[user_id]
    
    if not _PHONES_FILE.exists():
        return cfg["donor_phone"]
        
    try:
        phones = json.loads(_PHONES_FILE.read_text(encoding="utf-8"))
        if not phones:
            return cfg["donor_phone"]
        # Take the phone based on how many we've assigned
        idx = len(_used_phones) % len(phones)
        phone = phones[idx]
        _used_phones[user_id] = phone
        return phone
    except Exception:
        return cfg["donor_phone"]


def send_whatsapp_confirm(neg_id: str, user_id: str, phone: str, body: str, call_ctx: dict = None) -> dict:
    """Send the SMS Confirm/Decline ask and register the pending response.
    call_ctx (bg/hospital/date/lang) lets the escalation be a Bedrock voice chat."""
    global last_event

    # Use real phone mapping instead of the single UI phone
    real_phone = get_next_live_phone(user_id)
    phone = _norm(real_phone)

    pending[phone] = {"neg_id": neg_id, "user_id": user_id, "ctx": call_ctx or {},
                      "ts": datetime.utcnow().isoformat(), "escalated": False}

    # ── 20-second escalation timer ──────────────────────────────────────────
    def _escalate_if_unanswered():
        import time
        # Honour the live donor-contact policy (enable + delay).
        try:
            from data.policy import get_policy
            pol = get_policy()
            if not pol.get("enable_call", True):
                return                      # policy says: SMS only, never call
            delay = int(pol.get("escalate_after", ESCALATE_AFTER))
        except Exception:
            delay = ESCALATE_AFTER
        time.sleep(delay)
        rec = pending.get(phone)
        if not rec or rec.get("escalated"):
            return
        # Don't ring if the donor already answered through ANY channel (SMS reply,
        # the in-app phone sim, or the console Confirm button).
        try:
            from agents.orchestrator import _pending_confirms
            fut = _pending_confirms.get(neg_id, {}).get(user_id)
            if fut is not None and fut.done():
                return
        except Exception:
            pass
        rec["escalated"] = True
        _p(f"[twilio] No reply in {delay}s — escalating to call -> {phone}")
        if cfg.get("call_mode") == "conversational":
            # Real Bedrock-powered spoken conversation in the donor's language.
            from notify import voice_agent
            voice_agent.place_conversational_call(neg_id, user_id, phone, rec.get("ctx") or {})
        else:
            prompt = ("A patient urgently needs blood. Your Proxy agent is requesting your "
                      "confirmation. Press 1 to confirm your donation. Press 2 to decline.")
            place_escalation_call(neg_id, user_id, phone, prompt)

    t = threading.Thread(target=_escalate_if_unanswered, daemon=True)
    t.start()
    # ────────────────────────────────────────────────────────────────────────

    if not (cfg["account_sid"] and cfg["auth_token"] and cfg["call_from"]):
        _p(f"[twilio:MOCK SMS] -> {phone}\n{body}")
        last_event = {"status": "mock", "detail": f"SMS to {phone}", "ts": datetime.utcnow().isoformat()}
        return last_event
    try:
        # Standard SMS instead of WhatsApp
        msg = _client().messages.create(
            from_=cfg["call_from"], to=phone, body=body)
        _p(f"[twilio] REAL SMS sent to {phone} (sid={msg.sid})")
        last_event = {"status": "sms_sent", "detail": msg.sid, "ts": datetime.utcnow().isoformat()}
    except Exception as e:
        _p(f"[twilio] SMS error: {e}")
        last_event = {"status": "error", "detail": str(e), "ts": datetime.utcnow().isoformat()}
    return last_event


# ──────────────────────────────────────────────
# ONE-WAY SMS (patient "donor confirmed" notification)
# ──────────────────────────────────────────────
_notified_patients = set()   # neg_ids already texted, so the patient gets ONE SMS


def notify_patient_once(neg_id: str, phone: str, body: str) -> dict:
    """Send the patient SMS exactly once per negotiation (call + lock paths share this)."""
    if neg_id in _notified_patients:
        return {"status": "duplicate", "to": phone}
    _notified_patients.add(neg_id)
    return send_plain_sms(phone, body)


def send_plain_sms(phone: str, body: str) -> dict:
    """Fire-and-forget SMS (no pending/escalation) — used for the patient notice."""
    phone = _norm(phone)
    if not (cfg.get("account_sid") and cfg.get("auth_token") and cfg.get("call_from")):
        _p(f"[twilio:MOCK plain-sms] -> {phone}\n{body}")
        return {"status": "mock", "to": phone}
    try:
        msg = _client().messages.create(from_=cfg["call_from"], to=phone, body=body)
        _p(f"[twilio] patient SMS sent to {phone} (sid={msg.sid})")
        return {"status": "sent", "to": phone, "detail": msg.sid}
    except Exception as e:
        _p(f"[twilio] patient SMS error: {e}")
        return {"status": "error", "to": phone, "detail": str(e)}


# ──────────────────────────────────────────────
# ESCALATION: phone call (rings — can't ignore)
# ──────────────────────────────────────────────
def place_escalation_call(neg_id: str, user_id: str, phone: str, prompt: str) -> dict:
    """Ring the donor with an IVR: press 1 to confirm, 2 to decline."""
    global last_event
    phone = _norm(phone)
    twiml = voice_twiml(neg_id, user_id, prompt)
    if not (cfg["account_sid"] and cfg["auth_token"] and cfg["call_from"]):
        _p(f"[twilio:MOCK call] -> {phone} (would ring): {prompt}")
        last_event = {"status": "mock_call", "detail": f"call to {phone}", "ts": datetime.utcnow().isoformat()}
        return last_event
    try:
        call = _client().calls.create(from_=cfg["call_from"], to=phone, twiml=twiml)
        _p(f"[twilio] Escalation call placed to {phone} (sid={call.sid})")
        last_event = {"status": "call_placed", "detail": call.sid, "ts": datetime.utcnow().isoformat()}
    except Exception as e:
        _p(f"[twilio] Call error: {e}")
        last_event = {"status": "error", "detail": str(e), "ts": datetime.utcnow().isoformat()}
    return last_event


def voice_twiml(neg_id: str, user_id: str, prompt: str) -> str:
    """TwiML for the escalation call: speak the ask, gather a digit, post it back."""
    from urllib.parse import quote
    base = cfg["public_base"].strip().rstrip("/")
    # Donor user_ids contain '\x..' — must be URL-encoded or Twilio's webhook POST
    # 404s and the call dies with "an application error has occurred".
    action = f"{base}/twilio/voice/gather/{quote(neg_id, safe='')}/{quote(user_id, safe='')}"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response>'
        f'<Gather numDigits="1" action="{action}" method="POST" timeout="15">'
        f'<Say voice="Polly.Aditi">{_xml(prompt)} '
        'Press 1 to confirm. Press 2 to decline.</Say>'
        '</Gather>'
        '<Say>No response received. Goodbye.</Say>'
        '</Response>'
    )


def _xml(s: str) -> str:
    return (s or "").replace("&", "and").replace("<", "").replace(">", "")


# ──────────────────────────────────────────────
# INBOUND: resolve a pending ask -> feed the negotiation
# ──────────────────────────────────────────────
def _decide(text: str) -> Optional[bool]:
    t = (text or "").strip().lower()
    if t in ("yes", "y", "1", "confirm", "confirmed", "ok", "okay", "👍", "✅"):
        return True
    if t in ("no", "n", "2", "decline", "declined", "cancel", "👎", "❌"):
        return False
    return None


def handle_inbound(from_phone: str, body: str, button: str = "") -> dict:
    """Twilio WhatsApp inbound -> map to a pending ask -> human_confirm."""
    phone = _norm(from_phone)
    decision = _decide(button or body)
    rec = pending.get(phone)
    if not rec:
        return {"matched": False, "decision": decision}
    if decision is None:
        return {"matched": True, "decision": None, "neg_id": rec["neg_id"]}
    from agents.orchestrator import human_confirm
    human_confirm(rec["neg_id"], rec["user_id"], decision)
    pending.pop(phone, None)
    return {"matched": True, "decision": decision, "neg_id": rec["neg_id"], "user_id": rec["user_id"]}


def handle_gather(neg_id: str, user_id: str, digits: str) -> dict:
    decision = _decide(digits)
    if decision is None:
        return {"decision": None}
    from agents.orchestrator import human_confirm
    human_confirm(neg_id, user_id, decision)
    # clear any pending entry for this user
    for p, rec in list(pending.items()):
        if rec.get("neg_id") == neg_id and rec.get("user_id") == user_id:
            pending.pop(p, None)
    return {"decision": decision}


def clear_pending(neg_id: str, user_id: str):
    for p, rec in list(pending.items()):
        if rec.get("neg_id") == neg_id and rec.get("user_id") == user_id:
            pending.pop(p, None)
