#!/usr/bin/env python3
"""
Match recorder records to parcel APNs via owner-name join.

Input:  data/maricopa_recorder_raw.jsonl  (output of maricopa_recorder_api.py)
        data/maricopa_parcels.jsonl       (parcel master — owner name authority)

Output: data/maricopa_recorder_raw.jsonl  (rewritten in place with apn added
                                           to every record that matched)
        data/maricopa_recorder_unresolved.jsonl  (records we couldn't match —
                                                  for manual review)

Matching strategy (tiered):
    T1. Exact normalized match on full name (drop punctuation/suffixes, upper).
    T2. Last-name exact + first-name starts-with match.
    T3. Last-name exact + middle-initial present → flag as ambiguous.
    Anything else → unresolved.

We always skip business/lender names on the recorder side (FREEDOM MORTGAGE,
CHASE, etc.) because they're not the homeowner. A blacklist of known financial
institution terms is used.

Ambiguous matches (Tier 3) are still written with apn set and flagged with
'match_tier': 3, so they show in the dashboard with a ⚠️ badge. The
wholesaler can verify on the call.
"""
from __future__ import annotations

import json
import logging
import re
import sys
from collections import defaultdict
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RECORDER_IN = DATA_DIR / "maricopa_recorder_raw.jsonl"
PARCELS = DATA_DIR / "maricopa_parcels.jsonl"
UNRESOLVED_OUT = DATA_DIR / "maricopa_recorder_unresolved.jsonl"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("match_recorder")


# Anything containing one of these tokens is a business / lender / trustee,
# not a homeowner. Stripped before matching.
BUSINESS_TOKENS = {
    "MORTGAGE", "BANK", "LENDING", "LOANS", "FINANCIAL", "CAPITAL",
    "HOLDINGS LLC", "HOLDINGS INC", "SERVICES LLC", "SERVICES INC",
    "TRUSTEE", "N.A.", "NATIONAL ASSOCIATION", "NA", "CORPORATION", "CORP",
    "CHASE", "WELLS FARGO", "WELLS", "ROCKET", "QUICKEN", "CALIBER",
    "NATIONSTAR", "MR COOPER", "FREEDOM", "PENNYMAC", "CARRINGTON",
    "LAKEVIEW", "SELECT PORTFOLIO", "OCWEN", "SHELLPOINT", "NEWREZ",
    "BAYVIEW", "LOANDEPOT", "GUILD", "FAIRWAY", "ACADEMY",
    "ATTORNEYS", "ATTORNEY", "LAW FIRM", "ESCROW",
    "UNITED STATES OF AMERICA", "IRS", "INTERNAL REVENUE",
    "STATE OF ARIZONA", "DEPARTMENT OF REVENUE", "COUNTY OF",
}

NAME_SUFFIXES = {"JR", "SR", "II", "III", "IV", "ESQ", "MD", "DDS", "PHD"}
PUNCT_RE = re.compile(r"[.,&'\-/\\]")
WS_RE = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    """Uppercase, strip punctuation + suffixes, collapse whitespace."""
    if not name:
        return ""
    n = name.upper()
    n = PUNCT_RE.sub(" ", n)
    n = WS_RE.sub(" ", n).strip()
    tokens = [t for t in n.split() if t not in NAME_SUFFIXES]
    return " ".join(tokens)


def is_business_name(name: str) -> bool:
    """
    Return True if the name looks like a business/lender/trustee rather than
    an individual homeowner.

    Matching rules:
      1. Multi-word business phrases (e.g. "WELLS FARGO", "MR COOPER") use
         substring match — they don't collide with personal names.
      2. Short entity suffixes (LLC/INC/LP/CORP/NA) must appear as a standalone
         final or penultimate TOKEN — otherwise "BENALLY" would get flagged
         as a business because it contains "NA".
    """
    u = name.upper().strip()
    if not u:
        return False

    # Substring match — safe because these tokens are long enough to be unique
    for tok in BUSINESS_TOKENS:
        # Short acronym-like tokens need whole-word match
        if len(tok) <= 3:
            if tok in u.replace(",", " ").split():
                return True
        else:
            # Long phrases do substring match (WELLS FARGO, MR COOPER, etc.)
            if tok in u:
                return True

    # Entity-suffix tokens — must appear as a separate token, not inside a name
    tokens = u.replace(",", " ").replace(".", " ").split()
    if tokens:
        entity_suffixes = {
            "LLC", "INC", "LP", "LLP", "PLLC", "CORP", "CO",
            "LTD", "PA", "PC", "TRUST", "ASSOCIATION", "ASSN",
            "CHURCH", "FOUNDATION", "PARTNERS", "PARTNERSHIP", "GROUP",
            "HOLDINGS", "PROPERTIES", "INVESTMENTS", "INVESTMENT",
            "VENTURES", "ENTERPRISES", "REALTY", "DEVELOPMENT",
        }
        if tokens[-1] in entity_suffixes:
            return True
        if len(tokens) >= 2 and tokens[-2] in entity_suffixes:
            return True

    return False


