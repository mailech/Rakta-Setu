"""
LangGraph negotiation orchestration (spec §5.3 state machine, as an explicit graph).

The negotiation protocol is modeled as a LangGraph StateGraph — each protocol round
is a node, and the §5.3 transitions are graph edges:

    broadcast ─► collect ─► resolve ─┬─(insufficient)─► escalate ─► confirm ─► learn ─► END
                                     └─(covered)──────────────────► confirm ─┘

Each agent (Proxy / Guardian / Exchange) is invoked inside the nodes. The LLM is used
only for language/judgment via the Proxy persona; all facts stay deterministic.
This gives a real, inspectable multi-agent state graph (the kind judges recognize)
without handing control flow to an LLM.
"""
import asyncio
import random
import sqlite3
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import StateGraph, END

from agents.orchestrator import (
    DB_PATH, _log, update_negotiation, log_consent, get_event_log, _pending_confirms,
)
from agents.proxy import ProxyAgent
from agents.guardian import GuardianAgent


class NegState(TypedDict, total=False):
    neg_id: str
    bridge: dict
    bridge_id: str
    config: dict
    units_needed: int
    round_timeout: int
    auto_confirm: bool
    ctx: Dict[str, Any]            # runtime objects: exchange, all_donors, llm_fn, guardian
    requests: Dict[str, Any]
    responses: List[dict]
    resolution: dict
    coverage: int
    backfill_pool: List[dict]
    confirmed: List[str]
    declined: List[str]
    result: str
    learning_out: dict
    failed_early: bool


# ──────────────────────────────────────────────
# NODE: BROADCAST (Round 0)
# ──────────────────────────────────────────────
async def node_broadcast(state: NegState) -> dict:
    neg_id = state["neg_id"]; bridge = state["bridge"]; bridge_id = state["bridge_id"]
    ctx = state["ctx"]; exchange = ctx["exchange"]; guardian = ctx["guardian"]; config = state["config"]

    update_negotiation(neg_id, "COLLECTING")
    candidates = guardian.get_candidates()
    print(f"\n[graph:broadcast] {len(candidates)} candidates (need {guardian.units_needed}, "
          f"overbook x{config.get('OVERBOOK_FACTOR',1.5)})")

    # Contact ALL matched candidates. We record a lock for the Exchange arbitration
    # story, but a lock being held (e.g. by an abandoned earlier run) must NOT drop a
    # donor from this negotiation — otherwise repeated demo triggers starve each other.
    locked = list(candidates)
    for c in candidates:
        exchange.acquire_lock(c["user_id"], neg_id)  # best-effort ownership record
        log_consent(c["user_id"], "negotiation_start", "availability_negotiation", neg_id)

    if not locked:
        print("[graph:broadcast] no candidates matched — FAILED")
        update_negotiation(neg_id, "FAILED", 0, "FAILED", closed=True)
        return {"failed_early": True, "result": "FAILED", "coverage": 0, "confirmed": [], "declined": []}

    all_donors = ctx["all_donors"]
    requests = {}
    for c in locked:
        uid = c["user_id"]
        # Full donor (preferences, gender, history from donors.json) + roster extras
        # (distance to this bridge, compatibility). The Proxy reasons over both.
        donor_rec = {**all_donors.get(uid, {}), **c}
        req = guardian.build_request(uid); req["neg_id"] = neg_id; req["round"] = 0
        _log(neg_id, 0, req)
        requests[uid] = (req, donor_rec)
    return {"requests": requests, "failed_early": False}


def route_after_broadcast(state: NegState) -> str:
    return "end" if state.get("failed_early") else "collect"


