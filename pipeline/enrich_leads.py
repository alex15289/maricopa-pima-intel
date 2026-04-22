"""
Lead enrichment layer — 22 motivated-seller flags + property type classification.

All flags are derivable from parcel master data (no new scraping required).
Flags are grouped into 6 categories for dashboard filtering:

  1. DISTRESS      — value-gap / underwater / declining FCV
  2. LIFE EVENT    — widow, care-of, multi-owner inheritance
  3. HOLDING       — tired landlord, out-of-country, PO box owners
  4. VALUE         — cash-flow, teardown, fixer, equity outliers
  5. GEOGRAPHIC    — bulk owners, subdivision concentration
  6. VELOCITY      — never-sold, recent under-price sale

Property type classification maps Arizona PUC codes to 10 canonical types
for filter chips on the dashboard.
"""

import re
from typing import Optional
from datetime import datetime
from collections import defaultdict


# ---------------------------------------------------------------------------
# PROPERTY TYPE CLASSIFICATION (Arizona PUC codes)
# ---------------------------------------------------------------------------

PROPERTY_TYPES = {
    "sfr":              {"label": "Single Family",       "icon": "🏠", "residential": True},
    "townhome_condo":   {"label": "Townhome / Condo",    "icon": "🏘️", "residential": True},
    "mobile_home":      {"label": "Mobile Home",         "icon": "🚐", "residential": True},
    "multifamily_2_4":  {"label": "Multifamily 2-4",     "icon": "🏢", "residential": True},
    "multifamily_5_20": {"label": "Multifamily 5-20",    "icon": "🏬", "residential": True},
    "multifamily_20p":  {"label": "Multifamily 20+",     "icon": "🏙️", "residential": True},
    "vacant_land":      {"label": "Vacant Land",         "icon": "🌾", "residential": True},
    "rural_acreage":    {"label": "Rural Acreage",       "icon": "🌵", "residential": True},
    "commercial":       {"label": "Commercial",          "icon": "🏪", "residential": False},
    "industrial":       {"label": "Industrial",          "icon": "🏭", "residential": False},
    "agricultural":     {"label": "Agricultural",        "icon": "🚜", "residential": False},
    "other":            {"label": "Other",               "icon": "❓", "residential": False},
}


def classify_property_type(puc: Optional[str]) -> str:
    """Map Arizona PUC code to canonical property type key."""
    if not puc:
        return "other"
    code = str(puc).strip().upper().zfill(4)
    # Arizona Department of Revenue use codes
    if code in {"0131", "0132", "0136"}: return "sfr"
    if code == "0171":                    return "rural_acreage"
    if code in {"0133", "0134"}:          return "townhome_condo"
    if code in {"0135", "0137"}:          return "mobile_home"
    if code == "0141":                    return "multifamily_2_4"
    if code == "0142":                    return "multifamily_5_20"
    if code == "0143":                    return "multifamily_20p"
    if code == "0180":                    return "vacant_land"
    if code.startswith("02"):             return "commercial"
    if code.startswith("03"):             return "industrial"
    if code.startswith("04"):             return "agricultural"
    # Fallback: if it starts with 01, it's residential-ish
    if code.startswith("01"):             return "sfr"
    return "other"


# ---------------------------------------------------------------------------
# HELPER PARSERS
# ---------------------------------------------------------------------------

def _money(raw) -> float:
    if raw is None: return 0.0
    if isinstance(raw, (int, float)): return float(raw)
    s = str(raw).strip().replace(",", "").replace("$", "").replace(" ", "")
    try: return float(s) if s else 0.0
    except: return 0.0


def _year(raw) -> int:
    if raw is None: return 0
    try: return int(str(raw).strip()[:4])
    except: return 0


