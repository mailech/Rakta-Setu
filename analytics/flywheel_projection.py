"""
Module B — PREVENTION FLYWHEEL (spec §6B, Leak 4: demand elimination)

The mic-drop math, generated from the real CSV:
  screen X donors/yr at ~3.5% carrier rate
   -> Y carriers found
   -> at-risk couples informed (25% per-child risk between two carriers)
   -> each prevented birth ~= 500-700 transfusions that never need coordinating.

Plus a camp-placement heatmap (k-means on the 7,033 lat/longs):
  "place 5 screening camps here, reach N% of the active pool."

All carrier statuses are simulated at the real ~3-4% national rate and labeled as such.
"""
import json
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent.parent

# Population-level constants (labeled assumptions, not individual claims)
CARRIER_RATE          = 0.035   # ~3.5% national thalassemia carrier rate
TRANSFUSIONS_PER_LIFE = 600     # 500-700 per affected patient over a lifetime
COUPLE_RISK           = 0.25    # 25% chance per child when both parents are carriers


def _load_donors() -> list:
    return json.loads((ROOT / "data" / "donors.json").read_text(encoding="utf-8"))


def _load_bridges() -> list:
    return json.loads((ROOT / "data" / "bridges.json").read_text(encoding="utf-8"))


# ──────────────────────────────────────────────
# THE PAYOFF PROJECTION
# ──────────────────────────────────────────────

def projection(screened_per_year: Optional[int] = None) -> dict:
    """
    Project the flywheel payoff. If screened_per_year is None, default to the
    active donor pool size (everyone we already touch is a screening candidate).
    """
    donors  = _load_donors()
    bridges = _load_bridges()

    active = [d for d in donors
              if str(d.get("user_donation_active_status", "")).lower() == "active"]
    pool = len(active) if active else len(donors)
    screened = int(screened_per_year if screened_per_year is not None else pool)

    carriers_found = screened * CARRIER_RATE
    # Carrier-carrier couples: assume carriers pair into the general population at the
    # carrier rate again -> at-risk couples.
    at_risk_couples = carriers_found * CARRIER_RATE
    prevented_births = at_risk_couples * COUPLE_RISK
    transfusions_averted = prevented_births * TRANSFUSIONS_PER_LIFE

    # Demand grounding from real bridge data: quantity_required x cadence -> units/yr.
    per_patient_units_year = []
    for b in bridges:
        qty = float(b.get("quantity_required", 1) or 1)
        # cadence: days_to_next as a rough freq proxy if we lack frequency; assume monthly+
        per_patient_units_year.append(qty * 12)  # ~monthly transfusions
    avg_units_patient_year = (
        round(sum(per_patient_units_year) / len(per_patient_units_year), 1)
        if per_patient_units_year else 18.0
    )

    return {
        "carrier_rate":            CARRIER_RATE,
        "donor_pool":              pool,
        "screened_per_year":       screened,
        "carriers_found":          round(carriers_found, 1),
        "at_risk_couples":         round(at_risk_couples, 2),
        "prevented_births_per_yr": round(prevented_births, 2),
        "transfusions_averted_per_yr": round(transfusions_averted, 0),
        "avg_units_per_patient_year":  avg_units_patient_year,
        "transfusions_per_life":   TRANSFUSIONS_PER_LIFE,
        "note": "Carrier statuses simulated at the ~3.5% national rate; labeled as simulated.",
        # 10-year cumulative curve for the chart
        "cumulative_10yr": [
            {"year": y,
             "transfusions_averted": round(transfusions_averted * y, 0),
             "prevented_births": round(prevented_births * y, 2)}
            for y in range(1, 11)
        ],
    }


# ──────────────────────────────────────────────
# CAMP-PLACEMENT HEATMAP (k-means)
# ──────────────────────────────────────────────

def camp_placements(k: int = 5) -> dict:
    """
    k-means on donor lat/longs -> k camp centroids + coverage estimate.
    Coverage = fraction of donors within ~25 km of their nearest camp.
    """
    import numpy as np

    donors = _load_donors()
    pts = np.array([[d["latitude"], d["longitude"]] for d in donors
                    if d.get("latitude") and d.get("longitude")], dtype=float)
    if len(pts) < k:
        return {"camps": [], "coverage_pct": 0, "k": k, "n_points": len(pts)}

    try:
        from sklearn.cluster import KMeans
        km = KMeans(n_clusters=k, n_init=10, random_state=42)
        labels = km.fit_predict(pts)
        centers = km.cluster_centers_
    except Exception:
        # Fallback: simple grid-free pick of k spread points
        idx = np.linspace(0, len(pts) - 1, k).astype(int)
        centers = pts[idx]
        labels = np.zeros(len(pts), dtype=int)

    def haversine(a, b):
        R = 6371.0
        lat1, lon1, lat2, lon2 = map(np.radians, [a[0], a[1], b[0], b[1]])
        dlat, dlon = lat2 - lat1, lon2 - lon1
        h = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
        return R * 2 * np.arcsin(np.sqrt(h))

    # Coverage: donors within 25 km of nearest centroid
    covered = 0
    for p in pts:
        dmin = min(haversine(p, c) for c in centers)
        if dmin <= 25:
            covered += 1
    coverage_pct = round(100 * covered / len(pts), 1)

    camps = []
    for i, c in enumerate(centers):
        size = int((labels == i).sum())
        camps.append({
            "camp": i + 1,
            "lat":  round(float(c[0]), 5),
            "lon":  round(float(c[1]), 5),
            "donors_in_cluster": size,
        })
    camps.sort(key=lambda x: x["donors_in_cluster"], reverse=True)

    return {
        "camps": camps,
        "k": k,
        "n_points": len(pts),
        "coverage_pct": coverage_pct,
        "radius_km": 25,
    }


if __name__ == "__main__":
    print(json.dumps(projection(), indent=2))
    print(json.dumps(camp_placements(), indent=2))