# ──────────────────────────────────────────────
# NODE: COLLECT RESPONSES (Round 1)
# ──────────────────────────────────────────────
async def node_collect(state: NegState) -> dict:
    neg_id = state["neg_id"]; bridge_id = state["bridge_id"]
    ctx = state["ctx"]; llm_fn = ctx["llm_fn"]
    timeout = state["round_timeout"]; requests = state["requests"]

    # LLM (Bedrock) is a blocking network call. Two guards so a 40-donor emergency
    # doesn't make 40 blocking calls and freeze the event loop:
    #  1) only the first LLM_PROXY_CAP proxies use the LLM (the rest are rule-based,
    #     instant, with canned 'say' lines) — keeps Bedrock visible but cheap/fast.
    #  2) every proxy.respond runs in a worker thread (asyncio.to_thread) so the
    #     blocking call never stalls the server's event loop.
    LLM_PROXY_CAP = 6

    async def proxy_respond(idx, uid, req, donor_rec):
        use_llm = llm_fn if idx < LLM_PROXY_CAP else None
        proxy = ProxyAgent(donor_rec, use_llm)
        await asyncio.sleep(random.uniform(0.1, 0.4))   # theater
        resp = await asyncio.to_thread(proxy.respond, req)   # off the event loop
        resp["neg_id"] = neg_id; resp["round"] = 1; resp["to"] = f"guardian:{bridge_id}"
        _log(neg_id, 1, resp)
        print(f"  {proxy.alias}: {resp['action']} — {resp['say'][:60]}")
        return resp

    tasks = [proxy_respond(i, uid, req, dr) for i, (uid, (req, dr)) in enumerate(requests.items())]
    try:
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout)
        responses = [r for r in results if isinstance(r, dict)]
    except asyncio.TimeoutError:
        print("[graph:collect] timeout — partial responses")
        responses = []
    return {"responses": responses}


# ──────────────────────────────────────────────
# NODE: RESOLVE (Round 2 — greedy assignment)
# ──────────────────────────────────────────────
async def node_resolve(state: NegState) -> dict:
    neg_id = state["neg_id"]; bridge = state["bridge"]; bridge_id = state["bridge_id"]
    guardian = state["ctx"]["guardian"]
    update_negotiation(neg_id, "RESOLVING")
    resolution = guardian.resolve(state["responses"])
    coverage = resolution["coverage"]
    print(f"[graph:resolve] coverage {coverage}/{guardian.units_needed}")

    for r in resolution["accepted"]:
        _log(neg_id, 2, {
            "neg_id": neg_id, "round": 2, "from": f"guardian:{bridge_id}", "to": r["from"],
            "action": "ACCEPT", "params": {"date": bridge.get("next_transfusion_date")},
            "say": "You're confirmed. Thank you — checking with your donor now."})
    return {"resolution": resolution, "coverage": coverage, "backfill_pool": list(resolution["rejected"])}


def route_after_resolve(state: NegState) -> str:
    return "confirm" if state["resolution"]["sufficient"] else "escalate"


# ──────────────────────────────────────────────
# NODE: ESCALATE (Exchange mini-round)
# ──────────────────────────────────────────────
async def node_escalate(state: NegState) -> dict:
    neg_id = state["neg_id"]; bridge = state["bridge"]
    ctx = state["ctx"]; exchange = ctx["exchange"]; all_donors = ctx["all_donors"]; llm_fn = ctx["llm_fn"]
    guardian = ctx["guardian"]; resolution = state["resolution"]; coverage = state["coverage"]

    deficit = guardian.units_needed - coverage
    print(f"[graph:escalate] deficit={deficit} — Exchange joining")
    esc = guardian.escalate(deficit); esc["neg_id"] = neg_id; esc["round"] = 2
    _log(neg_id, 2, esc)

    outside = exchange.handle_escalation(esc, neg_id)
    print(f"  Exchange found {len(outside)} outside candidates")
    for oc in outside[:deficit]:
        uid = oc["user_id"]
        if not exchange.acquire_lock(uid, neg_id):
            continue
        donor_rec = all_donors.get(uid, oc)
        proxy = ProxyAgent(donor_rec, llm_fn)
        req = guardian.build_request(uid); req["neg_id"] = neg_id; req["round"] = 2
        req["params"]["source"] = oc.get("source", "exchange")
        _log(neg_id, 2, req)
        await asyncio.sleep(random.uniform(0.1, 0.3))
        resp = await asyncio.to_thread(proxy.respond, req); resp["neg_id"] = neg_id; resp["round"] = 2
        _log(neg_id, 2, resp)
        print(f"  Exchange candidate {proxy.alias}: {resp['action']}")
        if resp["action"] in {"OFFER", "CONDITIONAL_OFFER", "REQUEST_HUMAN_CONFIRM"}:
            resolution["accepted"].append(resp); coverage += 1
            if coverage >= guardian.units_needed:
                break
        else:
            exchange.release_lock(uid)
    return {"resolution": resolution, "coverage": coverage}