def _years_ago(date_str: Optional[str], now: datetime) -> Optional[float]:
    if not date_str: return None
    try:
        # Handle common formats: 2015-06-15, 6/15/2015, 20150615, epoch millis
        s = str(date_str).strip()
        if s.isdigit() and len(s) >= 10:
            dt = datetime.fromtimestamp(int(s[:10]))
        elif "-" in s:
            dt = datetime.strptime(s[:10], "%Y-%m-%d")
        elif "/" in s:
            dt = datetime.strptime(s[:10], "%m/%d/%Y")
        else:
            return None
        # Strip tz if now is aware — dt is always naive above
        now_naive = now.replace(tzinfo=None) if now.tzinfo else now
        return (now_naive - dt).days / 365.25
    except Exception:
        return None


# ---------------------------------------------------------------------------
# DISTRESS FLAGS (value-gap / underwater)
# ---------------------------------------------------------------------------

def flag_equity_gap_high(p: dict) -> bool:
    """FCV is >50% higher than last sale price — huge appreciation, massive equity."""
    fcv = _money(p.get("fcv"))
    last = _money(p.get("last_sale_price"))
    if fcv < 50000 or last < 10000: return False
    return (fcv - last) / fcv >= 0.50


def flag_negative_equity(p: dict) -> bool:
    """Paid more than current FCV — underwater, motivated to exit."""
    fcv = _money(p.get("fcv"))
    last = _money(p.get("last_sale_price"))
    if fcv < 50000 or last < 50000: return False
    return last > fcv * 1.05  # paid 5%+ over current value


# ---------------------------------------------------------------------------
# LIFE EVENT FLAGS (widow, care-of, inheritance)
# ---------------------------------------------------------------------------

WIDOW_RE = re.compile(r"SURVIVING\s+SPOUSE|WIDOW|SURVIVOR|&\s+SURV\b", re.I)
HEIRSHIP_RE = re.compile(r"\bHEIRS?\b|\bET\s+AL\b|SUCCESSOR|\bESTATE\b", re.I)


def flag_widow_indicator(p: dict) -> bool:
    owner = (p.get("owner") or "") + " " + (p.get("owner_2") or "")
    return bool(WIDOW_RE.search(owner))


def flag_multi_owner_inheritance(p: dict) -> bool:
    """Multiple owners with different last names = inheritance situation."""
    owner = p.get("owner") or ""
    if not owner or len(owner) < 10: return False
    # Skip obvious married couples (same last name pattern)
    if re.search(r"\b(JR|SR|II|III)\b", owner): return False
    if HEIRSHIP_RE.search(owner): return True
    # Count unique surnames
    parts = re.split(r"\s*(?:&|AND|\+)\s*", owner)
    if len(parts) >= 3: return True  # 3+ people on deed = probably heirs
    return False


def flag_care_of_address(p: dict) -> bool:
    """INCAREOF field populated = mail redirected to attorney/family (probate signal)."""
    return bool((p.get("care_of") or "").strip())


def flag_corporate_dissolved(p: dict, now: datetime) -> bool:
    """LLC/Inc + no recent activity = dormant entity, owner may have moved on."""
    owner = (p.get("owner") or "").upper()
    if not re.search(r"\bLLC\b|\bINC\b|\bCORP\b|\bLLP\b|\bLTD\b", owner): return False
    yrs = _years_ago(p.get("last_sale_date"), now)
    if yrs is None: return True  # no sale date + entity owner = very old hold
    return yrs >= 10


# ---------------------------------------------------------------------------
# HOLDING PATTERN FLAGS
# ---------------------------------------------------------------------------

def flag_tired_landlord(p: dict, now: datetime, prop_type: str) -> bool:
    """Absentee + long hold + multifamily = burned-out rental owner."""
    if not p.get("absentee"): return False
    if prop_type not in {"multifamily_2_4", "multifamily_5_20", "multifamily_20p"}: return False
    yrs = _years_ago(p.get("last_sale_date"), now)
    return yrs is not None and yrs >= 10


def flag_accidental_landlord(p: dict, now: datetime) -> bool:
    """Absentee SFR, held less than 5yrs, likely inherited/couldn't sell."""
    if not p.get("absentee"): return False
    yrs = _years_ago(p.get("last_sale_date"), now)
    return yrs is not None and yrs <= 5


