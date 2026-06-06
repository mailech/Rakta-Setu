# RAKTA-SETU Data Engine
# Loads Dataset.csv, applies all cleaning rules from spec §3.2,
# computes derived scores from spec §3.3, emits donors.json + bridges.json

import random
import json
import math
import numpy as np
import pandas as pd
from pathlib import Path
from scipy.special import expit  # sigmoid

random.seed(42)
np.random.seed(42)

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
DATA_DIR   = Path(__file__).parent
CSV_PATH   = DATA_DIR.parent / "data" / "Dataset.csv"
# Fallback: look in parent-parent (AFG folder) if not in data/
if not CSV_PATH.exists():
    CSV_PATH = DATA_DIR.parent.parent / "Dataset.csv"
if not CSV_PATH.exists():
    CSV_PATH = Path(__file__).parent.parent.parent / "Dataset.csv"

OUT_DONORS  = DATA_DIR / "donors.json"
OUT_BRIDGES = DATA_DIR / "bridges.json"
OUT_CONFIG  = DATA_DIR / "config.json"

DATE_MIN = pd.Timestamp("2015-01-01")
DATE_MAX = pd.Timestamp("2026-12-31")

# ABO+Rh compatibility: donor blood group -> list of compatible recipient groups
COMPATIBILITY = {
    "O-":  ["O-","O+","A-","A+","B-","B+","AB-","AB+"],
    "O+":  ["O+","A+","B+","AB+"],
    "A-":  ["A-","A+","AB-","AB+"],
    "A+":  ["A+","AB+"],
    "B-":  ["B-","B+","AB-","AB+"],
    "B+":  ["B+","AB+"],
    "AB-": ["AB-","AB+"],
    "AB+": ["AB+"],
}

KNOWN_BLOOD_GROUPS = set(COMPATIBILITY.keys())


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def haversine(lat1, lon1, lat2, lon2):
    """Distance in km between two lat/lon points."""
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi  = math.radians(lat2 - lat1)
    dlam  = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return R * 2 * math.asin(math.sqrt(a))


def clip_date(ts):
    """Return NaT if ts outside [2015, 2026], else ts."""
    if pd.isna(ts):
        return pd.NaT
    if ts < DATE_MIN or ts > DATE_MAX:
        return pd.NaT
    return ts


def winsorize_series(s, upper_pct=0.99):
    cap = s.quantile(upper_pct)
    return s.clip(upper=cap)


def recency_factor(last_donation_date, sim_today):
    """0 = never donated, 1 = donated today."""
    if pd.isna(last_donation_date):
        return 0.0
    days_ago = (sim_today - last_donation_date).days
    return float(np.exp(-days_ago / 180))  # half-life 180 days


# ─────────────────────────────────────────────
# LOAD & CLEAN
# ─────────────────────────────────────────────

def load_and_clean(csv_path: Path) -> pd.DataFrame:
    print(f"[engine] Loading {csv_path} ...")
    df = pd.read_csv(csv_path, low_memory=False)
    print(f"[engine] Raw shape: {df.shape}")

    # 1. Drop exact duplicate user_id rows (keep first)
    before = len(df)
    df = df.drop_duplicates(subset="user_id", keep="first")
    print(f"[engine] Dropped {before - len(df)} duplicate user_ids -> {len(df)} rows")

    # 2. Parse date columns
    date_cols = [
        "last_transfusion_date", "expected_next_transfusion_date",
        "registration_date", "last_contacted_date", "last_donation_date",
        "next_eligible_date", "last_bridge_donation_date"
    ]
    for col in date_cols:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce").apply(clip_date)

    # 3. cycle_of_donations: clip negatives -> 0, winsorize at 99th pct
    df["cycle_of_donations"] = df["cycle_of_donations"].clip(lower=0)
    df["cycle_of_donations"] = winsorize_series(df["cycle_of_donations"])

    # 4. calls_to_donations_ratio: fix nulls where total_calls > 0 but donations_till_date null
    mask = (
        df["calls_to_donations_ratio"].isna() &
        (df["total_calls"] > 0) &
        df["donations_till_date"].isna()
    )
    df.loc[mask, "calls_to_donations_ratio"] = df.loc[mask, "total_calls"]
    # Fill remaining nulls with 0
    df["calls_to_donations_ratio"] = df["calls_to_donations_ratio"].fillna(0)
    df["donations_till_date"]      = df["donations_till_date"].fillna(0)
    df["total_calls"]              = df["total_calls"].fillna(0)

    # 5. blood_group: flag unverified
    df["unverified_blood_group"] = (
        df["blood_group"].isna() | (df["blood_group"] == "Do not Know")
    )
    df["blood_group"] = df["blood_group"].where(
        df["blood_group"].isin(KNOWN_BLOOD_GROUPS), other=None
    )

    # 6. Fill misc nulls
    df["latitude"]  = df["latitude"].fillna(df["latitude"].median())
    df["longitude"] = df["longitude"].fillna(df["longitude"].median())
    df["frequency_in_days"] = df["frequency_in_days"].fillna(90)

    print(f"[engine] Cleaning done. Shape: {df.shape}")
    return df


