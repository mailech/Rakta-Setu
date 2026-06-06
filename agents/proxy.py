"""
Proxy Agent — loyalty: THE DONOR
One instance per donor, instantiated per negotiation from their record.
Enforces grammar protocol. All hard rules are code-enforced (no LLM for safety gates).
"""
import json
import uuid
import random
from datetime import datetime
from typing import Optional

random.seed(42)

# Grammar actions the Proxy may emit
PROXY_ACTIONS = {
    "OFFER", "CONDITIONAL_OFFER", "DECLINE", "COUNTER",
    "REQUEST_HUMAN_CONFIRM", "WITHDRAW"
}

# Decline reasons (never reveals specifics to Guardian)
DECLINE_REASONS = {
    "ineligible": "Not available at this time.",
    "unavailable": "Unavailable for this window.",
    "protected":   "Invoking donor protection.",   # fatigue shield — reason never disclosed
}

# Canned say-lines for rule-based responses (theater without LLM cost)
SAY_LINES = {
    "DECLINE_ineligible":   "My donor isn't eligible for this window — please check back later.",
    "DECLINE_protected":    "My donor is invoking protection — passing on this one.",
    "DECLINE_unavailable":  "My donor isn't available for this slot.",
    "OFFER":                "My donor is available and ready to help.",
    "REQUEST_HUMAN_CONFIRM":"Checking in with my donor — one moment.",
    "COUNTER":              "My donor can do it, but needs a different slot.",
    "WITHDRAW":             "Withdrawing — my donor is stepping aside for now.",
}


