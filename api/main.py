"""
RAKTA-SETU FastAPI Backend
REST + WebSocket for Negotiation Console, Bridge Board, Phone Simulator.
"""
import asyncio
import json
import sys
from pathlib import Path
from typing import List, Optional
from datetime import datetime

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from agents.orchestrator import (
    init_db, run_negotiation, set_broadcast_fn,
    human_confirm, human_decline, get_neg, get_event_log, DB_PATH
)
from agents.exchange import ExchangeAgent
from llm.wrapper import llm

import sqlite3

# ──────────────────────────────────────────────
# APP SETUP
# ──────────────────────────────────────────────

app = FastAPI(title="RAKTA-SETU API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ──────────────────────────────────────────────
# DATA LOADING (lazy, cached on first request)
# ──────────────────────────────────────────────

_donors: Optional[List[dict]] = None
_bridges: Optional[List[dict]] = None
_config: Optional[dict] = None
_exchange: Optional[ExchangeAgent] = None
_donors_by_id: Optional[dict] = None


def get_data():
    global _donors, _bridges, _config, _exchange, _donors_by_id
    if _donors is None:
        _donors  = json.loads((ROOT / "data" / "donors.json").read_text(encoding="utf-8"))
        _bridges = json.loads((ROOT / "data" / "bridges.json").read_text(encoding="utf-8"))
        _config  = json.loads((ROOT / "data" / "config.json").read_text(encoding="utf-8"))
        _donors_by_id = {d["user_id"]: d for d in _donors}
        _exchange = ExchangeAgent(_donors, _bridges, _config)
    return _donors, _bridges, _config, _exchange, _donors_by_id


# ──────────────────────────────────────────────
# WEBSOCKET MANAGER
# ──────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self.connections: dict = {}  # neg_id -> list of WebSocket

    async def connect(self, neg_id: str, ws: WebSocket):
        await ws.accept()
        self.connections.setdefault(neg_id, []).append(ws)

    def disconnect(self, neg_id: str, ws: WebSocket):
        if neg_id in self.connections:
            self.connections[neg_id].discard(ws) if hasattr(self.connections[neg_id], 'discard') else None
            try:
                self.connections[neg_id].remove(ws)
            except ValueError:
                pass

    async def broadcast(self, neg_id: str, data: dict):
        dead = []
        for ws in self.connections.get(neg_id, []):
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(neg_id, ws)

    async def broadcast_all(self, data: dict):
        for neg_id in list(self.connections.keys()):
            await self.broadcast(neg_id, data)


ws_manager = ConnectionManager()


def global_broadcast(msg: dict):
    """
    Single broadcast fn for the orchestrator. Routes each message to its own
    negotiation room (by neg_id) AND the '__all__' firehose, so multiple
    negotiations can stream concurrently (spec §5.5).
    """
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        return
    if not loop.is_running():
        return
    neg_id = msg.get("neg_id")
    if neg_id:
        asyncio.ensure_future(ws_manager.broadcast(neg_id, msg))
    asyncio.ensure_future(ws_manager.broadcast("__all__", msg))


# ──────────────────────────────────────────────
# STARTUP
# ──────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    init_db()
    get_data()
    set_broadcast_fn(global_broadcast)  # one router for all negotiations
    try:
        from notify import twilio_channel as _twi
        r = _twi.autodetect_ngrok()
        if r.get("ok"):
            print(f"[api] auto-detected ngrok: {r['public_base']}")
    except Exception:
        pass
    print("[api] RAKTA-SETU API started. DB initialized.")


# ──────────────────────────────────────────────
# WEBSOCKET — Live negotiation feed
# ──────────────────────────────────────────────

@app.websocket("/ws/negotiation/{neg_id}")
async def ws_negotiation(websocket: WebSocket, neg_id: str):
    await ws_manager.connect(neg_id, websocket)
    # Send existing log on connect
    events = get_event_log(neg_id)
    for e in events:
        await websocket.send_json(e)
    try:
        while True:
            await websocket.receive_text()  # keep alive
    except WebSocketDisconnect:
        ws_manager.disconnect(neg_id, websocket)


@app.websocket("/ws/all")
async def ws_all(websocket: WebSocket):
    """Subscribe to all negotiation events."""
    await ws_manager.connect("__all__", websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect("__all__", websocket)


# ──────────────────────────────────────────────
# NEGOTIATION ENDPOINTS
# ──────────────────────────────────────────────

class TriggerRequest(BaseModel):
    bridge_index: Optional[int] = 0
    bridge_id: Optional[str] = None
    use_llm: bool = False


@app.post("/negotiations/trigger")
async def trigger_negotiation(req: TriggerRequest, background_tasks: BackgroundTasks):
    """Start a negotiation for a bridge."""
    _, bridges, config, exchange, donors_by_id = get_data()

    # Find bridge
    bridge = None
    if req.bridge_id:
        for b in bridges:
            if req.bridge_id in b["bridge_id"]:
                bridge = b
                break
    if not bridge and req.bridge_index is not None:
        if 0 <= req.bridge_index < len(bridges):
            bridge = bridges[req.bridge_index]

    if not bridge:
        raise HTTPException(404, "Bridge not found")

    llm_fn = llm if req.use_llm else None

    # Mint the neg_id here so the client can subscribe to the matching WS room.
    safe_bridge_id = bridge['bridge_id'].replace("/", "").replace("\\", "")[:8]
    neg_id = f"NEG-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{safe_bridge_id}"

    async def _run():
        result = await run_negotiation(bridge, donors_by_id, exchange, config, llm_fn, neg_id=neg_id)
        await ws_manager.broadcast(neg_id, {"type": "negotiation_complete", **result})
        await ws_manager.broadcast("__all__", {"type": "negotiation_complete", **result})

    background_tasks.add_task(_run)

    return {
        "neg_id":    neg_id,
        "bridge_id": bridge["bridge_id"],
        "bridge_blood_group": bridge.get("bridge_blood_group"),
        "units_needed": bridge.get("quantity_required"),
        "next_transfusion_date": bridge.get("next_transfusion_date"),
        "status": "started"
    }


# ──────────────────────────────────────────────
# PATIENT INTAKE → 10km radius → (emergency) broadcast
# ──────────────────────────────────────────────

def _hospitals():
    p = ROOT / "data" / "hospitals.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


@app.get("/hospitals")
async def list_hospitals():
    return {"hospitals": _hospitals()}


@app.get("/live-phones")
async def get_live_phones():
    from notify import twilio_channel as twi
    return {"phones": twi.load_live_phones()}


class LivePhone(BaseModel):
    phone: str


@app.post("/live-phones")
async def add_live_phone(req: LivePhone):
    """Add a real number to the sensitive file — it'll then get SMS + escalation call."""
    p = ROOT / "data" / "live_phones.json"
    phones = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    if req.phone and req.phone not in phones:
        phones.append(req.phone.strip())
        p.write_text(json.dumps(phones, indent=2), encoding="utf-8")
    return {"phones": phones}


class IntakeRequest(BaseModel):
    blood_group: str                      # e.g. "B+"
    hospital_id: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    units: int = 1
    date: Optional[str] = None
    time: Optional[str] = None
    days: Optional[int] = None            # needed in N days
    emergency: bool = False
    radius_km: float = 10.0


@app.post("/intake")
async def patient_intake(req: IntakeRequest, background_tasks: BackgroundTasks):
    """
    Patient describes their need; we find every compatible donor within radius_km
    of the hospital and open a negotiation (emergency = alert them all at once).
    """
    from data.profiles import donors_within_radius, normalize_bg
    donors, bridges, config, exchange, donors_by_id = get_data()

    # Resolve hospital location
    hosp_name, hosp_city = "the hospital", ""
    lat, lon = req.lat, req.lon
    if req.hospital_id:
        h = next((x for x in _hospitals() if x["id"] == req.hospital_id), None)
        if h:
            lat, lon, hosp_name, hosp_city = h["lat"], h["lon"], h["name"], h.get("city", "")
    if lat is None or lon is None:
        raise HTTPException(400, "Provide hospital_id or lat/lon")

    bg = normalize_bg(req.blood_group) or req.blood_group

    # Date: explicit > SIM_TODAY + days > SIM_TODAY + 7
    sim_today = (config.get("SIM_TODAY") or "2025-08-08")[:10]
    if req.date:
        date = req.date[:10]
    else:
        from datetime import timedelta
        base = datetime.fromisoformat(sim_today)
        date = (base + timedelta(days=req.days if req.days is not None else 7)).date().isoformat()

    from data import policy as donor_policy
    pol = donor_policy.get_policy()
    radius = req.radius_km or pol["max_distance_km"]
    matched = donors_within_radius(donors, lat, lon, radius, bg,
                                   require_eligible=pol["only_eligible"], emergency=req.emergency)
    # Apply the donor-contact CONDITIONS (fatigue / propensity) — the agent rules.
    matched = [m for m in matched if donor_policy.passes(m, emergency=req.emergency)]
    cap = 40 if req.emergency else 10
    roster = [{k: v for k, v in m.items() if k != "_rank"} for m in matched[:cap]]

    if not roster:
        raise HTTPException(404, f"No compatible {bg} donors within {radius} km of {hosp_name} (after applying donor conditions)")

    safe = (req.hospital_id or "loc")[:8]
    neg_id = f"REQ-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{safe}"
    bridge = {
        "bridge_id": neg_id, "bridge_blood_group": bg, "quantity_required": req.units,
        "next_transfusion_date": date, "req_time": req.time or "",
        "roster": roster, "roster_size": len(roster),
        "centroid_lat": lat, "centroid_lon": lon,
        "hospital_name": hosp_name, "hospital_city": hosp_city,
        "emergency": req.emergency, "health_label": "green",
        "days_to_next_transfusion": req.days if req.days is not None else 7,
    }

    async def _run():
        result = await run_negotiation(bridge, donors_by_id, exchange, config, None, neg_id=neg_id)
        await ws_manager.broadcast(neg_id, {"type": "negotiation_complete", **result})
        await ws_manager.broadcast("__all__", {"type": "negotiation_complete", **result})
    background_tasks.add_task(_run)

    # Candidate preview (with mock profiles) for the UI
    preview = [{
        "user_id": m["user_id"], "distance_km": m["distance_km"],
        "eligible_now": m.get("eligible_now"), "propensity": m.get("propensity"),
        **{k: m["profile"][k] for k in ("name", "age", "blood_group")},
    } for m in roster[:12]]

    return {
        "neg_id": neg_id, "blood_group": bg, "hospital": hosp_name, "city": hosp_city,
        "date": date, "time": req.time, "units": req.units, "emergency": req.emergency,
        "radius_km": radius, "matched": len(matched), "contacting": len(roster),
        "candidates": preview, "status": "started",
    }


class ConfirmRequest(BaseModel):
    user_id: str
    confirmed: bool = True


@app.post("/negotiations/{neg_id}/confirm")
async def confirm_donation(neg_id: str, req: ConfirmRequest):
    """Human taps Confirm on phone simulator."""
    human_confirm(neg_id, req.user_id, True)
    await ws_manager.broadcast(neg_id, {
        "type": "human_confirmed", "neg_id": neg_id, "user_id": req.user_id
    })
    return {"status": "confirmed", "neg_id": neg_id, "user_id": req.user_id}


@app.post("/negotiations/{neg_id}/decline")
async def decline_donation(neg_id: str, req: ConfirmRequest):
    """Human taps Decline — triggers re-planning."""
    human_decline(neg_id, req.user_id)
    await ws_manager.broadcast(neg_id, {
        "type": "human_declined", "neg_id": neg_id, "user_id": req.user_id,
        "say": "Donor declined — Guardian is re-planning..."
    })
    return {"status": "declined", "neg_id": neg_id, "user_id": req.user_id}


@app.get("/negotiations/{neg_id}")
async def get_negotiation(neg_id: str):
    neg = get_neg(neg_id)
    if not neg:
        raise HTTPException(404, "Negotiation not found")
    events = get_event_log(neg_id)
    return {"negotiation": neg, "events": events}


@app.get("/negotiations")
async def list_negotiations():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM negotiations ORDER BY started_at DESC LIMIT 50"
    ).fetchall()
    conn.close()
    return {"negotiations": [dict(r) for r in rows]}


# ──────────────────────────────────────────────
# BRIDGE BOARD
# ──────────────────────────────────────────────

@app.get("/bridges")
async def list_bridges():
    _, bridges, _, _, _ = get_data()
    # Add negotiation history counts
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    for b in bridges:
        rows = conn.execute(
            "SELECT result, COUNT(*) as cnt FROM negotiations WHERE bridge_id=? GROUP BY result",
            (b["bridge_id"],)
        ).fetchall()
        b["negotiation_stats"] = {r["result"]: r["cnt"] for r in rows}
    conn.close()
    return {"bridges": bridges, "total": len(bridges)}


@app.get("/bridges/{bridge_id}")
async def get_bridge(bridge_id: str):
    _, bridges, _, _, _ = get_data()
    for b in bridges:
        if bridge_id in b["bridge_id"]:
            return b
    raise HTTPException(404, "Bridge not found")


# ──────────────────────────────────────────────
# DONOR / PROXY
# ──────────────────────────────────────────────

@app.get("/donors/{user_id}")
async def get_donor(user_id: str):
    _, _, _, _, donors_by_id = get_data()
    donor = donors_by_id.get(user_id)
    if not donor:
        raise HTTPException(404, "Donor not found")
    # Strip blood group from response (data minimization)
    safe = {k: v for k, v in donor.items() if k != "blood_group"}
    return safe


class ConsentUpdate(BaseModel):
    revoked: bool


@app.post("/donors/{user_id}/consent")
async def update_consent(user_id: str, req: ConsentUpdate):
    """Consent revoke toggle from Phone Simulator."""
    _, _, _, _, donors_by_id = get_data()
    donor = donors_by_id.get(user_id)
    if not donor:
        raise HTTPException(404, "Donor not found")
    donor.setdefault("consent", {})["revoked"] = req.revoked
    from agents.orchestrator import log_consent
    log_consent(user_id, "revoked" if req.revoked else "granted", "availability_negotiation")
    return {"user_id": user_id, "consent_revoked": req.revoked}


# ──────────────────────────────────────────────
# STATS (for dashboard top strip)
# ──────────────────────────────────────────────

@app.get("/stats")
async def get_stats():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    total_negs = conn.execute("SELECT COUNT(*) FROM negotiations").fetchone()[0]
    covered    = conn.execute("SELECT COUNT(*) FROM negotiations WHERE result='COVERED'").fetchone()[0]
    confirm_msgs = conn.execute(
        "SELECT COUNT(*) FROM event_log WHERE action='REQUEST_HUMAN_CONFIRM'"
    ).fetchone()[0]
    conn.close()

    _, bridges, _, _, _ = get_data()
    legacy_calls_saved = total_negs * 23 - confirm_msgs  # vs 23 calls per donation

    return {
        "negotiations_run":      total_negs,
        "covered":               covered,
        "human_notifications":   confirm_msgs,
        "legacy_calls_equivalent": total_negs * 23,
        "calls_saved":           max(0, legacy_calls_saved),
        "bridges":               len(bridges),
        "sim_today":             _config.get("SIM_TODAY") if _config else None,
    }


# ──────────────────────────────────────────────
# MODULE A — DEFERRAL GUARD (pre-screen + recovery)
# ──────────────────────────────────────────────

from recovery import pathway as recovery
from analytics import flywheel_projection as flywheel


@app.get("/prescreen/questions")
async def prescreen_questions():
    """The 4-question micro-chat the phone renders before confirmation."""
    return {"questions": recovery.PRESCREEN_QUESTIONS}


class PrescreenRequest(BaseModel):
    neg_id: Optional[str] = None
    user_id: Optional[str] = None
    answers: dict = {}


@app.post("/prescreen")
async def run_prescreen(req: PrescreenRequest):
    """
    Run the WHO/NACO-style pre-screen. On DEFER_LIKELY: enroll in the recovery
    pathway and withdraw the donor from the live negotiation (Guardian sees only
    WITHDRAW; the slot auto-backfills). Raw answers never leave this endpoint.
    """
    result = recovery.prescreen(req.answers or {})
    if result["verdict"] == recovery.DEFER_LIKELY and req.user_id:
        recovery.enroll(req.user_id, verdict=recovery.DEFER_LIKELY)
        if req.neg_id:
            human_decline(req.neg_id, req.user_id)  # triggers live backfill
            await ws_manager.broadcast(req.neg_id, {
                "neg_id": req.neg_id, "from": f"proxy:{req.user_id}", "to": "guardian",
                "action": "WITHDRAW", "round": 3,
                "params": {}, "say": "Withdrawing my donor for now — enrolling them in recovery.",
            })
    return result


@app.get("/recovery")
async def get_recovery():
    """Recovery pipeline summary for the Bridge Board counter + Prevention tab."""
    return recovery.pipeline()


@app.post("/recovery/{user_id}/advance")
async def advance_recovery(user_id: str):
    rec = recovery.advance(user_id)
    if not rec:
        raise HTTPException(404, "Not enrolled in recovery")
    return rec


# ──────────────────────────────────────────────
# MODULE B — PREVENTION FLYWHEEL
# ──────────────────────────────────────────────

# Simple screenings counter (SQLite)
def _ensure_screenings():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS screenings (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, ts TEXT)")
    conn.commit(); conn.close()


class ScreeningOptIn(BaseModel):
    user_id: Optional[str] = None


@app.post("/screening/optin")
async def screening_optin(req: ScreeningOptIn):
    """Donor taps 'yes' to the carrier-test opt-in after a LOCK."""
    _ensure_screenings()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO screenings (user_id, ts) VALUES (?,?)",
                 (req.user_id or "anon", datetime.utcnow().isoformat()))
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM screenings").fetchone()[0]
    conn.close()
    return {"status": "scheduled", "screenings_scheduled": count}