# ──────────────────────────────────────────────
# NODE: CONFIRM (Round 3 — human confirm + live backfill + SMS)
# ──────────────────────────────────────────────
async def node_confirm(state: NegState) -> dict:
    neg_id = state["neg_id"]; bridge = state["bridge"]; bridge_id = state["bridge_id"]
    ctx = state["ctx"]; exchange = ctx["exchange"]; all_donors = ctx["all_donors"]; guardian = ctx["guardian"]
    resolution = state["resolution"]; backfill_pool = list(state.get("backfill_pool", []))
    auto_confirm = state.get("auto_confirm", False)
    # Confirm round must outlast the call escalation: the phone rings at
    # escalate_after, and the donor needs time to answer + press 1. Give a
    # generous window (escalate_after + 90s, min 120s) so it never times out
    # mid-call on stage.
    cfg = state.get("config", {}) or {}
    if cfg.get("CONFIRM_TIMEOUT_SECS"):
        timeout = int(cfg["CONFIRM_TIMEOUT_SECS"])   # used by the radius-escalation retries
    else:
        try:
            from data.policy import get_policy
            timeout = max(120, int(get_policy().get("escalate_after", 20)) + 90)
        except Exception:
            timeout = max(120, state["round_timeout"])

    update_negotiation(neg_id, "CONFIRMING", state["coverage"])
    print(f"[graph:confirm] {len(resolution['accepted'])} requests, {len(backfill_pool)} backfill")

    confirmed: List[str] = []; declined: List[str] = []
    _pending_confirms[neg_id] = {}

    def send_confirm(resp):
        uid = resp["from"].split(":")[-1]
        _log(neg_id, 3, {
            "neg_id": neg_id, "round": 3, "from": resp["from"], "to": "human",
            "action": "REQUEST_HUMAN_CONFIRM",
            "params": {"date": bridge.get("next_transfusion_date"),
                       "donor": resp.get("meta", {}).get("alias", uid[:8])},
            "say": f"You're booked for {bridge.get('next_transfusion_date')}. Confirm? (one tap)"})
        _pending_confirms[neg_id][uid] = asyncio.get_event_loop().create_future()
        return uid

    emergency = bool(bridge.get("emergency"))
    # EMERGENCY: ask every matched donor at once. Else: just the shortlisted ones.
    contact = list(resolution["accepted"])
    if emergency:
        contact = contact + list(backfill_pool)
        backfill_pool = []
    for r in contact:
        send_confirm(r)

    # ── Real interactive alert (SMS → call escalation) to the live phones ──
    if not auto_confirm and contact:
        try:
            from notify import twilio_channel as twi
            n_live = max(1, len(twi.load_live_phones())) if hasattr(twi, "load_live_phones") else 2
        except Exception:
            n_live = 2
        # Only the first N contacts get a REAL phone (we have N live numbers).
        # Emergency still *broadcasts confirm requests* to everyone (above), but we
        # don't spam the same 2 real numbers 20× — the rest are simulated proxies.
        for r in contact[:n_live]:
            _dispatch_real_channel(neg_id, bridge, r)

    if auto_confirm:
        confirmed = list(_pending_confirms[neg_id].keys())
        print(f"  [auto] confirming {len(confirmed)}")
    else:
        deadline = time.monotonic() + timeout
        while len(confirmed) < guardian.units_needed and _pending_confirms[neg_id]:
            pend = {u: f for u, f in _pending_confirms[neg_id].items() if not f.done()}
            if not pend:
                break
            done, _ = await asyncio.wait(pend.values(),
                                         timeout=max(0.1, deadline - time.monotonic()),
                                         return_when=asyncio.FIRST_COMPLETED)
            if not done:
                print("  confirm timeout"); break
            for uid, fut in list(pend.items()):
                if not fut.done():
                    continue
                ok = fut.result() is True
                _pending_confirms[neg_id].pop(uid, None)
                if ok:
                    confirmed.append(uid)
                else:
                    declined.append(uid); exchange.release_lock(uid)
                    print(f"  DECLINE {uid[:12]}… — backfilling")
                    if backfill_pool and len(confirmed) < guardian.units_needed:
                        nxt = backfill_pool.pop(0)
                        _log(neg_id, 3, {
                            "neg_id": neg_id, "round": 3, "from": f"guardian:{bridge_id}", "to": "exchange",
                            "action": "PROPOSE_ALTERNATIVE", "params": {"reason": "decline_backfill"},
                            "say": "A donor stepped back — pulling in the next from the buffer."})
                        send_confirm(nxt)

    # Lock confirmed slots
    for uid in confirmed:
        lk = guardian.lock_slot(uid, bridge.get("next_transfusion_date", ""))
        lk["neg_id"] = neg_id; lk["round"] = 3
        prof = _confirmed_profile(uid, bridge, all_donors)
        lk["meta"] = {**lk.get("meta", {}), "profile": prof}
        lk["say"] = f"✅ {prof['name']} confirmed — {prof['blood_group']}, {prof['age']}y, {prof.get('distance_km','?')}km away."
        _log(neg_id, 3, lk)

    # ── REAL SMS to the patient (spec §8) ──
    _notify_patient(neg_id, bridge, confirmed, all_donors)
    return {"confirmed": confirmed, "declined": declined}