class ProxyAgent:
    """
    Represents a single donor in a negotiation.
    Instantiated fresh each time from the donor's DB record.
    """

    def __init__(self, donor_record: dict, llm_fn=None):
        self.user_id    = donor_record["user_id"]
        self.alias      = f"Donor-{self.user_id[:6]}"
        self.record     = donor_record
        self.llm        = llm_fn  # injected; None = rule-based only

        # Constraints (hard — code enforced)
        self.eligible_now      = donor_record.get("eligible_now", False)
        self.days_to_eligible  = donor_record.get("days_to_eligible", 999)
        self.fatigue_score     = donor_record.get("fatigue_score", 0.0)
        self.propensity        = donor_record.get("propensity", 0.0)
        self.blood_group       = donor_record.get("blood_group")
        self.unverified        = donor_record.get("unverified", False)

        # Preferences (seeded from CSV signals — disclosed as synthetic)
        prefs = donor_record.get("preference_rules", {})
        self.max_asks_per_month  = prefs.get("max_asks_per_month", 2)
        self.advance_notice_days = prefs.get("advance_notice_days", 2)
        self.no_contact_hours    = prefs.get("no_contact_hours", [22, 8])
        self.blackout_months     = prefs.get("blackout_months", [])

        # Learned policy (updated by failure learning)
        self.learned_policy = donor_record.get("learned_policy", {})
        self.negotiation_memory = donor_record.get("negotiation_memory", [])

        # Consent
        self.consent = donor_record.get("consent", {
            "scope": "availability_negotiation",
            "granted_at": datetime.utcnow().isoformat(),
            "revocable": True,
            "revoked": False
        })

    # ──────────────────────────────────────────────
    # HARD GATES (code-enforced, no LLM)
    # ──────────────────────────────────────────────

    def _consent_active(self) -> bool:
        return not self.consent.get("revoked", False)

    def _check_fatigue(self) -> bool:
        """Returns True if fatigue protection should trigger."""
        return self.fatigue_score > 0.7

    def _check_eligibility(self) -> bool:
        """Returns True if donor is currently eligible."""
        return self.eligible_now

    def _check_advance_notice(self, request: dict) -> bool:
        """
        Returns True if enough advance notice is given.
        Notice = how early the Guardian woke and asked (request.params.notice_days),
        i.e. the gap between the ask and the transfusion. This is what failure
        learning improves: bump the Guardian's lead_days -> more notice (§6).
        """
        params = request.get("params", {})
        notice_days = params.get("notice_days")
        if notice_days is not None:
            try:
                return int(notice_days) >= self.advance_notice_days
            except (TypeError, ValueError):
                return True
        # Fallback: derive notice from (date - SIM_TODAY) if notice_days absent
        try:
            from pathlib import Path
            import json
            config_path = Path(__file__).parent.parent / "data" / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            sim_today = datetime.fromisoformat(config["SIM_TODAY"]).date()
            donation_date = datetime.fromisoformat(params.get("date")).date()
            return (donation_date - sim_today).days >= self.advance_notice_days
        except Exception:
            return True  # if we can't parse, don't block

    # ──────────────────────────────────────────────
    # RESPOND TO A REQUEST
    # ──────────────────────────────────────────────

    def respond(self, request: dict) -> dict:
        """
        Given a REQUEST from Guardian, return one grammar action.
        Hard rules checked first (no LLM cost).
        LLM only called for judgment-zone decisions (conditional offers, counters).
        """
        if not self._consent_active():
            return self._emit("WITHDRAW", {}, "My donor has revoked consent — withdrawing.")

        # Hard gate 1: eligibility
        if not self._check_eligibility():
            return self._emit("DECLINE", {"reason": "ineligible"},
                              SAY_LINES["DECLINE_ineligible"])

        # Hard gate 2: fatigue protection
        if self._check_fatigue():
            return self._emit("DECLINE", {"reason": "protected"},
                              SAY_LINES["DECLINE_protected"])

        # Soft check: advance notice
        date_requested = request.get("params", {}).get("date")
        if not self._check_advance_notice(request):
            # Short notice — counter with a later date if LLM available, else decline
            if self.llm:
                return self._llm_respond(request, context="short_notice")
            else:
                # Rule-based counter: push date by advance_notice_days
                return self._emit("COUNTER", {"reason": "needs_more_notice"},
                                  f"My donor needs at least {self.advance_notice_days} days notice.")

        # Judgment zone: if LLM available, use persona
        if self.llm:
            return self._llm_respond(request)

        # Rule-based happy path: OFFER or REQUEST_HUMAN_CONFIRM
        return self._emit("REQUEST_HUMAN_CONFIRM",
                          {"date": date_requested, "donor": self.alias},
                          SAY_LINES["REQUEST_HUMAN_CONFIRM"])

    def _llm_respond(self, request: dict, context: str = "") -> dict:
        """Use LLM persona for judgment-zone decisions."""
        system = (
            f"You are the personal representative of donor {self.alias}. "
            f"You protect their time, privacy and goodwill. "
            f"Facts you may use: eligible={self.eligible_now}, "
            f"fatigue={self.fatigue_score:.2f}, propensity={self.propensity:.2f}, "
            f"advance_notice_days={self.advance_notice_days}, "
            f"learned_policy={json.dumps(self.learned_policy)}. "
            f"Context: {context}. "
            f"You may ONLY respond with one action from: "
            f"OFFER, CONDITIONAL_OFFER, DECLINE, COUNTER, REQUEST_HUMAN_CONFIRM, WITHDRAW. "
            f"Respond as JSON: {{\"action\": \"...\", \"params\": {{}}, \"say\": \"one sentence in character\"}}. "
            f"NEVER reveal WHY your donor is unavailable. NEVER agree to anything violating their preference_rules."
        )
        user = f"Guardian REQUEST: {json.dumps(request)}"
        try:
            result = self.llm(system, user, json_mode=True)
            action = result.get("action", "DECLINE")
            if action not in PROXY_ACTIONS:
                action = "DECLINE"
            return self._emit(action, result.get("params", {}), result.get("say", "..."))
        except Exception as e:
            # Fallback to rule-based on LLM error
            return self._emit("REQUEST_HUMAN_CONFIRM",
                              {"date": request.get("params", {}).get("date")},
                              SAY_LINES["REQUEST_HUMAN_CONFIRM"])

    # Why the Proxy decided what it did — surfaced to the console so judges can SEE
    # the agent reasoning over the donor's real data (location + condition + history).
    _REASONS = {
        ("DECLINE", "ineligible"): "Inside the 90-day eligibility window",
        ("DECLINE", "protected"):  "Fatigue protection (reason withheld from Guardian)",
        ("DECLINE", "unavailable"):"Donor unavailable for this slot",
        ("COUNTER", "needs_more_notice"): "Not enough advance notice for this donor",
        ("WITHDRAW", None):        "Consent revoked / stepping aside",
    }

    def _reason_for(self, action: str, params: dict) -> str:
        key = (action, params.get("reason"))
        if key in self._REASONS:
            return self._REASONS[key]
        if action in ("OFFER", "CONDITIONAL_OFFER", "REQUEST_HUMAN_CONFIRM"):
            bits = []
            if self.eligible_now: bits.append("eligible now")
            if self.record.get("distance_km") is not None:
                bits.append(f"{round(self.record['distance_km'])}km away")
            bits.append(f"propensity {self.propensity:.2f}")
            return "Available — " + ", ".join(bits)
        return ""

    def _emit(self, action: str, params: dict, say: str) -> dict:
        """Build a grammar-conformant response message (with reasoning metadata)."""
        return {
            "from":   f"proxy:{self.user_id}",
            "action": action,
            "params": params,
            "say":    say,
            "meta": {
                "alias":        self.alias,
                "fatigue":      round(self.fatigue_score, 3),
                "propensity":   round(self.propensity, 3),
                "eligible":     self.eligible_now,
                "days_to_eligible": self.days_to_eligible,
                "distance_km":  self.record.get("distance_km"),
                "gender":       self.record.get("gender"),
                "donations":    self.record.get("donations_till_date"),
                "reason":       self._reason_for(action, params),
            }
        }

    def update_memory(self, neg_id: str, outcome: str, lesson: str = ""):
        """Called after negotiation closes — persist learning."""
        self.negotiation_memory.append({
            "neg_id":  neg_id,
            "outcome": outcome,
            "lesson":  lesson,
            "ts":      datetime.utcnow().isoformat()
        })
        # Keep last 10 memories
        self.negotiation_memory = self.negotiation_memory[-10:]