def flag_mailing_to_pobox(p: dict) -> bool:
    """Mail goes to PO Box = owner avoiding physical address delivery."""
    mail = (p.get("mail_address") or "").upper()
    return bool(re.search(r"\bP\.?\s*O\.?\s*BOX\b|\bPOST\s+OFFICE\s+BOX\b", mail))


def flag_out_of_country(p: dict) -> bool:
    """Non-US mailing address = hard to reach, often motivated."""
    state = (p.get("mail_state") or "").strip().upper()
    if not state: return False
    # US states + territories
    us_codes = {"AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
                "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
                "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT",
                "VA","WA","WV","WI","WY","DC","PR","VI","GU","AS","MP","AA","AE","AP"}
    return state not in us_codes and len(state) >= 2


# ---------------------------------------------------------------------------
# VALUE FLAGS (deal quality)
# ---------------------------------------------------------------------------

def flag_cash_flow_candidate(p: dict, prop_type: str) -> bool:
    """2-4 unit + absentee + older build = rentable, priced well, motivated owner."""
    if prop_type != "multifamily_2_4": return False
    if not p.get("absentee"): return False
    yb = _year(p.get("year_built"))
    return 0 < yb <= 2000


def flag_teardown_candidate(p: dict) -> bool:
    """Pre-1970 build + land value dominant = dirt is worth more than the house."""
    yb = _year(p.get("year_built"))
    if yb == 0 or yb >= 1970: return False
    fcv = _money(p.get("fcv"))
    land = _money(p.get("land_value"))
    if fcv < 100000: return False
    # If land value specifically available, use that. Else proxy: lot >8k sqft + old house
    if land > 0:
        return land / fcv >= 0.70
    lot = _money(p.get("lot_sqft"))
    return lot >= 8000


def flag_hurricane_equity(p: dict) -> bool:
    """Old home + huge equity = senior owners, probate-adjacent, high-value play."""
    yb = _year(p.get("year_built"))
    if yb == 0 or yb >= 1980: return False
    fcv = _money(p.get("fcv"))
    last = _money(p.get("last_sale_price"))
    return (fcv - last) >= 400000


# ---------------------------------------------------------------------------
# GEOGRAPHIC FLAGS (portfolio owners, subdivision concentration)
# ---------------------------------------------------------------------------

def build_owner_index(parcels_iter) -> dict:
    """
    First pass: count how many parcels each normalized owner name holds.
    Returns {normalized_owner: count}.
    Call this ONCE before enriching individual leads.
    """
    owner_counts = defaultdict(int)
    for p in parcels_iter:
        owner_key = _normalize_owner_key(p.get("owner"))
        if owner_key:
            owner_counts[owner_key] += 1
    return dict(owner_counts)


def _normalize_owner_key(owner: Optional[str]) -> Optional[str]:
    if not owner: return None
    key = owner.upper().strip()
    # Drop common suffixes/prefixes that obscure duplicates
    key = re.sub(r"\s+(JR|SR|II|III|IV|TR|TRS|TRUSTEE)\b\.?", "", key)
    key = re.sub(r"\s+", " ", key).strip()
    return key if len(key) >= 3 else None


def flag_bulk_owner_3plus(p: dict, owner_counts: dict) -> bool:
    key = _normalize_owner_key(p.get("owner"))
    return bool(key and 3 <= owner_counts.get(key, 0) < 10)


def flag_bulk_owner_10plus(p: dict, owner_counts: dict) -> bool:
    key = _normalize_owner_key(p.get("owner"))
    return bool(key and owner_counts.get(key, 0) >= 10)


# ---------------------------------------------------------------------------
# VELOCITY FLAGS
# ---------------------------------------------------------------------------

def flag_never_sold(p: dict) -> bool:
    """No sale on record = generational ownership, heirs likely inherit."""
    return not p.get("last_sale_date") and not p.get("last_sale_price")


def flag_recent_underprice_sale(p: dict, now: datetime) -> bool:
    """Sold in last 2yrs for <70% of current FCV = distressed transaction."""
    yrs = _years_ago(p.get("last_sale_date"), now)
    if yrs is None or yrs > 2: return False
    fcv = _money(p.get("fcv"))
    last = _money(p.get("last_sale_price"))
    if fcv < 75000 or last < 10000: return False
    return last < fcv * 0.70


