"""
Pipeline: build the ranked motivated seller lead list — full enrichment edition.

Takes:
    data/maricopa_parcels.jsonl     (parcel master — Maricopa)
    data/pima_parcels.jsonl         (parcel master — Pima)
    data/maricopa_recorder_raw.jsonl, data/pima_recorder_raw.jsonl   (optional signals)
    data/tax_delinquent_*.jsonl, data/probate_*.jsonl,
    data/code_violations_*.jsonl    (optional signals)

Produces:
    data/leads.json                 (dashboard input — ranked, stacked, enriched)

Lead generation runs in two modes merged into one output:
  (a) Signal-stacked leads: properties with at least one recorder/tax signal
  (b) Flag-only leads: properties with strong enrichment flags but no external signals

Everything is scored, stacked by APN, enriched with the 22-flag module, and
property-type classified. Output is sorted by total score descending.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from pipeline.enrich_leads import (
    enrich_lead,
    build_owner_index,
    classify_property_type,
    PROPERTY_TYPES,
    ENRICHMENT_WEIGHTS,
)

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("pipeline")


# -----------------------------------------------------------------------------
# Scoring weights
# -----------------------------------------------------------------------------
SIGNAL_WEIGHTS = {
    # External recorded signals — highest value
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
    "Judgment Lien":           25,
    "Bankruptcy":              30,
    "Divorce":                 25,
}

# Original owner-name / parcel flags (legacy v1 flags)
LEGACY_FLAG_WEIGHTS = {
    "flag_estate_owner":       40,
    "flag_entity_owner":       -5,
    "flag_trust_only":         15,
    "flag_likely_vacant":      30,
    "flag_absentee":           12,
    "flag_out_of_state":       15,
    "flag_long_hold":           8,
}

# Stacking bonus per additional signal on same APN
STACK_BONUS = 10

# Equity bonuses
EQUITY_BRACKETS = [
    (500_000, 40),
    (300_000, 30),
    (150_000, 20),
    (50_000, 10),
]


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
ESTATE_RE  = re.compile(r"\b(ESTATE\s+OF|EST\s+OF|HEIRS\s+OF|EST\.|DECEASED|\(DEC\))\b", re.I)
ENTITY_RE  = re.compile(r"\b(LLC|L\.L\.C\.|INC|INCORPORATED|LTD|LIMITED|L\.P\.|LP|HOLDINGS|INVESTMENTS|PROPERTIES|GROUP|PARTNERS|PARTNERSHIP|CORP|CORPORATION)\b", re.I)
TRUST_RE   = re.compile(r"\bTRUST\b|\bTR\b|\bTRS\b|\bTRUSTEE\b|FAMILY\s+TR\b", re.I)
BANK_RE    = re.compile(r"BANK|FARGO|CHASE|CITI|CAPITAL|TITLE", re.I)


def parse_money(raw) -> float:
    if raw is None: return 0.0
    if isinstance(raw, (int, float)): return float(raw)
    s = str(raw).strip().replace(",", "").replace("$", "").replace(" ", "")
    try: return float(s) if s else 0.0
    except: return 0.0


def parse_year(raw) -> int:
    if raw is None: return 0
    try: return int(str(raw).strip()[:4])
    except: return 0


def parse_record_date(s: Optional[str]) -> Optional[datetime]:
    if not s: return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y%m%d"):
        try: return datetime.strptime(s[:10], fmt)
        except: pass
    # epoch millis
    try:
        if s.isdigit() and len(s) >= 10:
            return datetime.fromtimestamp(int(s[:10]))
    except: pass
    return None


def is_estate_owner(owner: Optional[str]) -> bool:
    return bool(owner and ESTATE_RE.search(owner))


def is_entity_owner(owner: Optional[str]) -> bool:
    return bool(owner and ENTITY_RE.search(owner))


def is_trust_owner(owner: Optional[str]) -> bool:
    if not owner: return False
    if BANK_RE.search(owner): return False
    return bool(TRUST_RE.search(owner)) and not is_entity_owner(owner)


def is_long_hold(parcel: dict, now: datetime) -> bool:
    dt = parse_record_date(parcel.get("last_sale_date"))
    if not dt: return False
    return (now - dt).days / 365.25 >= 10


def equity_estimate(parcel: dict) -> float:
    fcv = parse_money(parcel.get("fcv"))
    last = parse_money(parcel.get("last_sale_price"))
    return max(0.0, fcv - last)


def equity_bonus(equity: float) -> int:
    for threshold, bonus in EQUITY_BRACKETS:
        if equity >= threshold:
            return bonus
    return 0


# -----------------------------------------------------------------------------
# Load parcel master
# -----------------------------------------------------------------------------
def load_parcels(path: Path) -> list:
    if not path.exists():
        log.warning(f"missing parcel file: {path}")
        return []
    out = []
    with path.open() as f:
        for line in f:
            try: out.append(json.loads(line))
            except: pass
    log.info(f"loaded {len(out):,} parcels from {path.name}")
    return out


def load_signals(path: Path, signal_type: str = None) -> list:
    """Load a signal jsonl file. If signal_type given, tag all rows with it."""
    if not path.exists():
        log.info(f"missing (skipping): {path.name}")
        return []
    out = []
    with path.open() as f:
        for line in f:
            try:
                rec = json.loads(line)
                if signal_type and "signal_type" not in rec:
                    rec["signal_type"] = signal_type
                out.append(rec)
            except: pass
    log.info(f"loaded {len(out):,} signals from {path.name}")
    return out


# -----------------------------------------------------------------------------
# Main pipeline
# -----------------------------------------------------------------------------
def build(min_score: int = 30, limit_dashboard: int = 10000) -> dict:
    now = datetime.utcnow()

    maricopa_parcels = load_parcels(DATA_DIR / "maricopa_parcels.jsonl")
    pima_parcels     = load_parcels(DATA_DIR / "pima_parcels.jsonl")
    all_parcels = maricopa_parcels + pima_parcels

    # Per-county owner concentration index (for bulk-owner flags)
    owner_counts_maricopa = build_owner_index(maricopa_parcels)
    owner_counts_pima     = build_owner_index(pima_parcels)

    # APN lookup for signal resolution
    apn_index = {}
    for p in all_parcels:
        key = (p.get("county"), (p.get("apn_norm") or p.get("apn") or "").upper())
        if key[1]:
            apn_index[key] = p

    # Load all optional signal sources
    all_signals = []
    all_signals += load_signals(DATA_DIR / "maricopa_recorder_raw.jsonl")
    all_signals += load_signals(DATA_DIR / "pima_recorder_raw.jsonl")
    all_signals += load_signals(DATA_DIR / "tax_delinquent_maricopa.jsonl", "Tax Delinquent")
    all_signals += load_signals(DATA_DIR / "tax_delinquent_pima.jsonl",     "Tax Delinquent")
    all_signals += load_signals(DATA_DIR / "code_violations_maricopa.jsonl","Code Violation")
    all_signals += load_signals(DATA_DIR / "code_violations_pima.jsonl",    "Code Violation")
    all_signals += load_signals(DATA_DIR / "probate_maricopa.jsonl",        "Probate Case")
    all_signals += load_signals(DATA_DIR / "probate_pima.jsonl",            "Probate Case")
    log.info(f"total signals: {len(all_signals):,}")

    # Group signals by APN
    signals_by_apn = defaultdict(list)
    unresolved = 0
    for sig in all_signals:
        apn = (sig.get("apn_norm") or sig.get("apn") or "").upper()
        county = sig.get("county")
        if not apn:
            unresolved += 1
            continue
        key = (county, apn)
        if key not in apn_index:
            unresolved += 1
            continue
        signals_by_apn[key].append(sig)

    # Build leads: every parcel that either has signals OR has strong flags
    leads = []
    parcels_checked = 0
    for p in all_parcels:
        parcels_checked += 1
        if parcels_checked % 200000 == 0:
            log.info(f"processed {parcels_checked:,} parcels...")

        county = p.get("county")
        apn = (p.get("apn_norm") or p.get("apn") or "").upper()
        key = (county, apn)
        parcel_signals = signals_by_apn.get(key, [])

        # Run enrichment (22 new flags + property type)
        counts = owner_counts_maricopa if county == "Maricopa" else owner_counts_pima
        enrichment = enrich_lead(p, counts, now)

        # Compute legacy flags
        legacy_flags = {}
        if is_estate_owner(p.get("owner")):    legacy_flags["estate_owner"] = True
        if is_entity_owner(p.get("owner")):    legacy_flags["entity_owner"] = True
        if is_trust_owner(p.get("owner")):     legacy_flags["family_trust"] = True
        if p.get("absentee"):                  legacy_flags["absentee"] = True
        if p.get("out_of_state"):              legacy_flags["out_of_state"] = True
        if is_long_hold(p, now):               legacy_flags["long_hold"] = True

        # Score components
        signal_score = 0
        signal_entries = []
        for sig in parcel_signals:
            stype = sig.get("signal_type", "")
            w = SIGNAL_WEIGHTS.get(stype, 5)
            signal_score += w
            signal_entries.append({
                "type": stype,
                "date": sig.get("record_date") or sig.get("filed_date"),
                "doc_number": sig.get("doc_number") or sig.get("case_number"),
                "amount": sig.get("amount"),
            })
        # Stacking bonus
        if len(parcel_signals) > 1:
            signal_score += STACK_BONUS * (len(parcel_signals) - 1)

        legacy_score = 0
        if legacy_flags.get("estate_owner"):  legacy_score += LEGACY_FLAG_WEIGHTS["flag_estate_owner"]
        if legacy_flags.get("entity_owner"):  legacy_score += LEGACY_FLAG_WEIGHTS["flag_entity_owner"]
        if legacy_flags.get("family_trust"):  legacy_score += LEGACY_FLAG_WEIGHTS["flag_trust_only"]
        if legacy_flags.get("absentee"):      legacy_score += LEGACY_FLAG_WEIGHTS["flag_absentee"]
        if legacy_flags.get("out_of_state"):  legacy_score += LEGACY_FLAG_WEIGHTS["flag_out_of_state"]
        if legacy_flags.get("long_hold"):     legacy_score += LEGACY_FLAG_WEIGHTS["flag_long_hold"]

        enrichment_score = enrichment["enrichment_score"]
        equity = equity_estimate(p)
        eq_bonus = equity_bonus(equity)

        total_score = signal_score + legacy_score + enrichment_score + eq_bonus

        # Filter: only include if meets min_score threshold
        if total_score < min_score:
            continue

        # Only residential property types by default (can be overridden via dashboard)
        # but include commercial if it has signals or very high scores
        prop_type = enrichment["property_type"]
        is_resi = PROPERTY_TYPES.get(prop_type, {}).get("residential", False)
        if not is_resi and not parcel_signals and total_score < 60:
            continue  # drop low-scoring commercial noise

        lead = {
            "county":           county,
            "apn":              p.get("apn"),
            "apn_norm":         p.get("apn_norm"),
            "owner":            p.get("owner"),
            "owner_2":          p.get("owner_2"),
            "site_address":     p.get("site_address"),
            "site_city":        p.get("site_city"),
            "site_zip":         p.get("site_zip"),
            "mail_address":     p.get("mail_address"),
            "mail_city":        p.get("mail_city"),
            "mail_state":       p.get("mail_state"),
            "mail_zip":         p.get("mail_zip"),
            "use_code":         p.get("use_code"),
            "use_desc":         p.get("use_desc"),
            "property_type":    prop_type,
            "year_built":       p.get("year_built"),
            "living_sqft":      p.get("living_sqft"),
            "lot_sqft":         p.get("lot_sqft"),
            "fcv":              p.get("fcv"),
            "lpv":              p.get("lpv"),
            "last_sale_date":   p.get("last_sale_date"),
            "last_sale_price":  p.get("last_sale_price"),
            "latitude":         p.get("latitude"),
            "longitude":        p.get("longitude"),
            "equity_est":       int(equity),
            # Flags
            "legacy_flags":     legacy_flags,
            "enrichment_flags": enrichment["enrichment_flags"],
            # Signals
            "signals":          signal_entries,
            "signal_count":     len(signal_entries),
            # Scoring breakdown
            "score":            int(total_score),
            "score_parts": {
                "signals":    int(signal_score),
                "legacy":     int(legacy_score),
                "enrichment": int(enrichment_score),
                "equity":     int(eq_bonus),
            },
        }
        leads.append(lead)

    # Sort by score desc
    leads.sort(key=lambda x: x["score"], reverse=True)
    log.info(f"produced {len(leads):,} ranked leads (total, before 10k cap)")
    if leads:
        log.info(f"top scores: {[l['score'] for l in leads[:5]]}")

    out = {
        "generated_at": now.isoformat(timespec="seconds") + "Z",
        "counties":     ["Maricopa", "Pima"],
        "total_leads":  len(leads),
        "unresolved_signals": unresolved,
        "property_types": PROPERTY_TYPES,
        "flag_weights": {**LEGACY_FLAG_WEIGHTS, **ENRICHMENT_WEIGHTS, **SIGNAL_WEIGHTS},
        "leads":        leads[:limit_dashboard],
    }

    out_path = DATA_DIR / "leads.json"
    out_path.write_text(json.dumps(out, default=str))
    log.info(f"wrote -> {out_path}  ({len(leads[:limit_dashboard]):,} leads in dashboard file)")
    return out


if __name__ == "__main__":
    build()