@app.get("/screening/stats")
async def screening_stats():
    _ensure_screenings()
    conn = sqlite3.connect(DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM screenings").fetchone()[0]
    conn.close()
    return {"screenings_scheduled": count}


@app.get("/prevention/projection")
async def prevention_projection(screened_per_year: Optional[int] = None):
    return flywheel.projection(screened_per_year)


@app.get("/prevention/camps")
async def prevention_camps(k: int = 5):
    return flywheel.camp_placements(k)


# ──────────────────────────────────────────────
# CHURN MODEL
# ──────────────────────────────────────────────

@app.get("/churn/meta")
async def churn_meta():
    """AUC + model card for the slide."""
    try:
        from ml.churn import model_meta
        meta = model_meta()
        return meta or {"status": "model not trained — run `python -m ml.churn`"}
    except Exception as e:
        return {"status": f"unavailable: {e}"}


@app.get("/churn/{user_id}")
async def churn_for_donor(user_id: str):
    try:
        from ml.churn import churn_risk
        risk = churn_risk(user_id)
        if risk is None:
            raise HTTPException(404, "Donor not found")
        return {"user_id": user_id, "churn_risk": risk}
    except HTTPException:
        raise
    except Exception as e:
        return {"user_id": user_id, "error": str(e)}


# ──────────────────────────────────────────────
# SMS / PATIENT NOTIFICATION (AWS SNS)
# ──────────────────────────────────────────────

from notify import sms as sms_mod


class PhoneSetting(BaseModel):
    phone: str


@app.post("/settings/phone")
async def set_phone(req: PhoneSetting):
    """Set the patient phone number that receives the real 'donor confirmed' SMS."""
    saved = sms_mod.set_patient_phone(req.phone)
    return {"patient_phone": saved}


@app.get("/settings")
async def get_settings():
    return {
        "patient_phone": sms_mod.get_patient_phone(),
        "aws_region": sms_mod.AWS_REGION,
        "aws_configured": sms_mod._have_aws(),
        "last_sms": sms_mod.last_send,
    }


class TestSms(BaseModel):
    phone: Optional[str] = None
    message: Optional[str] = "RAKTA-SETU test message ✅"


@app.post("/settings/test-sms")
async def test_sms(req: TestSms):
    """Fire a one-off SMS to verify the SNS wiring live on stage."""
    phone = req.phone or sms_mod.get_patient_phone()
    return sms_mod.send_sms(phone, req.message)



# ──────────────────────────────────────────────
# AMAZON S3 — audit trail (proof of AWS usage)
# ──────────────────────────────────────────────

from aws import s3_audit


class AwsConfig(BaseModel):
    bucket: Optional[str] = None
    region: Optional[str] = None
    enabled: Optional[bool] = None


@app.get("/aws/status")
async def aws_status():
    return s3_audit.public_config()


@app.post("/aws/config")
async def aws_config(req: AwsConfig):
    return s3_audit.set_config(**{k: v for k, v in req.dict().items() if v is not None})


@app.post("/aws/test")
async def aws_test():
    """Write a test object to S3 to prove the wiring (shows up in the S3 console)."""
    return s3_audit.upload("TEST-" + datetime.utcnow().strftime("%H%M%S"),
                           {"test": True, "ts": datetime.utcnow().isoformat()})


# ──────────────────────────────────────────────
# DONOR-CONTACT POLICY (the agent rules)
# ──────────────────────────────────────────────

from data import policy as donor_policy_mod


class PolicyConfig(BaseModel):
    max_distance_km: Optional[float] = None
    only_eligible: Optional[bool] = None
    max_fatigue: Optional[float] = None
    min_propensity: Optional[float] = None
    enable_call: Optional[bool] = None
    escalate_after: Optional[int] = None


@app.get("/policy")
async def get_policy():
    return donor_policy_mod.get_policy()


@app.post("/policy")
async def set_policy(req: PolicyConfig):
    return donor_policy_mod.set_policy(**{k: v for k, v in req.dict().items() if v is not None})


# ──────────────────────────────────────────────
# TWILIO — interactive SMS + escalation call (spec §5.4)
# ──────────────────────────────────────────────

from fastapi import Form
from fastapi.responses import PlainTextResponse, Response
from notify import twilio_channel as twi


def _donor_card(user_id: str) -> dict:
    """Full donor profile for the floor's right rail + patient SMS (mock identity)."""
    from data.profiles import profile
    _, _, _, _, donors_by_id = get_data()
    donor = donors_by_id.get(user_id, {"user_id": user_id})
    prof = profile(donor)
    prof["phone"] = twi._used_phones.get(user_id)
    prof["donations"] = donor.get("donations_till_date")
    prof["distance_km"] = donor.get("distance_km")
    return prof


def _notify_patient(user_id: str, neg_id: str):
    """Text the patient ONCE per request when a donor confirms (with donor details)."""
    phone = twi.get_patient_phone()
    if not phone:
        print("[notify] No patient phone set — skipping")
        return
    prof = _donor_card(user_id)
    dist_s = f"{round(prof['distance_km'])} km away" if prof.get("distance_km") is not None else "nearby"
    msg = (
        "RAKTA-SETU: Donor Confirmed!\n"
        f"{prof['name']} ({prof['blood_group']}, {prof['age']}y) will donate for your transfusion.\n"
        f"Contact: {prof.get('phone') or 'shared at the bank'}\n"
        f"Distance: {dist_s}\n"
        "No calls needed — your Guardian agent handled it. - Blood Warriors"
    )
    res = twi.notify_patient_once(neg_id, phone, msg)
    print(f"[notify] patient {res.get('status')} -> {phone}")



class TwilioConfig(BaseModel):
    account_sid: Optional[str] = None
    auth_token: Optional[str] = None
    whatsapp_from: Optional[str] = None
    call_from: Optional[str] = None
    public_base: Optional[str] = None
    donor_phone: Optional[str] = None
    patient_phone: Optional[str] = None
    enabled: Optional[bool] = None


@app.post("/twilio/config")
async def twilio_config(req: TwilioConfig):
    """Set Twilio creds + phones + tunnel URL live from the UI, and keep the
    inbound-SMS webhook pointed at the current public URL."""
    out = twi.set_config(**{k: v for k, v in req.dict().items() if v is not None})
    out["webhook"] = twi.sync_inbound_webhook()
    return out


@app.get("/twilio/config")
async def get_twilio_config():
    return twi.public_config()


@app.post("/twilio/autodetect-ngrok")
async def twilio_autodetect():
    """Auto-fill the public URL from a running ngrok (no copy/paste)."""
    return twi.autodetect_ngrok()


@app.post("/twilio/test")
async def twilio_test(req: TwilioConfig):
    """Send a one-off SMS to the donor phone to confirm the wiring."""
    phone = req.donor_phone or twi.cfg["donor_phone"]
    return twi.send_whatsapp_confirm("NEG-TEST", "test-donor", phone,
        "RAKTA-SETU test. Reply YES to confirm or NO to decline.")


@app.post("/twilio/whatsapp/inbound")
async def twilio_whatsapp_inbound(Body: str = Form(""), From: str = Form(""),
                                  ButtonText: str = Form(""), ButtonPayload: str = Form("")):
    """Webhook: donor's WhatsApp reply/button -> feed into the live negotiation."""
    out = twi.handle_inbound(From, Body, ButtonText or ButtonPayload)
    if out.get("neg_id"):
        uid = out.get("user_id", ""); ok = out.get("decision") is True
        prof = _donor_card(uid) if ok else None
        say = (f"✅ {prof['name']} confirmed via SMS — {prof['blood_group']}, {prof['age']}y, "
               f"{prof.get('phone') or 'contact shared'}") if ok else "Donor declined via SMS."
        await ws_manager.broadcast(out["neg_id"], {
            "neg_id": out["neg_id"], "from": f"proxy:{uid}", "to": "guardian",
            "round": 3, "action": "ACCEPT" if ok else "DECLINE",
            "params": {"channel": "sms"}, "meta": {"profile": prof} if prof else {},
            "say": say})
        if ok:
            _notify_patient(uid, out.get("neg_id", ""))
    reply = ("Thanks - you're confirmed! Your blood is saving a life today." if out.get("decision")
             else "No problem, maybe next time." if out.get("decision") is False
             else "Please reply YES to confirm or NO to decline.")
    return PlainTextResponse(f"<Response><Message>{reply}</Message></Response>", media_type="application/xml")



@app.post("/twilio/voice/gather/{neg_id}/{user_id}")
async def twilio_voice_gather(neg_id: str, user_id: str, Digits: str = Form("")):
    """Webhook: donor's keypad digit on the escalation call -> feed in."""
    try:
        out = twi.handle_gather(neg_id, user_id, Digits)
        confirmed = out.get("decision") is True
        prof = _donor_card(user_id) if confirmed else None
        say = (f"📞✅ {prof['name']} confirmed on the call — {prof['blood_group']}, {prof['age']}y, "
               f"{prof.get('phone') or 'contact shared'}") if confirmed else "Donor declined on the call."
        await ws_manager.broadcast(neg_id, {
            "neg_id": neg_id, "from": f"proxy:{user_id}", "to": "guardian", "round": 3,
            "action": "ACCEPT" if confirmed else "DECLINE",
            "params": {"channel": "voice_call"}, "meta": {"profile": prof} if prof else {},
            "say": say})
        if confirmed:
            _notify_patient(user_id, neg_id)
        spoken = ("Confirmed. Thank you for saving a life. Goodbye."
                  if confirmed else "Understood. No problem. Goodbye.")
        return Response(
            content=f'<Response><Say voice="Polly.Aditi">{spoken}</Say></Response>',
            media_type="application/xml")
    except Exception as e:
        print(f"[voice/gather] error: {e}")
        return Response(
            content='<Response><Say>Thank you. Goodbye.</Say></Response>',
            media_type="application/xml")



# ──────────────────────────────────────────────
# HEALTH
# ──────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "RAKTA-SETU", "version": "1.0.0"}


# ──────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.main:app", host="0.0.0.0", port=8000, reload=True)
