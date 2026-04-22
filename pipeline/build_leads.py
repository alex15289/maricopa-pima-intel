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
from datetime import datetime, timezone
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
# -----------------------------------------------------------------------------
# Scoring weights  (v2 — wholesaler-tuned, Oct 2026)
# -----------------------------------------------------------------------------
# Philosophy: weights reflect *close-rate* in wholesaler terms.
#   Pre-foreclosure & probate are the highest-converting cold calls in the
#   wholesaler playbook; tax delinquent is close behind. Absentee + high
#   equity is the classic "tired landlord" combo that needs stacking, not
#   individual weight.
SIGNAL_WEIGHTS = {
    # Foreclosure family — phone-today level of distress
    "Notice of Trustee Sale":  75,   # ↑ from 55 — 60-90 day auction deadline
    "Sheriffs Deed":           60,   # post-foreclosure REO

    # Estate / probate family — cash-inheritance motivation
    "Affidavit of Death":      65,   # ↑ from 50
    "Probate Case":            60,   # ↑ from 45
    "Affidavit":               15,

    # Tax / government liens — clock is ticking
    "Tax Delinquent":          55,   # ↑ from 40
    "Federal Tax Lien":        40,   # ↑ from 30
    "State Tax Lien":          30,   # ↑ from 25
    "Medicaid Lien":           30,   # Pima AHCCCS
    "City Lien":               25,
    "HOA Lien":                20,

    # Legal pressure — bringing a buyer = exit strategy
    "Lis Pendens":             45,   # ↑ from 35
    "Judgment Lien":           30,
    "Bankruptcy":              40,   # ↑ from 30
    "Bankruptcy Discharge":    30,
    "Divorce":                 30,

    # Physical distress
    "Mechanics Lien":          25,
    "Code Violation":          30,   # ↑ from 20 — violation = tired owner
    "Hospital Lien":           18,
}

# Original owner-name / parcel flags — tuned up for Pima's flag-only leads
LEGACY_FLAG_WEIGHTS = {
    "flag_estate_owner":       50,   # ↑ from 40 — "EST OF"/"ESTATE OF" = gold
    "flag_entity_owner":       -3,   # LLCs are harder to reach but reachable
    "flag_trust_only":         20,   # ↑ from 15
    "flag_likely_vacant":      40,   # ↑ from 30
    "flag_absentee":           18,   # ↑ from 12
    "flag_out_of_state":       22,   # ↑ from 15 — harder to maintain remotely
    "flag_long_hold":          12,   # ↑ from 8  — 15+ yr holds = equity-rich
}

# Stacking bonus per additional signal on same APN
STACK_BONUS = 15   # ↑ from 10

# Combo multipliers — the wholesaler playbook is about stacked signals
# Absentee + OOS + high equity = canonical "tired OOS landlord" — worth extra
COMBO_BONUS_TIRED_OOS_LANDLORD = 25      # absentee + OOS + equity>=200k
COMBO_BONUS_ESTATE_LONG_HOLD    = 30     # estate + long hold (inherited property sitting)
COMBO_BONUS_PROBATE_HIGH_EQUITY = 35     # probate filing + equity>=300k