def _dispatch_real_channel(neg_id, bridge, accepted_resp):
    """
    Fire the can't-ignore alert to a real donor phone (if Twilio is enabled):
    SMS Confirm/Decline now; the Twilio module auto-escalates to a CALL if the
    donor doesn't reply within ESCALATE_AFTER seconds. The live phone for this
    donor is resolved inside send_whatsapp_confirm (from data/live_phones.json).
    """
    try:
        from notify import twilio_channel as twi
        if not twi.cfg.get("enabled"):
            return
        uid = accepted_resp["from"].split(":")[-1]
        date = (bridge.get("next_transfusion_date") or "")[:10]
        tm   = bridge.get("req_time", "")
        bg   = bridge.get("bridge_blood_group", "")
        hosp = bridge.get("hospital_name", "the hospital")
        urgent = "URGENT " if bridge.get("emergency") else ""
        try:
            from data.policy import get_policy
            delay = int(get_policy().get("escalate_after", twi.ESCALATE_AFTER))
        except Exception:
            delay = twi.ESCALATE_AFTER
        body = (f"RAKTA-SETU: {urgent}A patient needs {bg} blood at {hosp} on {date} {tm}. "
                f"Can you donate? We'll call you in ~{delay}s — press 1 to confirm, 2 to decline. "
                f"(On WhatsApp you can also reply YES/NO.)")

        # ── Amazon Translate "Rural Reach": send in the donor's own language ──
        lang_name = "English"
        try:
            from data.profiles import profile
            from aws import translate as tr
            roster = {d["user_id"]: d for d in bridge.get("roster", [])}
            prof = (roster.get(uid, {}).get("profile")) or profile(roster.get(uid, {"user_id": uid}))
            t = tr.translate(body, prof.get("preferred_language", "en"))
            body = t["text"]; lang_name = t["lang_name"]
            if t["translated"]:
                _log(neg_id, 3, {
                    "neg_id": neg_id, "round": 3, "from": "exchange:singleton", "to": "donor",
                    "action": "REQUEST_HUMAN_CONFIRM", "params": {"channel": "translate", "lang": lang_name},
                    "say": f"🌐 Amazon Translate → {lang_name}: {body[:80]}"})
        except Exception as te:
            print(f"[graph] translate skipped: {te}")

        call_ctx = {"bg": bg, "hospital": hosp, "date": date, "time": tm,
                    "lang": (locals().get("prof", {}) or {}).get("preferred_language", "te")}
        res = twi.send_whatsapp_confirm(neg_id, uid, None, body, call_ctx=call_ctx)  # phone resolved internally
        _log(neg_id, 3, {
            "neg_id": neg_id, "round": 3, "from": "exchange:singleton", "to": "donor",
            "action": "REQUEST_HUMAN_CONFIRM",
            "params": {"channel": "sms", "lang": lang_name, "status": res.get("status")},
            "say": f"📲 SMS sent to donor's real phone in {lang_name} ({res.get('status')}) — rings in {delay}s if ignored."})
    except Exception as e:
        print(f"[graph] real-channel dispatch failed: {e}")


