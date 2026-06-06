"""
Guardian Agent — loyalty: THE PATIENT
One per bridge. Wakes at SIM_TODAY, ranks roster Proxies, runs negotiation rounds.
"""
import json
import random
from datetime import datetime
from typing import List, Optional

random.seed(42)

GUARDIAN_ACTIONS = {"REQUEST", "ACCEPT", "PROPOSE_ALTERNATIVE", "ESCALATE", "LOCK", "RELEASE"}


class GuardianAgent:
    """
    Fights for the patient's next transfusion.
    Manages the negotiation lifecycle for one bridge.
    """

    def __init__(self, bridge_record: dict, config: dict, llm_fn=None):
        self.bridge_id    = bridge_record["bridge_id"]
        self.bridge       = bridge_record
        self.config       = config
        self.llm          = llm_fn

        # Demand spec
        self.blood_group  = bridge_record.get("bridge_blood_group", "Unknown")
        self.units_needed = int(bridge_record.get("quantity_required", 1))
        self.next_date    = bridge_record.get("next_transfusion_date")
        self.roster       = bridge_record.get("roster", [])

        # Playbook (updated by failure learning, persisted per bridge — §6)
        try:
            from learning.lessons import get_playbook
            pb = get_playbook(self.bridge_id, config)
        except Exception:
            pb = {"lead_days": config.get("LEAD_DAYS", 7),
                  "overbook_factor": config.get("OVERBOOK_FACTOR", 1.5),
                  "rotation_depth": 3}
        self.lead_days       = pb["lead_days"]
        self.overbook_factor = pb["overbook_factor"]
        self.rotation_depth  = pb.get("rotation_depth", 3)

        # Alias for display
        bg = self.blood_group.replace(" ", "")
        self.alias = f"Guardian-{self.bridge_id[:6]}-{bg}"

        # State
        self.locked_slots: List[dict] = []
        self.playbook: dict = {}

    # ──────────────────────────────────────────────
    # ROSTER RANKING
    # ──────────────────────────────────────────────

    def rank_roster(self) -> List[dict]:
        """
        Rank roster Proxies by propensity × eligibility × (1 - fatigue).
        Returns sorted list (best first).
        """
        def score(d):
            elig = 1.0 if d.get("eligible_now") else max(0, 1 - d.get("days_to_eligible", 999) / 90)
            return d.get("propensity", 0) * elig * (1 - d.get("fatigue_score", 0))

        # All roster members are candidates (compatibility pre-checked in engine)
        candidates = list(self.roster)
        candidates.sort(key=score, reverse=True)
        return candidates

    def get_candidates(self) -> List[dict]:
        """Get top N candidates with overbooking factor."""
        ranked = self.rank_roster()
        # EMERGENCY: contact EVERY matched donor in the radius (no shortlist).
        if self.bridge.get("emergency"):
            return ranked
        n = int(self.units_needed * self.overbook_factor) + 1
        # rotation_depth (learned) widens the funnel after an OVERASKED_SEGMENT lesson
        return ranked[:max(n, self.rotation_depth)]  # always at least rotation_depth

    # ──────────────────────────────────────────────
    # MESSAGE BUILDERS
    # ──────────────────────────────────────────────

    def build_request(self, target_user_id: str) -> dict:
        """Build a REQUEST message for a specific Proxy."""
        return {
            "from":   f"guardian:{self.bridge_id}",
            "to":     f"proxy:{target_user_id}",
            "action": "REQUEST",
            "params": {
                "units":  self.units_needed,
                "date":   self.next_date,
                "window": "08:00-18:00",
                "blood_group_compatible": True,  # never reveals exact group needed
                # notice the donor actually gets = how early the Guardian wakes (§6).
                # Failure learning bumps lead_days -> more notice -> fewer declines.
                "notice_days": self.lead_days,
            },
            "say": (
                f"We need {self.units_needed} unit(s) of compatible blood "
                f"on or before {self.next_date}. Can your donor help?"
            )
        }

    def emit(self, action: str, params: dict, say: str, to: str = "exchange") -> dict:
        return {
            "from":   f"guardian:{self.bridge_id}",
            "to":     to,
            "action": action,
            "params": params,
            "say":    say,
            "meta":   {"alias": self.alias, "units_needed": self.units_needed}
        }

    # ──────────────────────────────────────────────
    # RESOLUTION (greedy assignment, Round 2)
    # ──────────────────────────────────────────────

    def resolve(self, responses: List[dict]) -> dict:
        """
        Given Proxy responses, greedily assign donors to cover units_needed.
        Returns: {accepted: [...], rejected: [...], coverage: int}
        """
        accepted = []
        rejected = []

        # Filter positive responses
        positives = [
            r for r in responses
            if r.get("action") in {"OFFER", "CONDITIONAL_OFFER", "REQUEST_HUMAN_CONFIRM"}
        ]

        # Sort by propensity (highest first) — from meta
        positives.sort(key=lambda r: r.get("meta", {}).get("propensity", 0), reverse=True)

        for r in positives:
            if len(accepted) < self.units_needed:
                accepted.append(r)
            else:
                rejected.append(r)

        coverage = len(accepted)
        return {
            "accepted": accepted,
            "rejected": rejected,
            "coverage": coverage,
            "sufficient": coverage >= self.units_needed
        }

    # ──────────────────────────────────────────────
    # POST-ROUND ACTIONS
    # ──────────────────────────────────────────────

    def lock_slot(self, user_id: str, date: str) -> dict:
        slot = {"user_id": user_id, "date": date, "locked_at": datetime.utcnow().isoformat()}
        self.locked_slots.append(slot)
        return self.emit("LOCK", slot, f"Slot locked for {user_id[:6]} on {date}", to=f"proxy:{user_id}")

    def escalate(self, deficit: int) -> dict:
        return self.emit(
            "ESCALATE",
            {"deficit": deficit, "bridge_id": self.bridge_id,
             "blood_group": self.blood_group, "date": self.next_date},
            f"Need {deficit} more unit(s). Requesting Exchange help.",
            to="exchange"
        )

    def update_playbook(self, patches: dict):
        """Apply failure-learning patches to playbook."""
        for k, v in patches.items():
            setattr(self, k, v)
            self.playbook[k] = v