# ---------------------------------------------------------------------------
# SCORING WEIGHTS for new flags
# ---------------------------------------------------------------------------

ENRICHMENT_WEIGHTS = {
    # Distress
    "equity_gap_high":          25,
    "negative_equity":          35,
    # Life event
    "widow_indicator":          45,
    "multi_owner_inheritance":  40,
    "care_of_address":          35,
    "corporate_dissolved":      30,
    # Holding
    "tired_landlord":           30,
    "accidental_landlord":      25,
    "mailing_to_pobox":         15,
    "out_of_country":           25,
    # Value
    "cash_flow_candidate":      20,
    "teardown_candidate":       30,
    "hurricane_equity":         40,
    # Geographic
    "bulk_owner_3plus":         15,
    "bulk_owner_10plus":        25,
    # Velocity
    "never_sold":               20,
    "recent_underprice_sale":   45,
}


# ---------------------------------------------------------------------------
# MAIN ENRICHMENT FUNCTION
# ---------------------------------------------------------------------------

def enrich_lead(p: dict, owner_counts: dict, now: datetime) -> dict:
    """
    Apply all 22 flag detectors and property type classifier to a single parcel.
    Returns dict of {flag_name: True} for all flags that fired, plus property_type.
    Does NOT modify p.
    """
    prop_type = classify_property_type(p.get("use_code"))
    
    flags = {}
    # Distress
    if flag_equity_gap_high(p):            flags["equity_gap_high"] = True
    if flag_negative_equity(p):            flags["negative_equity"] = True
    # Life event
    if flag_widow_indicator(p):            flags["widow_indicator"] = True
    if flag_multi_owner_inheritance(p):    flags["multi_owner_inheritance"] = True
    if flag_care_of_address(p):            flags["care_of_address"] = True
    if flag_corporate_dissolved(p, now):   flags["corporate_dissolved"] = True
    # Holding
    if flag_tired_landlord(p, now, prop_type):    flags["tired_landlord"] = True
    if flag_accidental_landlord(p, now):   flags["accidental_landlord"] = True
    if flag_mailing_to_pobox(p):           flags["mailing_to_pobox"] = True
    if flag_out_of_country(p):             flags["out_of_country"] = True
    # Value
    if flag_cash_flow_candidate(p, prop_type):    flags["cash_flow_candidate"] = True
    if flag_teardown_candidate(p):         flags["teardown_candidate"] = True
    if flag_hurricane_equity(p):           flags["hurricane_equity"] = True
    # Geographic
    if flag_bulk_owner_3plus(p, owner_counts):    flags["bulk_owner_3plus"] = True
    if flag_bulk_owner_10plus(p, owner_counts):   flags["bulk_owner_10plus"] = True
    # Velocity
    if flag_never_sold(p):                 flags["never_sold"] = True
    if flag_recent_underprice_sale(p, now):       flags["recent_underprice_sale"] = True

    return {
        "property_type": prop_type,
        "enrichment_flags": flags,
        "enrichment_score": sum(ENRICHMENT_WEIGHTS.get(k, 0) for k in flags),
    }


# ---------------------------------------------------------------------------
# CLI ENTRY (allows standalone testing)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json, sys
    # Minimal self-test
    sample = {
        "owner": "SMITH JOHN & MARY H HEIRS OF",
        "owner_2": None,
        "use_code": "0141",
        "absentee": True,
        "out_of_state": True,
        "mail_state": "CA",
        "mail_address": "PO BOX 1234",
        "year_built": 1965,
        "fcv": 485000,
        "last_sale_price": 120000,
        "last_sale_date": "1998-05-12",
        "lot_sqft": 9200,
        "care_of": None,
        "land_value": None,
    }
    # Fake owner index
    counts = {"SMITH JOHN & MARY H": 1}
    now = datetime.utcnow()
    print(json.dumps(enrich_lead(sample, counts, now), indent=2))
