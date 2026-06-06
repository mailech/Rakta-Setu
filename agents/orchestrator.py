"""
Orchestrator — "The Floor"
Owns the SQLite schema, the event_log, the WebSocket broadcast hook, and the
human-confirm futures. The negotiation control flow itself lives in a LangGraph
StateGraph (see agents/graph.py); run_negotiation() is the thin entry point that
invokes that graph.
"""
import json
import random
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

random.seed(42)

DB_PATH = Path(__file__).parent.parent / "data" / "rakta_setu.db"

# ──────────────────────────────────────────────
# DB SETUP
# ──────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.executescript("""
    CREATE TABLE IF NOT EXISTS negotiations (
        neg_id TEXT PRIMARY KEY,
        bridge_id TEXT,
        state TEXT,
        units_needed INTEGER,
        units_covered INTEGER DEFAULT 0,
        started_at TEXT,
        updated_at TEXT,
        closed_at TEXT,
        result TEXT,
        playbook_snapshot TEXT
    );

    CREATE TABLE IF NOT EXISTS event_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        neg_id TEXT,
        round INTEGER,
        ts TEXT,
        from_agent TEXT,
        to_agent TEXT,
        action TEXT,
        params TEXT,
        say TEXT,
        meta TEXT
    );

    CREATE TABLE IF NOT EXISTS proxies (
        user_id TEXT PRIMARY KEY,
        fatigue_score REAL,
        preference_rules TEXT,
        learned_policy TEXT,
        negotiation_memory TEXT,
        consent TEXT,
        updated_at TEXT
    );

    CREATE TABLE IF NOT EXISTS consent_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        action TEXT,
        scope TEXT,
        ts TEXT,
        neg_id TEXT
    );

    CREATE TABLE IF NOT EXISTS policies (
        bridge_id TEXT PRIMARY KEY,
        playbook TEXT,
        updated_at TEXT
    );
    """)

    conn.commit()
    conn.close()
    print(f"[db] Initialized at {DB_PATH}")


def log_event(neg_id: str, round_num: int, msg: dict, broadcast_fn: Callable = None):
    """Write a message to event_log. Optionally broadcast via WebSocket callback."""
    conn = sqlite3.connect(DB_PATH)
    ts = datetime.utcnow().isoformat()
    conn.execute(
        """INSERT INTO event_log (neg_id, round, ts, from_agent, to_agent, action, params, say, meta)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (neg_id, round_num, ts,
         msg.get("from", ""), msg.get("to", ""),
         msg.get("action", ""), json.dumps(msg.get("params", {})),
         msg.get("say", ""), json.dumps(msg.get("meta", {})))
    )
    conn.commit()
    conn.close()

    # Broadcast to WebSocket if callback provided
    if broadcast_fn:
        broadcast_fn({
            "neg_id": neg_id, "round": round_num, "ts": ts, **msg
        })


def update_negotiation(neg_id: str, state: str, units_covered: int = None,
                        result: str = None, closed: bool = False):
    conn = sqlite3.connect(DB_PATH)
    now = datetime.utcnow().isoformat()
    if closed:
        conn.execute(
            "UPDATE negotiations SET state=?, units_covered=?, result=?, updated_at=?, closed_at=? WHERE neg_id=?",
            (state, units_covered or 0, result or state, now, now, neg_id)
        )
    else:
        conn.execute(
            "UPDATE negotiations SET state=?, units_covered=?, updated_at=? WHERE neg_id=?",
            (state, units_covered or 0, now, neg_id)
        )
    conn.commit()
    conn.close()


def save_proxy(donor: dict):
    conn = sqlite3.connect(DB_PATH)
    now = datetime.utcnow().isoformat()
    conn.execute(
        """INSERT OR REPLACE INTO proxies
           (user_id, fatigue_score, preference_rules, learned_policy, negotiation_memory, consent, updated_at)
           VALUES (?,?,?,?,?,?,?)""",
        (donor["user_id"],
         donor.get("fatigue_score", 0),
         json.dumps(donor.get("preference_rules", {})),
         json.dumps(donor.get("learned_policy", {})),
         json.dumps(donor.get("negotiation_memory", [])),
         json.dumps(donor.get("consent", {})),
         now)
    )
    conn.commit()
    conn.close()


def log_consent(user_id: str, action: str, scope: str, neg_id: str = ""):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO consent_ledger (user_id, action, scope, ts, neg_id) VALUES (?,?,?,?,?)",
        (user_id, action, scope, datetime.utcnow().isoformat(), neg_id)
    )
    conn.commit()
    conn.close()


def get_neg(neg_id: str) -> Optional[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM negotiations WHERE neg_id=?", (neg_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_event_log(neg_id: str) -> List[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM event_log WHERE neg_id=? ORDER BY id ASC", (neg_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ──────────────────────────────────────────────
# ORCHESTRATOR
# ──────────────────────────────────────────────

# State machine states
STATES = ["OPEN", "COLLECTING", "RESOLVING", "CONFIRMING",
          "COVERED", "PARTIAL", "FAILED", "LESSONS_WRITTEN"]

# Pending human confirmations: neg_id -> {user_id: Future}
_pending_confirms: dict = {}

# WebSocket broadcast callback (set by API layer)
_broadcast_fn: Optional[Callable] = None


def set_broadcast_fn(fn: Callable):
    global _broadcast_fn
    _broadcast_fn = fn


def _log(neg_id: str, round_num: int, msg: dict):
    log_event(neg_id, round_num, msg, _broadcast_fn)


async def run_negotiation(
    bridge_record: dict,
    all_donors: dict,   # user_id -> donor dict
    exchange,           # ExchangeAgent instance
    config: dict,
    llm_fn=None,
    auto_confirm: bool = False,  # CLI/replay: skip the human-confirm wait
    neg_id: Optional[str] = None,  # API supplies this so the WS room matches
) -> dict:
    """
    Run a full negotiation for one bridge by invoking the LangGraph StateGraph
    (agents/graph.py). The graph is the §5.3 state machine; this just hands off.
    """
    from agents.graph import run_graph
    return await run_graph(
        bridge_record, all_donors, exchange, config,
        llm_fn=llm_fn, auto_confirm=auto_confirm, neg_id=neg_id,
    )


def human_confirm(neg_id: str, user_id: str, confirmed: bool):
    """Called by API when human taps Confirm/Decline on phone sim."""
    futures = _pending_confirms.get(neg_id, {})
    fut = futures.get(user_id)
    if fut and not fut.done():
        fut.set_result(confirmed)


def human_decline(neg_id: str, user_id: str):
    """Shortcut for decline."""
    human_confirm(neg_id, user_id, False)
