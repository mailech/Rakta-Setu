"""
Failure Learning (spec §6) — capability #6, the signature feature.

After every negotiation:
  1. An LLM call summarizes the transcript into structured lessons.
  2. Deterministic code applies them:
       - Proxy fixes   -> learned_policy (donor memory)
       - Guardian fixes -> playbook (LEAD_DAYS, OVERBOOK_FACTOR, rotation depth, weights)
  3. Lessons + patched policies are persisted to SQLite.

The whole point: run the same bridge twice and watch the system rewrite its own
protocol. Nobody told it to.
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

DB_PATH = Path(__file__).parent.parent / "data" / "rakta_setu.db"

# Fields the Guardian's playbook is allowed to learn (whitelist — never let the
# LLM patch arbitrary attributes).
GUARDIAN_LEARNABLE = {"lead_days", "overbook_factor", "rotation_depth"}
PROXY_LEARNABLE     = {"advance_notice_days", "best_channel", "accepts_weekends",
                       "declined_morning_slots"}


# ──────────────────────────────────────────────
# PERSISTENCE
# ──────────────────────────────────────────────

def ensure_tables():
    """Add the lessons table (idempotent)."""
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS lessons (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        neg_id TEXT,
        bridge_id TEXT,
        outcome TEXT,
        lessons_json TEXT,
        ts TEXT
    );
    """)
    conn.commit()
    conn.close()


def get_playbook(bridge_id: str, config: dict) -> dict:
    """Load a Guardian's persisted playbook, falling back to config defaults."""
    defaults = {
        "lead_days":       config.get("LEAD_DAYS", 7),
        "overbook_factor": config.get("OVERBOOK_FACTOR", 1.5),
        "rotation_depth":  3,
    }
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT playbook FROM policies WHERE bridge_id=?", (bridge_id,)).fetchone()
    except sqlite3.OperationalError:
        row = None
    conn.close()
    if row and row["playbook"]:
        try:
            stored = json.loads(row["playbook"])
            defaults.update({k: v for k, v in stored.items() if k in GUARDIAN_LEARNABLE})
        except json.JSONDecodeError:
            pass
    return defaults