# ─────────────────────────────────────────────
# SIM_TODAY
# ─────────────────────────────────────────────

def compute_sim_today(df: pd.DataFrame) -> pd.Timestamp:
    valid_dates = df["expected_next_transfusion_date"].dropna()
    if valid_dates.empty:
        sim_today = pd.Timestamp("2025-06-07")
    else:
        sim_today = valid_dates.min() - pd.Timedelta(days=10)
    print(f"[engine] SIM_TODAY = {sim_today.date()}")
    return sim_today


# ─────────────────────────────────────────────
# DERIVED SCORES
# ─────────────────────────────────────────────

def compute_scores(df: pd.DataFrame, sim_today: pd.Timestamp) -> pd.DataFrame:
    print("[engine] Computing derived scores ...")

    # eligible_now
    df["eligible_now"] = (
        (df["eligibility_status"].str.lower() == "eligible") &
        (df["next_eligible_date"].notna()) &
        (df["next_eligible_date"] <= sim_today)
    )

    # days_to_eligible
    def dte(row):
        if pd.isna(row["next_eligible_date"]):
            return 999
        return max(0, (row["next_eligible_date"] - sim_today).days)
    df["days_to_eligible"] = df.apply(dte, axis=1)

    # recency
    df["recency_factor"] = df["last_donation_date"].apply(
        lambda d: recency_factor(d, sim_today)
    )

    # churn_risk (heuristic v1 — will be replaced by ML model in Phase 4)
    df["churn_risk_heuristic"] = (
        df["user_donation_active_status"].str.lower() == "inactive"
    ).astype(float)
    # Blend with high call ratio signal
    call_ratio_norm = (df["calls_to_donations_ratio"] / (df["calls_to_donations_ratio"].max() + 1e-9))
    df["churn_risk_heuristic"] = (0.6 * df["churn_risk_heuristic"] + 0.4 * call_ratio_norm).clip(0, 1)

    # propensity = sigmoid(1.2*log1p(donations) - 0.35*call_ratio - 0.4*churn_risk + 0.3*recency)
    propensity_raw = (
        1.2 * np.log1p(df["donations_till_date"])
        - 0.35 * df["calls_to_donations_ratio"]
        - 0.4  * df["churn_risk_heuristic"]
        + 0.3  * df["recency_factor"]
    )
    df["propensity"] = expit(propensity_raw).round(4)

    # fatigue = normalized calls_to_donations_ratio (proxy for call burden)
    max_cdr = df["calls_to_donations_ratio"].max()
    df["fatigue_score"] = (df["calls_to_donations_ratio"] / (max_cdr + 1e-9)).round(4)

    print(f"[engine] Eligible donors: {df['eligible_now'].sum()} / {len(df)}")
    return df


# ─────────────────────────────────────────────
# BUILD BRIDGES
# ─────────────────────────────────────────────

