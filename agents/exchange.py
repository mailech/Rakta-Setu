"""
Exchange Agent (singleton) — loyalty: THE NETWORK
Market-maker: inter-bridge donor loans, emergency pool, reactivation queue.
Holds the global concurrency lock table.
"""
import json
import random
from datetime import datetime
from typing import Dict, List, Optional

random.seed(42)


class ExchangeAgent:
    """
    Handles ESCALATE from Guardians.
    Arbitrates cross-bridge conflicts.
    Maintains scarcity board.
    """

    def __init__(self, all_donors: List[dict], all_bridges: List[dict], config: dict):
        self.donors   = {d["user_id"]: d for d in all_donors}
        self.bridges  = {b["bridge_id"]: b for b in all_bridges}
        self.config   = config

        # Concurrency lock: user_id -> neg_id (one active negotiation per donor)
        self.lock_table: Dict[str, str] = {}

        # Debt tokens: bridge_id -> list of owed favors
        self.debt_ledger: Dict[str, List[dict]] = {}

        # Scarcity board: blood_group -> count of eligible donors
        self.scarcity_board = self._build_scarcity_board()

        # Reactivation queue (to be enriched by churn model in Phase 4)
        self.reactivation_queue = self._build_reactivation_queue()

    # ──────────────────────────────────────────────
    # INIT HELPERS
    # ──────────────────────────────────────────────

    def _build_scarcity_board(self) -> Dict[str, int]:
        board = {}
        for d in self.donors.values():
            bg = d.get("blood_group") or "Unknown"
            if d.get("eligible_now"):
                board[bg] = board.get(bg, 0) + 1
        return board

    def _build_reactivation_queue(self) -> List[dict]:
        """
        Build prioritized list of inactive donors for reactivation.
        Keyed to inactive_trigger_comment for cause-matched messaging.
        """
        candidates = []
        for d in self.donors.values():
            if d.get("user_donation_active_status", "").lower() == "inactive":
                comment = d.get("inactive_trigger_comment", "") or ""
                if "limited activity" in comment.lower() or "multiple calls" in comment.lower():
                    strategy = "rest_and_channel_switch"
                elif "not donated in last" in comment.lower():
                    strategy = "re_onboarding"
                else:
                    strategy = "gentle_reminder"
                candidates.append({
                    "user_id":    d["user_id"],
                    "blood_group": d.get("blood_group"),
                    "propensity": d.get("propensity", 0),
                    "churn_risk": d.get("churn_risk", 1.0),
                    "strategy":   strategy,
                    "comment":    comment,
                })
        # Sort by propensity descending (best reactivation candidates first)
        candidates.sort(key=lambda x: x["propensity"], reverse=True)
        return candidates

    # ──────────────────────────────────────────────
    # CONCURRENCY LOCK TABLE
    # ──────────────────────────────────────────────

    LOCK_TTL = 130  # seconds — locks auto-expire so abandoned negotiations don't
                    # permanently tie up donors (just over the confirm wait window).

    def acquire_lock(self, user_id: str, neg_id: str) -> bool:
        """Returns True if lock acquired. A stale lock (older than LOCK_TTL) is reclaimed."""
        import time
        ts = getattr(self, "_lock_ts", None)
        if ts is None:
            ts = self._lock_ts = {}
        now = time.monotonic()
        if user_id in self.lock_table and (now - ts.get(user_id, 0)) < self.LOCK_TTL:
            return False
        self.lock_table[user_id] = neg_id
        ts[user_id] = now
        return True

    def release_lock(self, user_id: str):
        self.lock_table.pop(user_id, None)
        getattr(self, "_lock_ts", {}).pop(user_id, None)

    def is_locked(self, user_id: str) -> bool:
        import time
        ts = getattr(self, "_lock_ts", {})
        return user_id in self.lock_table and (time.monotonic() - ts.get(user_id, 0)) < self.LOCK_TTL

    # ──────────────────────────────────────────────
    # ESCALATION HANDLER
    # ──────────────────────────────────────────────

    def handle_escalation(self, escalation_msg: dict, requesting_neg_id: str) -> List[dict]:
        """
        Given an ESCALATE message, return a list of candidate donor dicts
        from outside the bridge, ranked for a mini-round.

        Escalation ladder (spec §5.4):
        1. Compatible donors from low-stress sibling bridges (donor loan)
        2. Emergency/One-Time donors (conversion opportunity)
        3. Reactivation candidates
        """
        params       = escalation_msg.get("params", {})
        bridge_id    = params.get("bridge_id")
        blood_group  = params.get("blood_group", "")
        deficit      = params.get("deficit", 1)
        date_needed  = params.get("date")

        candidates = []

        # ── Step 1: Sibling bridge donor loans ──────────────────────
        for bid, bridge in self.bridges.items():
            if bid == bridge_id:
                continue
            # Only borrow from bridges with good health
            if bridge.get("health_label", "red") == "red":
                continue
            for member in bridge.get("roster", []):
                uid = member["user_id"]
                # Must be compatible, eligible, not locked
                if (member.get("compatible") or True) and \
                   member.get("eligible_now") and \
                   not self.is_locked(uid):
                    donor = self.donors.get(uid, member)
                    candidates.append({
                        **donor,
                        "source": "bridge_loan",
                        "loan_from": bid,
                    })
                if len(candidates) >= deficit * 3:
                    break
            if len(candidates) >= deficit * 3:
                break

        # ── Step 2: Emergency / One-Time pool ──────────────────────
        if len(candidates) < deficit:
            for uid, donor in self.donors.items():
                if donor.get("role") in {"Emergency Donor", "Guest"} and \
                   donor.get("eligible_now") and \
                   not self.is_locked(uid) and \
                   uid not in [c["user_id"] for c in candidates]:
                    candidates.append({**donor, "source": "emergency_pool"})
                if len(candidates) >= deficit * 4:
                    break

        # ── Step 3: Reactivation queue ──────────────────────────────
        if len(candidates) < deficit:
            for r in self.reactivation_queue:
                uid = r["user_id"]
                if not self.is_locked(uid) and \
                   uid not in [c["user_id"] for c in candidates]:
                    donor = self.donors.get(uid, {})
                    candidates.append({
                        **donor, **r,
                        "source": "reactivation",
                    })
                if len(candidates) >= deficit * 5:
                    break

        # Sort by propensity
        candidates.sort(key=lambda d: d.get("propensity", 0), reverse=True)
        return candidates[:deficit * 3]

    # ──────────────────────────────────────────────
    # CONFLICT ARBITRATION
    # ──────────────────────────────────────────────

    def arbitrate(self, neg_a: dict, neg_b: dict, contested_user_id: str) -> dict:
        """
        Two Guardians want the same donor.
        Priority = days_to_transfusion ASC, then units_needed DESC.
        Returns the winning neg dict and issues a debt token to the loser.
        """
        def priority_key(neg):
            return (neg.get("days_to_next_transfusion", 999),
                    -neg.get("units_needed", 1))

        if priority_key(neg_a) <= priority_key(neg_b):
            winner, loser = neg_a, neg_b
        else:
            winner, loser = neg_b, neg_a

        # Issue debt token
        loser_bridge = loser.get("bridge_id", "unknown")
        self.debt_ledger.setdefault(loser_bridge, []).append({
            "owed_by":   winner.get("bridge_id"),
            "donor_id":  contested_user_id,
            "issued_at": datetime.utcnow().isoformat(),
        })

        return {"winner": winner, "loser": loser, "debt_issued": True}

    def emit(self, action: str, params: dict, say: str) -> dict:
        return {
            "from":   "exchange:singleton",
            "action": action,
            "params": params,
            "say":    say,
        }