def save_playbook(bridge_id: str, playbook: dict):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO policies (bridge_id, playbook, updated_at) VALUES (?,?,?)
           ON CONFLICT(bridge_id) DO UPDATE SET playbook=excluded.playbook, updated_at=excluded.updated_at""",
        (bridge_id, json.dumps(playbook), datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()


def save_lessons_record(neg_id: str, bridge_id: str, lessons: dict):
    ensure_tables()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO lessons (neg_id, bridge_id, outcome, lessons_json, ts) VALUES (?,?,?,?,?)",
        (neg_id, bridge_id, lessons.get("outcome", ""), json.dumps(lessons), datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()


# ──────────────────────────────────────────────
# EXTRACTION (LLM, with deterministic fallback)
# ──────────────────────────────────────────────

def extract_lessons(neg_id: str, transcript: List[dict], outcome: str,
                    llm_fn: Optional[Callable] = None) -> dict:
    """
    Summarize the transcript into structured lessons.
    Uses the LLM if available; otherwise derives lessons deterministically from
    the message actions so the replay demo works fully offline.
    """
    if llm_fn:
        system = (
            "You are the failure-learning module. Read the negotiation transcript and its "
            "outcome and return structured lessons that improve future negotiations. "
            'Respond as JSON: {"outcome":"...","summary":"...","failures":[{"type":"...",'
            '"target":"guardian|proxy","donor":"<user_id or null>","field":"...","new":<value>}]}. '
            "Only patch these guardian fields: lead_days, overbook_factor, rotation_depth. "
            "Only patch these proxy fields: advance_notice_days, best_channel, accepts_weekends."
        )
        user = f"OUTCOME: {outcome}\nTRANSCRIPT:\n{json.dumps(transcript[-40:], default=str)}"
        try:
            result = llm_fn(system, user, json_mode=True)
            result.setdefault("outcome", outcome)
            result.setdefault("failures", [])
            return _normalize(result)
        except Exception:
            pass
    return _derive_lessons_deterministic(transcript, outcome)


def _params_of(m: dict) -> dict:
    """event_log rows store params as a JSON string; in-memory msgs as a dict."""
    p = m.get("params", {})
    if isinstance(p, str):
        try:
            return json.loads(p) if p else {}
        except json.JSONDecodeError:
            return {}
    return p or {}


def _derive_lessons_deterministic(transcript: List[dict], outcome: str) -> dict:
    """
    Rule-based lesson derivation (no LLM). Scans the transcript for the failure
    signatures the protocol can produce.
    """
    failures = []

    short_notice = [m for m in transcript
                    if m.get("action") == "COUNTER"
                    and _params_of(m).get("reason") == "needs_more_notice"]
    declines     = [m for m in transcript if m.get("action") == "DECLINE"]
    protected    = [m for m in transcript
                    if m.get("action") == "DECLINE"
                    and _params_of(m).get("reason") == "protected"]

    if short_notice:
        # Donors needed more notice -> Guardian should open earlier next time.
        failures.append({
            "type":   "DECLINED_SHORT_NOTICE",
            "target": "guardian",
            "field":  "lead_days",
            "new":    10,
            "count":  len(short_notice),
        })

    if len(protected) >= 2 or (len(declines) >= 2 and outcome != "COVERED"):
        # Too many of the top-ranked roster were unavailable -> widen the funnel.
        failures.append({
            "type":   "OVERASKED_SEGMENT",
            "target": "guardian",
            "field":  "overbook_factor",
            "new":    2.0,
        })
        failures.append({
            "type":   "OVERASKED_SEGMENT",
            "target": "guardian",
            "field":  "rotation_depth",
            "new":    5,
        })

    summary = (
        "Clean run — no protocol changes needed." if not failures else
        f"{len(failures)} adjustment(s) learned from a {outcome} outcome: "
        + ", ".join(f["type"] for f in failures)
    )
    return {"outcome": outcome, "summary": summary, "failures": failures}


def _normalize(result: dict) -> dict:
    """Coerce LLM output (which may use the §6 nested 'fix' shape) to a flat list."""
    out = []
    for f in result.get("failures", []):
        if "fix" in f and isinstance(f["fix"], dict):
            fix = f["fix"]
            out.append({
                "type":   f.get("type", "UNKNOWN"),
                "target": fix.get("target", "guardian"),
                "donor":  f.get("donor"),
                "field":  fix.get("field"),
                "new":    fix.get("new"),
            })
        else:
            out.append(f)
    result["failures"] = out
    return result


# ──────────────────────────────────────────────
# APPLICATION
# ──────────────────────────────────────────────

def apply_lessons(lessons: dict, bridge_id: str, playbook: dict,
                  donors_by_id: Optional[dict] = None) -> dict:
    """
    Apply the structured lessons.
      - Guardian patches mutate (and persist) the playbook.
      - Proxy patches mutate the donor record's learned_policy (in-memory; the
        API/engine persists donors).
    Returns a diff: {"guardian": {field: [old, new]}, "proxy": {uid: {field:[old,new]}}}.
    """
    diff = {"guardian": {}, "proxy": {}}

    for f in lessons.get("failures", []):
        target = f.get("target", "guardian")
        field  = f.get("field")
        new    = f.get("new")
        if field is None or new is None:
            continue

        if target == "guardian" and field in GUARDIAN_LEARNABLE:
            old = playbook.get(field)
            if old != new:
                playbook[field] = new
                diff["guardian"][field] = [old, new]

        elif target == "proxy" and field in PROXY_LEARNABLE and donors_by_id:
            uid = f.get("donor")
            donor = donors_by_id.get(uid) if uid else None
            if donor is not None:
                lp = donor.setdefault("learned_policy", {})
                old = lp.get(field)
                if old != new:
                    lp[field] = new
                    diff["proxy"].setdefault(uid, {})[field] = [old, new]

    if diff["guardian"]:
        save_playbook(bridge_id, playbook)

    return diff


# ──────────────────────────────────────────────
# TOP-LEVEL ENTRY (called by orchestrator after close)
# ──────────────────────────────────────────────

def learn_from_negotiation(neg_id: str, bridge_id: str, transcript: List[dict],
                           outcome: str, config: dict,
                           donors_by_id: Optional[dict] = None,
                           llm_fn: Optional[Callable] = None) -> dict:
    """
    Full failure-learning pass. Returns {"lessons":..., "diff":..., "playbook":...}.
    """
    ensure_tables()
    lessons  = extract_lessons(neg_id, transcript, outcome, llm_fn)
    playbook = get_playbook(bridge_id, config)
    diff     = apply_lessons(lessons, bridge_id, playbook, donors_by_id)
    save_lessons_record(neg_id, bridge_id, lessons)
    return {"lessons": lessons, "diff": diff, "playbook": playbook}