# Equity bonuses — more granular for the high end
EQUITY_BRACKETS = [
    (1_000_000, 55),
    (500_000,   45),   # ↑ from 40
    (300_000,   35),   # ↑ from 30
    (150_000,   22),   # ↑ from 20
    (50_000,    10),
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
    # Strip timezone from now for comparison (last_sale_date is naive)
    now_naive = now.replace(tzinfo=None) if now.tzinfo else now
    return (now_naive - dt).days / 365.25 >= 10


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
# Call script angle composer
# -----------------------------------------------------------------------------
def _compose_call_angle(legacy_flags: dict, enrichment_flags: dict,
                        signals: list, equity: int) -> dict:
    """
    Build a 1-line "pitch angle" and a short script teaser for cold calling.
    Wholesalers need a reason for the call, dialed to whatever signal fires
    strongest on this lead.
    """
    sig_types = {s.get("type") for s in signals}

    # Priority order — strongest pitch first
    if "Notice of Trustee Sale" in sig_types:
        return {
            "headline": "🔥 Pre-Foreclosure — auction in ~90 days",
            "pitch":    "You can buy before it hits their credit. Fast cash close, no showings, walk-away money.",
            "tag":      "pre-foreclosure",
        }
    if "Affidavit of Death" in sig_types or "Probate Case" in sig_types:
        return {
            "headline": "👑 Estate / Probate filing",
            "pitch":    "Inherited property — pitch buying as-is so the heirs settle the estate without cleanup, repairs, or showings.",
            "tag":      "estate",
        }
    if "Tax Delinquent" in sig_types or "State Tax Lien" in sig_types or "Federal Tax Lien" in sig_types:
        return {
            "headline": "💰 Tax Lien — owner behind",
            "pitch":    "Cash offer covers the lien and leaves them walk-away money. Works for retired fixed-income owners especially.",
            "tag":      "tax-lien",
        }
    if "Lis Pendens" in sig_types:
        return {
            "headline": "⚖️ Lawsuit pending on title",
            "pitch":    "Title issue is delaying sale. You bring a buyer who can clear through closing escrow.",
            "tag":      "lis-pendens",
        }
    if "Judgment Lien" in sig_types:
        return {
            "headline": "⚖️ Judgment against owner",
            "pitch":    "Selling the property clears the judgment. Cash close + lien payoff = exit strategy.",
            "tag":      "judgment",
        }
    if "Code Violation" in sig_types:
        return {
            "headline": "🏚️ Code violation on file",
            "pitch":    "Owner avoiding city fines. Pitch: we buy as-is, handle the violations, you walk away clean.",
            "tag":      "violation",
        }
    # No signal — fall back to flag-based pitches
    if enrichment_flags.get("tired_landlord"):
        return {
            "headline": "🏢 Tired Landlord — long-held rental",
            "pitch":    "Done with tenants? Cash offer, 14-day close, no showings, no repairs, no tenant stress.",
            "tag":      "tired-landlord",
        }
    if legacy_flags.get("out_of_state") and equity >= 200_000:
        return {
            "headline": "📍 Out-of-State Owner — big equity",
            "pitch":    "Remote ownership + stacking equity. Pitch: cash out without the cross-country hassle.",
            "tag":      "oos-equity",
        }
    if legacy_flags.get("estate_owner"):
        return {
            "headline": "📜 Estate-owned",
            "pitch":    "Owner listed as 'ESTATE OF' — usually means family dispute or administrator burnout. Offer simplicity.",
            "tag":      "estate-owner",
        }
    if enrichment_flags.get("multi_owner_inheritance"):
        return {
            "headline": "👥 Inherited by multiple heirs",
            "pitch":    "Joint ownership complicates sale; pitch a clean single-buyer cash offer, they split cleanly at close.",
            "tag":      "multi-heir",
        }
    if enrichment_flags.get("corporate_dissolved"):
        return {
            "headline": "🏚️ LLC dissolved but still owns",
            "pitch":    "Abandoned asset. Track down managers and offer cash exit — they often forgot they own it.",
            "tag":      "corp-dissolved",
        }
    if legacy_flags.get("absentee"):
        return {
            "headline": "📬 Absentee owner",
            "pitch":    "Mailing address ≠ site. Pitch: stop dealing with it remotely — cash out, we handle everything.",
            "tag":      "absentee",
        }
    if equity >= 500_000:
        return {
            "headline": "💎 Deep equity, long hold",
            "pitch":    "Tap equity without listing. Clean all-cash offer, two-week close.",
            "tag":      "high-equity",
        }
    return {
        "headline": "📞 Qualified prospect",
        "pitch":    "Use the flags and signals on this lead to tailor your opener.",
        "tag":      "generic",
    }


# -----------------------------------------------------------------------------
# Main pipeline
# -----------------------------------------------------------------------------
def build(min_score: int = 30, limit_dashboard: int = 50_000) -> dict:
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds").replace("+00:00", "Z")

    # ── Load previous leads.json to detect new leads since last build ──────
    prev_path = DATA_DIR / "leads.json"
    prev_first_seen: dict[str, str] = {}
    prev_generated = None
    if prev_path.exists():
        try:
            prev = json.loads(prev_path.read_text())
            prev_generated = prev.get("generated_at")
            for pl in prev.get("leads", []):
                k = f"{pl.get('county')}:{pl.get('apn_norm') or pl.get('apn')}"
                # Preserve original first_seen if present; otherwise stamp with prev build
                prev_first_seen[k] = pl.get("first_seen_at") or prev_generated or now_iso
            log.info(f"loaded previous leads.json ({len(prev_first_seen):,} leads "
                     f"generated {prev_generated})")
        except Exception as e:
            log.warning(f"could not parse previous leads.json: {e}")

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
        max_imminence = 0   # track foreclosure risk from tax-delinquent signals
        for sig in parcel_signals:
            stype = sig.get("signal_type", "")
            w = SIGNAL_WEIGHTS.get(stype, 5)
            signal_score += w
            # Pima tax-delinquent signals carry foreclosure_imminence (1-5)
            imminence = sig.get("foreclosure_imminence", 0) or 0
            if imminence > max_imminence:
                max_imminence = imminence
            # Bonus weight for deep delinquency (3+ years = tax auction threshold)
            if imminence >= 4:
                signal_score += 15
            elif imminence >= 3:
                signal_score += 8
            signal_entries.append({
                "type": stype,
                "date": sig.get("record_date") or sig.get("filed_date"),
                "doc_number": sig.get("doc_number") or sig.get("case_number"),
                "amount": sig.get("amount"),
                "years_delinquent": sig.get("years_delinquent"),
                "foreclosure_imminence": imminence or None,
                "doc_url": sig.get("doc_url"),
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

        # ── Synthetic "likely_foreclosure" flag ───────────────────────────
        # Fires when: NOTS/Sheriffs Deed present, OR tax imminence >= 3
        # (tax auction threshold), OR bankruptcy + absentee
        likely_foreclosure = False
        has_nots = any(s.get("type") == "Notice of Trustee Sale" for s in signal_entries)
        has_sheriff = any(s.get("type") == "Sheriffs Deed" for s in signal_entries)
        has_bk = any(s.get("type") == "Bankruptcy" for s in signal_entries)
        if has_nots or has_sheriff:
            likely_foreclosure = True
        elif max_imminence >= 3:
            likely_foreclosure = True
        elif has_bk and legacy_flags.get("absentee"):
            likely_foreclosure = True
        if likely_foreclosure:
            legacy_flags["likely_foreclosure"] = True

        # ── Combo bonuses — the wholesaler playbook ───────────────────────
        combo_score = 0
        combos_hit = []
        if (legacy_flags.get("absentee") and legacy_flags.get("out_of_state")
                and equity >= 200_000):
            combo_score += COMBO_BONUS_TIRED_OOS_LANDLORD
            combos_hit.append("tired_oos_landlord")
        if (legacy_flags.get("estate_owner") and legacy_flags.get("long_hold")):
            combo_score += COMBO_BONUS_ESTATE_LONG_HOLD
            combos_hit.append("inherited_held_property")
        has_probate_signal = any(
            s.get("type") in ("Affidavit of Death", "Probate Case")
            for s in signal_entries
        )
        if has_probate_signal and equity >= 300_000:
            combo_score += COMBO_BONUS_PROBATE_HIGH_EQUITY
            combos_hit.append("probate_high_equity")

        # ── Additional combos (Oct 2026 tuning) ───────────────────────────
        has_any_tax = any(s.get("type") in ("Tax Delinquent", "State Tax Lien",
                                             "Federal Tax Lien") for s in signal_entries)
        has_any_lien = any("Lien" in (s.get("type") or "") for s in signal_entries)
        # Tax + Time: tax-delinquent + 15+ yr hold = long-ignored equity
        if has_any_tax and (p.get("hold_years") or 0) >= 15:
            combo_score += 20
            combos_hit.append("tax_delinquent_long_hold")
        # Distant Heir: estate-owner + out-of-state
        if legacy_flags.get("estate_owner") and legacy_flags.get("out_of_state"):
            combo_score += 25
            combos_hit.append("distant_heir")
        # LLC Dissolution + Lien: dissolved LLC still owns + any lien = abandoned
        if enrichment["enrichment_flags"].get("corporate_dissolved") and has_any_lien:
            combo_score += 20
            combos_hit.append("dissolved_llc_with_lien")
        # Serial Absentee: absentee + 3+ other properties
        if legacy_flags.get("absentee") and enrichment["enrichment_flags"].get("bulk_owner_3plus"):
            combo_score += 18
            combos_hit.append("serial_absentee")
        # Deep distress: tax imminence >= 4 + absentee
        if max_imminence >= 4 and legacy_flags.get("absentee"):
            combo_score += 30
            combos_hit.append("deep_distress_absentee")

        total_score = signal_score + legacy_score + enrichment_score + eq_bonus + combo_score

        # Filter: only include if meets min_score threshold
        if total_score < min_score:
            continue

        # Only residential property types by default (can be overridden via dashboard)
        # but include commercial if it has signals or very high scores
        prop_type = enrichment["property_type"]
        is_resi = PROPERTY_TYPES.get(prop_type, {}).get("residential", False)
        if not is_resi and not parcel_signals and total_score < 60:
            continue  # drop low-scoring commercial noise

        # ── First-seen timestamp: preserve previous value if lead existed before ─
        lead_key = f"{county}:{p.get('apn_norm') or p.get('apn')}"
        first_seen_at = prev_first_seen.get(lead_key, now_iso)

        # ── Call-script angle: short pitch reason based on top distress markers ─
        call_angle = _compose_call_angle(legacy_flags, enrichment["enrichment_flags"],
                                         signal_entries, equity)

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
            "combos":           combos_hit,
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
                "combos":     int(combo_score),
            },
            # Recency tracking
            "first_seen_at":    first_seen_at,
            # Wholesaler copy
            "call_angle":       call_angle,
        }
        leads.append(lead)

    # Sort by score desc
    leads.sort(key=lambda x: x["score"], reverse=True)
    log.info(f"produced {len(leads):,} ranked leads (total, before cap)")
    if leads:
        log.info(f"top scores: {[l['score'] for l in leads[:5]]}")

    # ── NEW-lead detection vs previous build ─────────────────────────────
    new_count_24h = 0
    new_count_7d = 0
    if prev_first_seen:
        prev_keys = set(prev_first_seen.keys())
        cur_keys = {f"{l['county']}:{l['apn_norm'] or l['apn']}" for l in leads}
        truly_new = cur_keys - prev_keys   # didn't exist in previous build at all
        for l in leads:
            k = f"{l['county']}:{l['apn_norm'] or l['apn']}"
            if k in truly_new:
                new_count_24h += 1
        log.info(f"NEW leads since last build: {len(truly_new):,}")
    else:
        log.info("no previous leads.json — all leads are considered seen today")

    # ── Daily rotation seed — "New Opportunities" pool ───────────────────
    # Surface a fresh ~250 leads each day by seeding the shuffle with today's
    # date. Top 2,000 high-scoring leads act as the pool we rotate through.
    import random as _random
    import datetime as _dt
    seed = int(_dt.date.today().strftime("%Y%m%d"))
    _rng = _random.Random(seed)
    pool = leads[:2000]
    rotation_indexes = list(range(len(pool)))
    _rng.shuffle(rotation_indexes)
    daily_rotation_keys = set()
    for idx in rotation_indexes[:250]:
        l = pool[idx]
        daily_rotation_keys.add(f"{l['county']}:{l['apn_norm'] or l['apn']}")
    # Tag the leads that are in today's rotation
    for l in leads:
        k = f"{l['county']}:{l['apn_norm'] or l['apn']}"
        if k in daily_rotation_keys:
            l["daily_opportunity"] = True
    log.info(f"daily rotation: {len(daily_rotation_keys)} leads tagged (seed={seed})")

    out = {
        "generated_at": now_iso,
        "previous_generated_at": prev_generated,
        "daily_rotation_date": _dt.date.today().isoformat(),
        "daily_rotation_size": len(daily_rotation_keys),
        "counties":     ["Maricopa", "Pima"],
        "total_leads":  len(leads),
        "new_since_last_build": new_count_24h,
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