def build_bridges(df: pd.DataFrame, sim_today: pd.Timestamp) -> list:
    print("[engine] Building bridge records ...")
    bridge_donors = df[df["bridge_id"].notna()].copy()
    bridge_ids    = bridge_donors["bridge_id"].unique()
    bridges = []

    for bid in bridge_ids:
        bdf = bridge_donors[bridge_donors["bridge_id"] == bid]

        # Bridge-level fields (take from first patient/spec row)
        bg_row = bdf[bdf["bridge_blood_group"].notna()]
        bridge_blood_group = bg_row["bridge_blood_group"].iloc[0] if len(bg_row) else "Unknown"
        qty_row = bdf[bdf["quantity_required"].notna()]
        quantity_required = float(qty_row["quantity_required"].iloc[0]) if len(qty_row) else 1.0

        # Next transfusion date
        nxt_row = bdf[bdf["expected_next_transfusion_date"].notna()]
        next_transfusion = nxt_row["expected_next_transfusion_date"].min() if len(nxt_row) else pd.NaT

        # Centroid
        centroid_lat = bdf["latitude"].mean()
        centroid_lon = bdf["longitude"].mean()

        # Roster: anyone in the bridge who could donate (exclude Patient role)
        donor_mask = ~bdf["role"].isin(["Patient"])
        roster_df  = bdf[donor_mask]

        # Per-donor: add distance to centroid
        roster = []
        for _, row in roster_df.iterrows():
            dist = haversine(row["latitude"], row["longitude"], centroid_lat, centroid_lon)
            compatible = False
            if row["blood_group"] and bridge_blood_group in KNOWN_BLOOD_GROUPS:
                compatible = bridge_blood_group in COMPATIBILITY.get(row["blood_group"], [])
            roster.append({
                "user_id":            str(row["user_id"]),
                "blood_group":        row["blood_group"],
                "unverified":         bool(row["unverified_blood_group"]),
                "compatible":         compatible,
                "eligible_now":       bool(row["eligible_now"]),
                "days_to_eligible":   int(row["days_to_eligible"]),
                "propensity":         float(row["propensity"]),
                "fatigue_score":      float(row["fatigue_score"]),
                "churn_risk":         float(row["churn_risk_heuristic"]),
                "distance_km":        round(dist, 2),
            })

        # Bridge health = eligible supply (next 90 days) vs demand
        eligible_soon = sum(
            1 for d in roster
            if d["eligible_now"] or d["days_to_eligible"] <= 90
        )
        demand_90d = (90 / max(1, 30)) * quantity_required  # rough monthly demand
        health_score = round(min(1.0, eligible_soon / max(1, demand_90d)), 3)
        if health_score >= 0.7:
            health_label = "green"
        elif health_score >= 0.4:
            health_label = "amber"
        else:
            health_label = "red"

        days_to_next = (
            (next_transfusion - sim_today).days
            if pd.notna(next_transfusion) else 999
        )

        bridges.append({
            "bridge_id":            str(bid),
            "bridge_blood_group":   bridge_blood_group,
            "quantity_required":    quantity_required,
            "next_transfusion_date": next_transfusion.isoformat() if pd.notna(next_transfusion) else None,
            "days_to_next_transfusion": int(days_to_next),
            "centroid_lat":         round(centroid_lat, 5),
            "centroid_lon":         round(centroid_lon, 5),
            "roster":               roster,
            "roster_size":          len(roster),
            "health_score":         health_score,
            "health_label":         health_label,
        })

    bridges.sort(key=lambda b: b["days_to_next_transfusion"])
    print(f"[engine] Built {len(bridges)} bridges.")
    return bridges


# ─────────────────────────────────────────────
# BUILD DONORS (full export for Proxy state init)
# ─────────────────────────────────────────────

