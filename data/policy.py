"""
Donor-contact policy — the conditions under which a donor's agent will actually be
contacted (and escalated to a call). This is the demonstrable "agent rules" layer:
the Proxy/Guardian respect these thresholds, so you can show on stage exactly why a
donor was or wasn't disturbed.

Editable live from the UI (Setup tab) or here. Persisted to data/policy_cfg.json.
"""
import json
from pathlib import Path

_F = Path(__file__).parent / "policy_cfg.json"

policy = {
    "max_distance_km":  10.0,   # only contact donors within this radius
    "only_eligible":    True,   # skip donors inside the 90-day window (emergency overrides)
    "max_fatigue":      0.7,    # skip donors more fatigued than this (donor protection)
    "min_propensity":   0.0,    # skip donors below this willingness score
    "enable_call":      True,   # escalate to a phone call if SMS ignored
    "escalate_after":   20,     # seconds before the call
}

if _F.exists():
    try:
        for k, v in json.loads(_F.read_text(encoding="utf-8")).items():
            if k in policy and v is not None:
                policy[k] = v
    except Exception:
        pass


def get_policy() -> dict:
    return dict(policy)


def set_policy(**kw) -> dict:
    for k, v in kw.items():
        if k in policy and v is not None:
            policy[k] = v
    try:
        _F.write_text(json.dumps(policy, indent=2), encoding="utf-8")
    except Exception:
        pass
    return get_policy()


def passes(donor: dict, emergency: bool = False) -> bool:
    """Does this donor meet the contact conditions? (distance handled in matching.)"""
    if float(donor.get("fatigue_score", 0)) > policy["max_fatigue"]:
        return False
    if float(donor.get("propensity", 0)) < policy["min_propensity"]:
        return False
    if policy["only_eligible"] and not emergency and not donor.get("eligible_now"):
        return False
    return True