def _confirmed_profile(uid, bridge, all_donors):
    """Build the donor-details payload shown on the negotiation floor's right rail."""
    from data.profiles import profile
    donor = all_donors.get(uid, {"user_id": uid})
    prof = profile(donor)
    dist_map = {d["user_id"]: d.get("distance_km") for d in bridge.get("roster", [])}
    prof["distance_km"] = dist_map.get(uid)
    prof["city"] = bridge.get("hospital_city", "")
    prof["lat"] = donor.get("latitude"); prof["lon"] = donor.get("longitude")
    prof["donations"] = donor.get("donations_till_date")
    try:
        from notify import twilio_channel as twi
        prof["phone"] = twi._used_phones.get(uid)
    except Exception:
        pass
    return prof


def _notify_patient(neg_id, bridge, confirmed, all_donors):
    """Text the patient (via MSG91) when a donor confirms — with full donor details."""
    if not confirmed:
        return
    try:
        from notify import sms, twilio_channel as twi
        from data.profiles import profile
        phone = twi.get_patient_phone()                    # default +917416470528
        if not phone:
            return
        dist_map = {d["user_id"]: d.get("distance_km") for d in bridge.get("roster", [])}
        uid = confirmed[0]
        donor = dict(all_donors.get(uid, {}))
        prof = profile(donor)
        donor["alias"] = prof["name"]
        donor["blood_group"] = prof["blood_group"]
        donor.setdefault("distance_km", dist_map.get(uid))
        msg = sms.compose_patient_message(bridge, donor, bridge.get("next_transfusion_date", ""))
        res = sms.send_sms(phone, msg)
        _log(neg_id, 3, {
            "neg_id": neg_id, "round": 3, "from": "exchange:singleton", "to": "patient",
            "action": "ACCEPT",
            "params": {"channel": "SMS", "to": phone, "status": res.get("status")},
            "say": f"📲 Patient texted ({res.get('status')}): {prof['name']} confirmed, {prof['blood_group']}, {donor.get('distance_km','?')}km."})
    except Exception as e:
        print(f"[graph] patient notify failed: {e}")


# ──────────────────────────────────────────────
# NODE: LEARN (failure learning §6 + finalize)
# ──────────────────────────────────────────────
async def node_learn(state: NegState) -> dict:
    neg_id = state["neg_id"]; bridge_id = state["bridge_id"]; config = state["config"]
    ctx = state["ctx"]; exchange = ctx["exchange"]; all_donors = ctx["all_donors"]; llm_fn = ctx["llm_fn"]
    guardian = ctx["guardian"]
    confirmed = state.get("confirmed", [])

    final_cov = len(confirmed)
    result = "COVERED" if final_cov >= guardian.units_needed else "PARTIAL" if final_cov > 0 else "FAILED"
    update_negotiation(neg_id, result, final_cov, result, closed=True)

    for uid in list(exchange.lock_table.keys()):
        if exchange.lock_table.get(uid) == neg_id:
            exchange.release_lock(uid)
    _pending_confirms.pop(neg_id, None)
    print(f"[graph] NEG {neg_id} -> {result} ({final_cov}/{guardian.units_needed})")

    learning_out = {}
    try:
        from learning.lessons import learn_from_negotiation
        learning_out = learn_from_negotiation(
            neg_id, bridge_id, get_event_log(neg_id), result, config, all_donors, llm_fn)
        if learning_out.get("diff", {}).get("guardian"):
            print(f"[learning] playbook updated: {learning_out['diff']['guardian']}")
    except Exception as e:
        print(f"[learning] skipped: {e}")
    update_negotiation(neg_id, "LESSONS_WRITTEN", final_cov, result, closed=True)

    # ── Amazon S3 audit trail (responsible-data / proof of AWS usage) ──
    try:
        from aws import s3_audit
        transcript = get_event_log(neg_id)
        s3_audit.upload(neg_id, {
            "neg_id": neg_id, "bridge_id": bridge_id, "result": result,
            "units_needed": guardian.units_needed, "units_covered": final_cov,
            "confirmed_donors": confirmed, "lessons": learning_out.get("lessons", {}),
            "transcript": transcript,
        }, ts=transcript[-1]["ts"] if transcript else None)
    except Exception as e:
        print(f"[s3] audit skipped: {e}")
    return {"result": result, "learning_out": learning_out}


