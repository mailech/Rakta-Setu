"""
Mock donor identities (name, age, DOB, blood group) + geo matching.

The real dataset has no names/ages/DOB, and its blood_group column is unusable
(format mismatch nulled it). For the demo we synthesize a *stable* identity per
donor (same user_id -> same person every time) and label it as simulated.
This is what powers the "donor details" panel and the blood-compatibility match.
"""
import hashlib
import json
import math
from pathlib import Path
from datetime import date

DATA = Path(__file__).parent

# Indian-name pools (demo only)
_FIRST_M = ["Aarav","Vikram","Rohan","Arjun","Karthik","Ramesh","Suresh","Naveen",
            "Imran","Joseph","Aditya","Sandeep","Manoj","Rahul","Kiran","Yusuf"]
_FIRST_F = ["Priya","Ananya","Sneha","Lakshmi","Fatima","Divya","Meera","Kavya",
            "Pooja","Anjali","Sara","Nandini","Reshma","Swathi","Aisha","Deepa"]
_LAST = ["Reddy","Sharma","Kumar","Rao","Nair","Patel","Khan","Singh","Iyer",
         "Verma","Gupta","Naidu","Pillai","Das","Menon","Shetty"]

# India blood-group distribution (approx, for plausible mock assignment)
_BG = (["O+"]*37 + ["B+"]*32 + ["A+"]*22 + ["AB+"]*7
       + ["O-"]*2 + ["B-"]*2 + ["A-"]*1 + ["AB-"]*1)

# Preferred language mix (demo, weighted for the Hyderabad/Telangana region) —
# powers the Amazon Translate "Rural Reach" feature. (code, display name)
_LANGS = ([("en", "English")]*8 + [("te", "Telugu")]*7 + [("hi", "Hindi")]*4
          + [("kn", "Kannada")]*1)

# Who can donate TO whom (donor group -> compatible recipient groups)
COMPAT = {
    "O-":["O-","O+","A-","A+","B-","B+","AB-","AB+"], "O+":["O+","A+","B+","AB+"],
    "A-":["A-","A+","AB-","AB+"], "A+":["A+","AB+"],
    "B-":["B-","B+","AB-","AB+"], "B+":["B+","AB+"],
    "AB-":["AB-","AB+"], "AB+":["AB+"],
}

# Normalize "O Positive" / "o positive" -> "O+"
_LONG = {"positive":"+","negative":"-","pos":"+","neg":"-"}


def normalize_bg(s):
    if not s:
        return None
    s = str(s).strip()
    if s in COMPAT:
        return s
    parts = s.replace("Do not Know", "").lower().split()
    if len(parts) >= 2 and parts[-1] in _LONG:
        letter = parts[0].upper().replace("AB", "AB")
        return f"{parts[0].upper()}{_LONG[parts[-1]]}"
    return None


def _seed(user_id: str) -> int:
    return int(hashlib.md5(str(user_id).encode()).hexdigest()[:8], 16)


def profile(donor: dict) -> dict:
    """Deterministic mock identity for a donor record."""
    uid = str(donor.get("user_id", ""))
    h = _seed(uid)
    gender = str(donor.get("gender", "")).lower()
    if gender.startswith("f"):
        first = _FIRST_F[h % len(_FIRST_F)]; g = "Female"
    elif gender.startswith("m"):
        first = _FIRST_M[h % len(_FIRST_M)]; g = "Male"
    else:
        pool = _FIRST_M + _FIRST_F
        first = pool[h % len(pool)]; g = "Male" if h % 2 else "Female"
    last = _LAST[(h // 7) % len(_LAST)]
    age = 18 + (h % 42)                       # 18..59
    # deterministic DOB
    yr = 2025 - age
    mo = 1 + (h // 13) % 12
    dy = 1 + (h // 31) % 28
    bg = normalize_bg(donor.get("blood_group")) or _BG[h % len(_BG)]
    lang_code, lang_name = _LANGS[(h // 17) % len(_LANGS)]
    return {
        "user_id": uid,
        "name": f"{first} {last}",
        "age": age,
        "dob": f"{yr:04d}-{mo:02d}-{dy:02d}",
        "gender": g,
        "blood_group": bg,
        "blood_group_simulated": normalize_bg(donor.get("blood_group")) is None,
        "preferred_language": lang_code,
        "language_name": lang_name,
        "phone": None,  # filled when a live phone is assigned
    }


def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1); dl = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return R * 2 * math.asin(math.sqrt(a))


def donors_within_radius(donors, lat, lon, radius_km, recipient_bg,
                         require_eligible=True, emergency=False):
    """
    Find donors within radius_km of (lat,lon) whose (mock) blood group can donate
    to recipient_bg. Emergency relaxes the eligibility gate. Returns enriched
    candidate dicts sorted by (compatible desc, distance asc, propensity desc).
    """
    recipient_bg = normalize_bg(recipient_bg) or recipient_bg
    out = []
    for d in donors:
        if d.get("latitude") is None or d.get("longitude") is None:
            continue
        dist = haversine(lat, lon, d["latitude"], d["longitude"])
        if dist > radius_km:
            continue
        prof = profile(d)
        compatible = recipient_bg in COMPAT.get(prof["blood_group"], [])
        if not compatible:
            continue
        eligible = bool(d.get("eligible_now"))
        if require_eligible and not emergency and not eligible:
            continue
        out.append({
            **d,
            "distance_km": round(dist, 2),
            "compatible": True,
            "profile": prof,
            "_rank": (0 if eligible else 1, dist, -float(d.get("propensity", 0))),
        })
    out.sort(key=lambda x: x["_rank"])
    return out