def parse_parcel_owner(owner: str) -> list[tuple[str, str, str]]:
    """
    Split a parcel owner string into (last, first, normalized_full) tuples.
    Handles common patterns:
        "SMITH JOHN"                    → [(SMITH, JOHN, SMITH JOHN)]
        "SMITH JOHN L"                  → [(SMITH, JOHN L, SMITH JOHN L)]
        "SMITH JOHN/MARY A"             → two owners
        "SMITH JOHN L/JANE"             → two owners (second missing lastname)
        "SMITH JOHN L & JANE A"         → two owners (& separator)
        "SMITH FAMILY TRUST"            → trust, not-splittable, still returned
    """
    if not owner:
        return []
    norm = normalize_name(owner)
    if not norm:
        return []

    # Multiple-owner separators
    parts = re.split(r" / | & |/| & ", owner)
    parts = [p.strip() for p in parts if p.strip()]

    out: list[tuple[str, str, str]] = []
    primary_last: str | None = None
    for idx, p in enumerate(parts):
        pn = normalize_name(p)
        if not pn:
            continue
        toks = pn.split()
        if not toks:
            continue

        # Most AZ parcel owners are formatted LAST FIRST [M]
        if idx == 0:
            last = toks[0]
            primary_last = last
            first = " ".join(toks[1:]) if len(toks) > 1 else ""
        else:
            # Second+ owner — usually the spouse/co-owner. They almost always
            # share the primary owner's last name and only their first name
            # appears in the parcel record. So we inherit primary_last.
            # Only override if the second part has an explicit last name
            # (e.g. 2+ tokens where first token is 3+ chars AND differs from
            # any plausible first name — rare edge case).
            if primary_last:
                last = primary_last
                first = pn
            else:
                last = toks[0]
                first = " ".join(toks[1:]) if len(toks) > 1 else ""
        full_norm = f"{last} {first}".strip()
        out.append((last, first, full_norm))
    return out


def parse_recorder_name(name: str) -> tuple[str, str, str] | None:
    """Recorder names are mostly 'LAST FIRST [M]'. Returns (last, first, full_norm)."""
    if not name or is_business_name(name):
        return None
    norm = normalize_name(name)
    toks = norm.split()
    if len(toks) < 2:
        return None
    last = toks[0]
    first = " ".join(toks[1:])
    return (last, first, norm)


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------
def load_parcels_index() -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """
    Build two indexes over Maricopa parcels:
        exact_full_idx:  norm_full_name → [parcels...]
        lastname_idx:    last_name      → [(first, parcel), ...]
    """
    exact: dict[str, list[dict]] = defaultdict(list)
    by_last: dict[str, list[tuple[str, dict]]] = defaultdict(list)

    count = 0
    skipped_business = 0
    with PARCELS.open() as f:
        for line in f:
            try:
                p = json.loads(line)
            except Exception:
                continue
            if p.get("county") != "Maricopa":
                continue
            owner = p.get("owner") or ""
            # Skip LLC/corp/business parcels entirely — their owners appear on
            # recorder docs under personal names that map to different parcels.
            # Indexing them causes false matches like "ANDREW" → "ANDREW THE
            # HOME BUYER LLC" pulling in every dead Andrew in the county.
            if is_business_name(owner):
                skipped_business += 1
                count += 1
                continue
            for last, first, full in parse_parcel_owner(owner):
                # Require a first name for the index — last-name-only entries
                # (like "SMITH FAMILY TRUST") would create too many false hits.
                if not first:
                    continue
                # Require last name to be at least 3 chars to prevent
                # single-letter or short-token collisions
                if len(last) < 3:
                    continue
                if full:
                    exact[full].append(p)
                if last:
                    by_last[last].append((first, p))
            count += 1
            if count % 200000 == 0:
                log.info(f"  indexed {count:,} parcels…")
    log.info(f"parcel index: {count:,} parcels, "
             f"{skipped_business:,} business/LLC skipped, "
             f"{sum(len(v) for v in exact.values()):,} exact-full entries, "
             f"{len(by_last):,} unique last names")
    return exact, by_last