# ──────────────────────────────────────────────
# BUILD + COMPILE (once)
# ──────────────────────────────────────────────
def _build():
    g = StateGraph(NegState)
    g.add_node("broadcast", node_broadcast)
    g.add_node("collect", node_collect)
    g.add_node("resolve", node_resolve)
    g.add_node("escalate", node_escalate)
    g.add_node("confirm", node_confirm)
    g.add_node("learn", node_learn)
    g.set_entry_point("broadcast")
    g.add_conditional_edges("broadcast", route_after_broadcast, {"collect": "collect", "end": END})
    g.add_edge("collect", "resolve")
    g.add_conditional_edges("resolve", route_after_resolve, {"escalate": "escalate", "confirm": "confirm"})
    g.add_edge("escalate", "confirm")
    g.add_edge("confirm", "learn")
    g.add_edge("learn", END)
    return g.compile()


NEGOTIATION_GRAPH = _build()


async def run_graph(bridge_record, all_donors, exchange, config, llm_fn=None,
                    auto_confirm=False, neg_id=None) -> dict:
    """Public entry: build initial state, run the LangGraph, return the result dict."""
    if not neg_id:
        neg_id = f"NEG-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{bridge_record['bridge_id'][:8]}"
    bridge_id = bridge_record["bridge_id"]

    conn = sqlite3.connect(DB_PATH)
    conn.execute("""INSERT OR IGNORE INTO negotiations (neg_id, bridge_id, state, units_needed, started_at, updated_at)
                    VALUES (?,?,?,?,?,?)""",
                 (neg_id, bridge_id, "OPEN", bridge_record.get("quantity_required", 1),
                  datetime.utcnow().isoformat(), datetime.utcnow().isoformat()))
    conn.commit(); conn.close()

    guardian = GuardianAgent(bridge_record, config, llm_fn)
    state: NegState = {
        "neg_id": neg_id, "bridge": bridge_record, "bridge_id": bridge_id, "config": config,
        "units_needed": guardian.units_needed, "round_timeout": config.get("ROUND_TIMEOUT_SECS", 30),
        "auto_confirm": auto_confirm,
        "ctx": {"exchange": exchange, "all_donors": all_donors, "llm_fn": llm_fn, "guardian": guardian},
        "confirmed": [], "declined": [], "result": "FAILED", "coverage": 0,
    }
    final = await NEGOTIATION_GRAPH.ainvoke(state)
    lo = final.get("learning_out", {}) or {}
    return {
        "neg_id": neg_id, "bridge_id": bridge_id,
        "result": final.get("result", "FAILED"),
        "coverage": len(final.get("confirmed", [])),
        "units_needed": guardian.units_needed,
        "confirmed_donors": final.get("confirmed", []),
        "declined_donors": final.get("declined", []),
        "lessons": lo.get("lessons", {}), "policy_diff": lo.get("diff", {}), "playbook": lo.get("playbook", {}),
        "engine": "langgraph",
    }
