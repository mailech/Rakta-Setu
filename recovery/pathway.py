"""
Module A — DEFERRAL GUARD (spec §6B, Leak 2: the deferral black hole)

Three pieces, all cheap, all here:
  1. Conversational pre-screen (4 WHO/NACO-style questions) -> CLEAR / SOFT_FLAG / DEFER_LIKELY
  2. Recovery pathway state machine:
       FLAGGED -> NURTURE -> RETEST_REMINDER -> REINVITE -> RECOVERED | DORMANT
  3. Deferral-risk heuristic (population-level priors, NOT individual medical claims)

Honesty (stated on the slide): the CSV has no deferral/hemoglobin data, so v1 is a
rule-based pre-screen + recovery state machine. The deferral-risk *prediction model*
is the data ask to Blood Warriors.

Privacy: raw pre-screen answers never leave this module. Only the verdict
(CLEAR / SOFT_FLAG / DEFER_LIKELY) is ever returned to the Guardian — which sees only
a WITHDRAW, never the health reason. (In production this runs inside the Proxy's
Nitro Enclave; spec §6C.)
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

DB_PATH = Path(__file__).parent.parent / "data" / "rakta_setu.db"

# Pre-screen verdicts
CLEAR        = "CLEAR"
SOFT_FLAG    = "SOFT_FLAG"
DEFER_LIKELY = "DEFER_LIKELY"

# Recovery states
FLAGGED          = "FLAGGED"
NURTURE          = "NURTURE"
RETEST_REMINDER  = "RETEST_REMINDER"
REINVITE         = "REINVITE"
RECOVERED        = "RECOVERED"
DORMANT          = "DORMANT"

RECOVERY_SEQUENCE = [FLAGGED, NURTURE, RETEST_REMINDER, REINVITE, RECOVERED]

# The 4-question micro-chat (WHO/NACO-style triage — published eligibility criteria,
# NOT diagnosis). Each answer is a bool from the donor's one-tap reply.
PRESCREEN_QUESTIONS = [
    {"id": "recent_illness", "q": "Any fever, cold, or infection in the last 2 weeks?",          "flag": DEFER_LIKELY},
    {"id": "medication",     "q": "On antibiotics or any new medication right now?",             "flag": DEFER_LIKELY},
    {"id": "ate_today",      "q": "Have you eaten in the last 4 hours?",                          "flag": SOFT_FLAG, "invert": True},
    {"id": "low_hb_history", "q": "Ever been told your hemoglobin/iron is low, or feeling unusually tired?", "flag": DEFER_LIKELY},
]

NURTURE_NUDGES = [
    "Day 0: Iron-rich foods help — spinach, dates, jaggery, lentils. Small daily wins.",
    "Day 15: Pair iron with vitamin C (lemon, amla). Skip tea/coffee right after meals.",
    "Day 30: You're building back up. A short walk + good sleep helps absorption too.",
]


# ──────────────────────────────────────────────
# 1. PRE-SCREEN
# ──────────────────────────────────────────────

def prescreen(answers: Dict[str, bool]) -> dict:
    """
    Given the donor's answers (id -> bool), return a verdict + a kind, plain-language
    message. Raw answers are NOT included in what the Guardian sees.

    answers True means "yes" to the question as asked.
    """
    defer_reasons = []
    soft_reasons  = []

    for q in PRESCREEN_QUESTIONS:
        ans = bool(answers.get(q["id"], False))
        # 'invert' questions flag when the answer is False (e.g. "ate today?" -> No)
        triggered = (not ans) if q.get("invert") else ans
        if not triggered:
            continue
        if q["flag"] == DEFER_LIKELY:
            defer_reasons.append(q["id"])
        else:
            soft_reasons.append(q["id"])

    if defer_reasons:
        verdict = DEFER_LIKELY
        message = ("Thanks for the honesty — let's not risk a wasted trip today. "
                   "We'll help you get ready and reach back out at the right time. "
                   "(Please also check with the blood bank or a doctor.)")
    elif soft_reasons:
        verdict = SOFT_FLAG
        message = ("You're good to go — just eat something and drink plenty of water "
                   "before you come in.")
    else:
        verdict = CLEAR
        message = "All set — see you at your slot!"

    return {
        "verdict":     verdict,
        "message":     message,
        # counts only — never the raw answers (data minimization / TEE boundary)
        "flag_count":  len(defer_reasons) + len(soft_reasons),
        "guardian_sees": "WITHDRAW" if verdict == DEFER_LIKELY else "PROCEED",
    }


# ──────────────────────────────────────────────
# 3. DEFERRAL-RISK HEURISTIC (population priors only)
# ──────────────────────────────────────────────

def deferral_risk(donor: dict) -> float:
    """
    v1 heuristic to decide who gets the pre-screen proactively. Priors are
    population-level (national anemia statistics), explicitly NOT individual claims.
    """
    risk = 0.15  # base
    if str(donor.get("gender", "")).lower() in ("female", "f", "woman"):
        risk += 0.30  # national anemia prevalence skews heavily female
    last = donor.get("last_donation_date")
    # very recent donation -> iron not recovered yet
    if last:
        try:
            cfg = json.loads((Path(__file__).parent.parent / "data" / "config.json").read_text(encoding="utf-8"))
            sim_today = datetime.fromisoformat(cfg["SIM_TODAY"])
            days = (sim_today - datetime.fromisoformat(last)).days
            if 0 <= days < 100:
                risk += 0.20
        except Exception:
            pass
    if donor.get("unverified"):
        risk += 0.05
    return round(min(1.0, risk), 3)


# ──────────────────────────────────────────────
# 2. RECOVERY PATHWAY STATE MACHINE (persisted to SQLite)
# ──────────────────────────────────────────────

def ensure_tables():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS recovery (
        user_id TEXT PRIMARY KEY,
        state TEXT,
        verdict TEXT,
        enrolled_at TEXT,
        updated_at TEXT,
        history TEXT
    );
    """)
    conn.commit()
    conn.close()