def match_name(last: str, first: str, full: str,
               exact_idx, by_last_idx) -> tuple[dict | None, int]:
    """Return (parcel, tier) or (None, 0).

    Strict-first matching: only accept when the evidence is clear. Ambiguous
    matches (multiple same-name parcels, no first name, short tokens) are
    rejected — better to leave a signal unmatched than to pin it on the
    wrong property.
    """
    # Reject obviously-weak inputs
    if not last or len(last) < 3 or not first:
        return None, 0

    # Tier 1 — exact full-name match
    hits = exact_idx.get(full, [])
    if hits:
        unique_apns = {p.get("apn") for p in hits}
        if len(unique_apns) == 1:
            # Same person, one property — perfect match
            return hits[0], 1
        # Multiple distinct properties with this exact owner name.
        # We can't tell which one from just a name → reject as ambiguous.
        return None, 0

    # Tier 2 — last-name + first-name starts-with, must be unique
    pool = by_last_idx.get(last, [])
    if not pool:
        return None, 0

    first_tok = first.split()[0] if first else ""
    if not first_tok or len(first_tok) < 2:
        return None, 0

    candidates = [p for (f, p) in pool
                  if f and f.split()
                  and (f.split()[0].startswith(first_tok)
                       or first_tok.startswith(f.split()[0]))]
    # Deduplicate by APN (same owner on same parcel counts once)
    seen = set()
    unique = []
    for p in candidates:
        apn = p.get("apn")
        if apn and apn not in seen:
            seen.add(apn)
            unique.append(p)

    if len(unique) == 1:
        return unique[0], 2

    # Ambiguous → reject. Safer than pinning on wrong property.
    return None, 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    if not RECORDER_IN.exists():
        log.error(f"missing input: {RECORDER_IN}")
        log.error("run scrapers/maricopa_recorder_api.py first")
        sys.exit(1)
    if not PARCELS.exists():
        log.error(f"missing parcel master: {PARCELS}")
        sys.exit(1)

    log.info("building parcel name indexes…")
    exact_idx, by_last_idx = load_parcels_index()

    # Stream in & rewrite with apn resolution
    log.info(f"matching recorder records from {RECORDER_IN}…")

    records_in = list(RECORDER_IN.open())

    # First pass: detect trustees/attorneys/repeat-signers.
    # Anyone whose name appears on 5+ different recorder filings in the scrape
    # window is almost certainly a trustee, attorney, or agent (like Leonard J
    # McDonald who signs most Maricopa foreclosure docs). Their names are a
    # professional artifact, not a personal distress signal — filter them out.
    name_frequency: dict[str, int] = {}
    for line in records_in:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        for n in rec.get("names") or []:
            if is_business_name(n):
                continue
            nn = normalize_name(n)
            if nn:
                name_frequency[nn] = name_frequency.get(nn, 0) + 1

    TRUSTEE_THRESHOLD = 5
    repeat_signers = {n for n, c in name_frequency.items() if c >= TRUSTEE_THRESHOLD}
    if repeat_signers:
        log.info(f"  detected {len(repeat_signers)} repeat-signer names "
                 f"(appear in {TRUSTEE_THRESHOLD}+ filings — filtering as trustees/attorneys)")
        # Log top 10 for debugging
        top = sorted(((c, n) for n, c in name_frequency.items() if c >= TRUSTEE_THRESHOLD),
                     reverse=True)[:10]
        for count, name in top:
            log.info(f"    trustee: {name} ({count} filings)")

    matched = 0
    ambiguous = 0
    unresolved = 0
    resolved_records: list[dict] = []
    unresolved_records: list[dict] = []

    for line in records_in:
        try:
            rec = json.loads(line)
        except Exception:
            continue

        names = rec.get("names") or []
        if not names:
            unresolved += 1
            unresolved_records.append(rec)
            continue

        # Try each name on the record in order; first resolution wins, but we
        # keep scanning for ambiguity warnings. Skip trustees/attorneys.
        best_tier = 0
        best_parcel: dict | None = None
        for n in names:
            parsed = parse_recorder_name(n)
            if not parsed:
                continue
            last, first, full = parsed
            if full in repeat_signers:
                continue   # known trustee/attorney, not a homeowner
            parcel, tier = match_name(last, first, full, exact_idx, by_last_idx)
            if parcel and (best_tier == 0 or tier < best_tier):
                best_tier = tier
                best_parcel = parcel
                if tier == 1:
                    break

        if best_parcel:
            rec["apn"] = best_parcel.get("apn")
            rec["apn_norm"] = best_parcel.get("apn_norm")
            rec["match_tier"] = best_tier
            rec["matched_owner"] = best_parcel.get("owner")
            resolved_records.append(rec)
            if best_tier == 3:
                ambiguous += 1
            matched += 1
        else:
            unresolved += 1
            unresolved_records.append(rec)

    # Rewrite input in place (resolved only)
    with RECORDER_IN.open("w") as f:
        for r in resolved_records:
            f.write(json.dumps(r) + "\n")

    # Save unresolved for manual review
    with UNRESOLVED_OUT.open("w") as f:
        for r in unresolved_records:
            f.write(json.dumps(r) + "\n")

    total = matched + unresolved
    log.info(f"✓ matched {matched:,} / {total:,} ({100*matched/max(total,1):.1f}%)")
    if ambiguous:
        log.info(f"  ⚠ {ambiguous:,} were tier-3 ambiguous (flagged in dashboard)")
    log.info(f"  unresolved → {UNRESOLVED_OUT.name} ({unresolved:,} records)")
    log.info(f"  resolved   → {RECORDER_IN.name}    ({matched:,} records)")


if __name__ == "__main__":
    main()
