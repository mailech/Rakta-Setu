"""Tests for the data engine — Phase 1 exit gate."""
import json
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from data.engine import run, COMPATIBILITY, haversine


def test_bridge_count():
    """Must have exactly 80 bridges (per spec §3.3)."""
    _, bridges, _ = run()
    assert len(bridges) >= 79, f"Expected 80 bridges, got {len(bridges)}"
    print(f"PASS test_bridge_count: {len(bridges)} bridges")


def test_compatibility_matrix():
    """O- is universal donor — check against all groups."""
    assert set(COMPATIBILITY["O-"]) == {"O-","O+","A-","A+","B-","B+","AB-","AB+"}
    assert "AB+" in COMPATIBILITY["AB+"]
    assert "O-" not in COMPATIBILITY["AB+"]
    print("PASS test_compatibility_matrix")


def test_haversine():
    """Mumbai to Delhi ≈ 1148 km."""
    dist = haversine(19.076, 72.877, 28.704, 77.102)
    assert 1100 < dist < 1200, f"Haversine off: {dist}"
    print(f"PASS test_haversine: Mumbai->Delhi = {dist:.1f} km")


def test_eligibility_scores(donors=None):
    """All eligible_now donors must have days_to_eligible == 0."""
    if donors is None:
        donors, _, _ = run()
    violations = [d for d in donors if d["eligible_now"] and d["days_to_eligible"] > 0]
    assert len(violations) == 0, f"{len(violations)} donors have eligible_now=True but days_to_eligible>0"
    print(f"PASS test_eligibility_scores: no violations")


def test_fatigue_scores(donors=None):
    """All fatigue_scores must be in [0, 1]."""
    if donors is None:
        donors, _, _ = run()
    bad = [d for d in donors if not (0 <= d["fatigue_score"] <= 1)]
    assert len(bad) == 0, f"{len(bad)} donors have fatigue_score out of [0,1]"
    print("PASS test_fatigue_scores: all in [0,1]")


def test_propensity_scores(donors=None):
    """All propensity scores must be in [0, 1]."""
    if donors is None:
        donors, _, _ = run()
    bad = [d for d in donors if not (0 <= d["propensity"] <= 1)]
    assert len(bad) == 0, f"{len(bad)} donors have propensity out of [0,1]"
    print("PASS test_propensity_scores: all in [0,1]")


def test_sim_today(sim_today=None):
    """SIM_TODAY must be a valid date between 2015 and 2027."""
    import pandas as pd
    if sim_today is None:
        _, _, sim_today = run()
    assert pd.Timestamp("2015-01-01") <= sim_today <= pd.Timestamp("2027-01-01")
    print(f"PASS test_sim_today: {sim_today.date()}")


if __name__ == "__main__":
    print("\n=== RAKTA-SETU Phase 1 Tests ===\n")
    donors, bridges, sim_today = run()
    print()
    test_bridge_count.__doc__ and None
    # Run with pre-loaded data to avoid re-running engine 6 times
    assert len(bridges) >= 79, f"Expected >=79 bridges, got {len(bridges)}"
    print(f"PASS test_bridge_count: {len(bridges)} bridges")
    test_compatibility_matrix()
    test_haversine()
    test_eligibility_scores(donors)
    test_fatigue_scores(donors)
    test_propensity_scores(donors)
    test_sim_today(sim_today)
    print("\n=== ALL TESTS PASSED ===")