def enroll(user_id: str, verdict: str = DEFER_LIKELY) -> dict:
    """Enroll a deferred donor into the recovery pathway (idempotent)."""
    ensure_tables()
    now = datetime.utcnow().isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM recovery WHERE user_id=?", (user_id,)).fetchone()
    if row:
        conn.close()
        return dict(row)
    history = [{"state": FLAGGED, "ts": now}]
    conn.execute(
        "INSERT INTO recovery (user_id, state, verdict, enrolled_at, updated_at, history) VALUES (?,?,?,?,?,?)",
        (user_id, FLAGGED, verdict, now, now, json.dumps(history))
    )
    conn.commit()
    conn.close()
    return {"user_id": user_id, "state": FLAGGED, "verdict": verdict,
            "enrolled_at": now, "history": history}


def advance(user_id: str) -> Optional[dict]:
    """Move a donor to the next recovery state. Returns the updated record."""
    ensure_tables()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM recovery WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        conn.close()
        return None
    rec = dict(row)
    cur = rec["state"]
    nxt = cur
    if cur in RECOVERY_SEQUENCE:
        i = RECOVERY_SEQUENCE.index(cur)
        if i < len(RECOVERY_SEQUENCE) - 1:
            nxt = RECOVERY_SEQUENCE[i + 1]
    now = datetime.utcnow().isoformat()
    history = json.loads(rec.get("history") or "[]")
    history.append({"state": nxt, "ts": now})
    conn.execute("UPDATE recovery SET state=?, updated_at=?, history=? WHERE user_id=?",
                 (nxt, now, json.dumps(history), user_id))
    conn.commit()
    conn.close()
    rec["state"] = nxt
    rec["history"] = history
    return rec


def mark_dormant(user_id: str) -> Optional[dict]:
    ensure_tables()
    now = datetime.utcnow().isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE recovery SET state=?, updated_at=? WHERE user_id=?",
                 (DORMANT, now, user_id))
    conn.commit()
    conn.close()
    return get_one(user_id)


def get_one(user_id: str) -> Optional[dict]:
    ensure_tables()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM recovery WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def pipeline() -> dict:
    """Summary for the Bridge Board 'Recovery pipeline' counter."""
    ensure_tables()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM recovery ORDER BY updated_at DESC").fetchall()
    conn.close()
    records = [dict(r) for r in rows]
    by_state: Dict[str, int] = {}
    for r in records:
        by_state[r["state"]] = by_state.get(r["state"], 0) + 1
    active = sum(v for k, v in by_state.items() if k not in (RECOVERED, DORMANT))
    return {
        "total":      len(records),
        "active":     active,
        "recovered":  by_state.get(RECOVERED, 0),
        "dormant":    by_state.get(DORMANT, 0),
        "by_state":   by_state,
        "records":    records[:50],
        "nurture_nudges": NURTURE_NUDGES,
    }