def build_donors(df: pd.DataFrame) -> list:
    print("[engine] Building donor records ...")
    donors = []
    for _, row in df.iterrows():
        # Seed preference_rules from CSV signals
        max_asks = 1 if row["calls_to_donations_ratio"] > 10 else (2 if row["calls_to_donations_ratio"] > 5 else 3)
        advance_days = 3 if row["donations_till_date"] >= 5 else 2

        donor = {
            "user_id":              str(row["user_id"]),
            "role":                 str(row["role"]) if pd.notna(row["role"]) else "Guest",
            "blood_group":          row["blood_group"],
            "unverified":           bool(row["unverified_blood_group"]),
            "gender":               str(row["gender"]) if pd.notna(row["gender"]) else "Unknown",
            "latitude":             float(row["latitude"]),
            "longitude":            float(row["longitude"]),
            "bridge_id":            str(row["bridge_id"]) if pd.notna(row["bridge_id"]) else None,
            "donor_type":           str(row["donor_type"]) if pd.notna(row["donor_type"]) else "One-Time",
            # Eligibility
            "eligibility_status":   str(row["eligibility_status"]) if pd.notna(row["eligibility_status"]) else "unknown",
            "eligible_now":         bool(row["eligible_now"]),
            "days_to_eligible":     int(row["days_to_eligible"]),
            "next_eligible_date":   row["next_eligible_date"].isoformat() if pd.notna(row["next_eligible_date"]) else None,
            "last_donation_date":   row["last_donation_date"].isoformat() if pd.notna(row["last_donation_date"]) else None,
            # Scores
            "propensity":           float(row["propensity"]),
            "fatigue_score":        float(row["fatigue_score"]),
            "churn_risk":           float(row["churn_risk_heuristic"]),
            "recency_factor":       float(row["recency_factor"]),
            # Raw signals
            "donations_till_date":  int(row["donations_till_date"]),
            "total_calls":          int(row["total_calls"]),
            "calls_to_donations_ratio": float(row["calls_to_donations_ratio"]),
            "frequency_in_days":    int(row["frequency_in_days"]),
            "user_donation_active_status": str(row["user_donation_active_status"]) if pd.notna(row["user_donation_active_status"]) else "Active",
            "inactive_trigger_comment": str(row["inactive_trigger_comment"]) if pd.notna(row["inactive_trigger_comment"]) else None,
            # Proxy preference_rules (seeded from CSV signals — disclosed as synthetic)
            "preference_rules": {
                "channels":             ["whatsapp"],
                "no_contact_hours":     [22, 8],
                "advance_notice_days":  advance_days,
                "blackout_months":      [],
                "max_asks_per_month":   max_asks,
            },
            # Proxy learned_policy (starts empty, updated by failure learning)
            "learned_policy": {
                "declined_morning_slots": 0,
                "accepts_weekends":       True,
                "best_channel":           "whatsapp",
            },
        }
        donors.append(donor)

    print(f"[engine] Built {len(donors)} donor records.")
    return donors


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def run():
    df = load_and_clean(CSV_PATH)
    sim_today = compute_sim_today(df)
    df = compute_scores(df, sim_today)

    donors  = build_donors(df)
    bridges = build_bridges(df, sim_today)

    # Write outputs
    OUT_DONORS.write_text(json.dumps(donors, indent=2, default=str), encoding="utf-8")
    OUT_BRIDGES.write_text(json.dumps(bridges, indent=2, default=str), encoding="utf-8")
    OUT_CONFIG.write_text(json.dumps({
        "SIM_TODAY": sim_today.isoformat(),
        "LEAD_DAYS": 7,
        "OVERBOOK_FACTOR": 1.5,
        "MAX_ROUNDS": 3,
        "ROUND_TIMEOUT_SECS": 30,
    }, indent=2), encoding="utf-8")

    print(f"\n[engine] DONE Done!")
    print(f"  donors.json  -> {len(donors)} records")
    print(f"  bridges.json -> {len(bridges)} bridges")
    print(f"  SIM_TODAY    = {sim_today.date()}")
    print("\n[engine] Bridge health summary:")
    green = sum(1 for b in bridges if b["health_label"] == "green")
    amber = sum(1 for b in bridges if b["health_label"] == "amber")
    red   = sum(1 for b in bridges if b["health_label"] == "red")
    print(f"  [G] Green: {green}  [A] Amber: {amber}  [R] Red: {red}")
    print("\n[engine] Top 5 urgent bridges:")
    for b in bridges[:5]:
        print(f"  {b['bridge_id']} | {b['bridge_blood_group']} | need {b['quantity_required']}u | in {b['days_to_next_transfusion']}d | health={b['health_label']} | roster={b['roster_size']}")

    return donors, bridges, sim_today


if __name__ == "__main__":
    run()
