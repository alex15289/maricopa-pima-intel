"""
Pipeline: build the ranked motivated seller lead list.

Takes:
    data/maricopa_parcels.jsonl       (parcel master — Maricopa)
    data/pima_parcels.jsonl           (parcel master — Pima)
    data/maricopa_recorder_raw.jsonl  (motivated-seller signals — Maricopa)
    data/pima_recorder_raw.jsonl      (motivated-seller signals — Pima)
    [optional] data/tax_delinquent_*.jsonl,
               data/code_violations_*.jsonl,
               data/probate_*.jsonl

Produces:
    data/leads.json                   (dashboard input — ranked, stacked)

Core logic:
    1. Load parcel master into APN-keyed dict (one per county)
    2. For each signal, resolve property via APN -> parcel master
       If no APN, fall back to owner-name match (grantor or decedent)
    3. Stack multiple signals for the same APN into a single lead
    4. Score each lead by weighted signal contributions + property flags
    5. Sort by score descending, write leads.json

Score is tunable via WEIGHTS at top of file.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("pipeline")

# -----------------------------------------------------------------------------
# Scoring weights — tune these for your campaign.
# -----------------------------------------------------------------------------
WEIGHTS = {
    # Signal-type base weights (max signal score, decays slightly with age)
    "Notice of Trustee Sale":  55,
    "Lis Pendens":             35,
    "Affidavit of Death":      50,
    "Affidavit":               10,
    "Federal Tax Lien":        30,
    "State Tax Lien":          25,
    "Mechanics Lien":          20,
    "HOA Lien":                15,
    "Tax Delinquent":          40,
    "Code Violation":          20,
    "Probate Case":            45,

    # Owner-name flags
    "flag_estate_owner":       40,
    "flag_cash_buyer":         -5,
    "flag_trust_only":         15,
    "flag_likely_vacant":      30,

    # Property flags (static — added once per lead regardless of signals)
    "flag_absentee":           10,
    "flag_out_of_state":       15,
    "flag_long_hold":          8,    # owned 10+ years
    "flag_no_site_address":   -20,   # raw land / junk parcel — demote

    # Stacking bonuses
    "stack_2_signals":         10,
    "stack_3_signals":         25,
    "stack_4plus_signals":     45,

    # Equity tier bonuses (equity = FCV - last sale price)
    "equity_50k":               5,
    "equity_150k":             15,
    "equity_300k":             30,
}

# Signal age decay: lose 1 point per 30 days over 180 days old (floor 0 off base).
AGE_DECAY_PER_MONTH = 1
AGE_DECAY_START_DAYS = 180


def load_jsonl(path: Path) -> list:
    if not path.exists():
        log.info("missing (skipping): %s", path)
        return []
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    log.info("loaded %d rows from %s", len(rows), path.name)
    return rows


def build_parcel_index(parcel_rows: list) -> tuple[dict, dict]:
    """Returns (by_apn, by_owner_name_lower)."""
    by_apn: dict = {}
    by_owner: dict = defaultdict(list)
    for p in parcel_rows:
        key = p.get("apn_norm") or p.get("apn")
        if key:
            by_apn[key] = p
        owner = (p.get("owner") or "").strip().upper()
        if owner:
            by_owner[owner].append(p)
    return by_apn, dict(by_owner)


def normalize_apn(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    return re.sub(r"[^A-Z0-9]", "", raw.upper())


def resolve_parcel(signal: dict, by_apn: dict, by_owner: dict) -> Optional[dict]:
    """Find the parcel for this signal. APN first, owner name fallback."""
    for apn in signal.get("apns_found") or []:
        norm = normalize_apn(apn)
        if norm and norm in by_apn:
            return by_apn[norm]
    # Fallback: grantor name match (single unique hit only — multiple owners
    # with the same name are ambiguous and get skipped to avoid false pairs).
    for name_field in ("grantor", "decedent", "owner"):
        name = (signal.get(name_field) or "").strip().upper()
        if name and name in by_owner and len(by_owner[name]) == 1:
            return by_owner[name][0]
    return None




# Owner-name pattern detection
ESTATE_RE      = re.compile(r"\b(ESTATE\s+OF|EST\s+OF|HEIRS\s+OF|EST\.)\b|\bEST\b\s+OF|DECEASED|\(DEC\)")
ENTITY_RE      = re.compile(r"\b(LLC|L\.L\.C\.|INC|INCORPORATED|LTD|LIMITED|L\.P\.|LP|HOLDINGS|INVESTMENTS|PROPERTIES|GROUP|PARTNERS|PARTNERSHIP|CORP|CORPORATION)\b")
TRUST_RE       = re.compile(r"\bTRUST\b|\bTR\b|\bTRS\b|\bTRUSTEE\b|FAMILY\s+TR\b")
BANK_TRUST_RE  = re.compile(r"BANK|TITLE|FARGO|CHASE|CITI|TRUSTEE\s+FOR|CAPITAL")

def is_estate_owner(owner):
    return bool(owner and ESTATE_RE.search(owner.upper()))

def is_entity_owner(owner):
    return bool(owner and ENTITY_RE.search(owner.upper()))

def is_trust_owner(owner):
    if not owner: return False
    up = owner.upper()
    if BANK_TRUST_RE.search(up): return False
    return bool(TRUST_RE.search(up)) and not is_entity_owner(owner)

def is_likely_vacant(parcel, now):
    if not parcel.get("out_of_state"): return False
    if not long_hold(parcel, now): return False
    try: yb = int(parcel.get("year_built") or 0)
    except: yb = 0
    return yb > 0 and (now.year - yb) >= 30

def parse_money(raw):
    if raw is None: return 0.0
    if isinstance(raw, (int, float)): return float(raw)
    s = str(raw).strip().replace(",", "").replace("$", "").replace(" ", "")
    try: return float(s) if s else 0.0
    except: return 0.0

def parse_record_date(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s.strip()[:10], fmt)
        except ValueError:
            continue
    return None


def signal_score(signal: dict, now: datetime) -> int:
    base = WEIGHTS.get(signal.get("signal_type", ""), 5)
    d = parse_record_date(signal.get("record_date"))
    if not d:
        return base
    age_days = (now - d).days
    if age_days <= AGE_DECAY_START_DAYS:
        return base
    months_over = (age_days - AGE_DECAY_START_DAYS) // 30
    return max(0, base - months_over * AGE_DECAY_PER_MONTH)


def equity_estimate(parcel: dict) -> float:
    fcv  = parse_money(parcel.get("fcv"))
    last = parse_money(parcel.get("last_sale_price"))
    return max(0.0, fcv - last)


def long_hold(parcel: dict, now: datetime) -> bool:
    s = parcel.get("last_sale_date")
    d = parse_record_date(s) if isinstance(s, str) else None
    if not d:
        return False
    return (now - d).days >= 365 * 10


def build_leads(signals: list, parcels_by_county: dict) -> list:
    """Stack signals by (county, apn) and score each resulting lead."""
    now = datetime.utcnow()

    # Group signals by (county, apn)
    grouped: dict = defaultdict(list)
    unresolved = 0

    for sig in signals:
        county = sig.get("county")
        idx = parcels_by_county.get(county)
        if not idx:
            continue
        by_apn, by_owner = idx
        parcel = resolve_parcel(sig, by_apn, by_owner)
        if not parcel:
            unresolved += 1
            continue
        key = (county, parcel.get("apn_norm"))
        grouped[key].append((sig, parcel))

    log.info("grouped %d signals into %d unique properties (%d unresolved)",
             sum(len(v) for v in grouped.values()), len(grouped), unresolved)

    leads = []
    for (county, apn_norm), entries in grouped.items():
        parcel = entries[0][1]
        sigs = [e[0] for e in entries]

        # Score components
        signal_scores = [signal_score(s, now) for s in sigs]
        signal_total = sum(signal_scores)

        flag_score = 0
        if parcel.get("absentee"):      flag_score += WEIGHTS["flag_absentee"]
        if parcel.get("out_of_state"):  flag_score += WEIGHTS["flag_out_of_state"]
        if long_hold(parcel, now):      flag_score += WEIGHTS["flag_long_hold"]
        if not parcel.get("site_address"):
            flag_score += WEIGHTS["flag_no_site_address"]

        estate_flag       = is_estate_owner(parcel.get("owner"))
        entity_flag       = is_entity_owner(parcel.get("owner"))
        trust_flag        = is_trust_owner(parcel.get("owner"))
        likely_vacant_flag = is_likely_vacant(parcel, now)
        if estate_flag:        flag_score += WEIGHTS["flag_estate_owner"]
        if entity_flag:        flag_score += WEIGHTS["flag_cash_buyer"]
        if trust_flag:         flag_score += WEIGHTS["flag_trust_only"]
        if likely_vacant_flag: flag_score += WEIGHTS["flag_likely_vacant"]

        stack_bonus = 0
        n = len(sigs)
        if n == 2:    stack_bonus = WEIGHTS["stack_2_signals"]
        elif n == 3:  stack_bonus = WEIGHTS["stack_3_signals"]
        elif n >= 4:  stack_bonus = WEIGHTS["stack_4plus_signals"]

        equity = equity_estimate(parcel)
        equity_bonus = 0
        if equity >= 300_000:   equity_bonus = WEIGHTS["equity_300k"]
        elif equity >= 150_000: equity_bonus = WEIGHTS["equity_150k"]
        elif equity >=  50_000: equity_bonus = WEIGHTS["equity_50k"]

        total = signal_total + flag_score + stack_bonus + equity_bonus

        lead = {
            # Identity
            "county":         county,
            "apn":            parcel.get("apn"),
            "apn_norm":       apn_norm,
            # Property
            "owner":          parcel.get("owner"),
            "owner_2":        parcel.get("owner_2"),
            "site_address":   parcel.get("site_address"),
            "site_city":      parcel.get("site_city"),
            "site_zip":       parcel.get("site_zip"),
            "mail_address":   parcel.get("mail_address"),
            "mail_city":      parcel.get("mail_city"),
            "mail_state":     parcel.get("mail_state"),
            "mail_zip":       parcel.get("mail_zip"),
            "use_desc":       parcel.get("use_desc"),
            "year_built":     parcel.get("year_built"),
            "living_sqft":    parcel.get("living_sqft"),
            "lot_sqft":       parcel.get("lot_sqft"),
            "bedrooms":       parcel.get("bedrooms"),
            "bathrooms":      parcel.get("bathrooms"),
            "fcv":            parcel.get("fcv"),
            "lpv":            parcel.get("lpv"),
            "last_sale_date": parcel.get("last_sale_date"),
            "last_sale_price":parcel.get("last_sale_price"),
            "absentee":       parcel.get("absentee"),
            "out_of_state":   parcel.get("out_of_state"),
            "estate_owner":   estate_flag,
            "cash_buyer":     entity_flag,
            "family_trust":   trust_flag,
            "likely_vacant":  likely_vacant_flag,
            "long_hold":      long_hold(parcel, now),
            # Signals (stacked)
            "signal_count":   n,
            "signals":        [
                {
                    "type":     s.get("signal_type"),
                    "doc_code": s.get("doc_code"),
                    "date":     s.get("record_date"),
                    "doc_url":  s.get("doc_url"),
                    "grantor":  s.get("grantor"),
                    "grantee":  s.get("grantee"),
                }
                for s in sigs
            ],
            # Score breakdown
            "score":          int(total),
            "score_parts":    {
                "signals":   signal_total,
                "flags":     flag_score,
                "stack":     stack_bonus,
                "equity":    equity_bonus,
            },
            "equity_est":     int(equity),
        }
        leads.append(lead)

    leads.sort(key=lambda x: x["score"], reverse=True)
    return leads


def main():
    ap = argparse.ArgumentParser(description="Build ranked motivated seller lead list")
    ap.add_argument("--out", default=str(DATA_DIR / "leads.json"))
    args = ap.parse_args()

    # Parcel masters
    mar_p = load_jsonl(DATA_DIR / "maricopa_parcels.jsonl")
    pim_p = load_jsonl(DATA_DIR / "pima_parcels.jsonl")
    parcels_by_county = {
        "Maricopa": build_parcel_index(mar_p),
        "Pima":     build_parcel_index(pim_p),
    }

    # Signals (add more files here as you build them — tax delinquent,
    # code violations, probate, etc. All must share the unified signal schema.)
    signals = []
    signals.extend(load_jsonl(DATA_DIR / "maricopa_recorder_raw.jsonl"))
    signals.extend(load_jsonl(DATA_DIR / "pima_recorder_raw.jsonl"))
    signals.extend(load_jsonl(DATA_DIR / "tax_delinquent_maricopa.jsonl"))
    signals.extend(load_jsonl(DATA_DIR / "tax_delinquent_pima.jsonl"))
    signals.extend(load_jsonl(DATA_DIR / "code_violations_maricopa.jsonl"))
    signals.extend(load_jsonl(DATA_DIR / "code_violations_pima.jsonl"))
    signals.extend(load_jsonl(DATA_DIR / "probate_maricopa.jsonl"))
    signals.extend(load_jsonl(DATA_DIR / "probate_pima.jsonl"))

    log.info("total signals: %d", len(signals))

    leads = build_leads(signals, parcels_by_county)
    log.info("produced %d ranked leads", len(leads))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        json.dump({
            "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "counties":     list(parcels_by_county.keys()),
            "total_leads":  len(leads),
            "leads":        leads,
        }, f, indent=2, default=str)

    log.info("wrote -> %s", out)


if __name__ == "__main__":
    main()
