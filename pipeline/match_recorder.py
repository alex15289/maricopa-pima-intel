#!/usr/bin/env python3
"""
Name -> parcel matching for Maricopa recorder documents.

Maricopa recorder docs carry party NAMES, not APNs, so we join them to the
parcel master by owner name. This module is the matching authority: the
doc-centric pipeline (build_docleads.py) imports build_name_index() +
resolve_names() to pin each document to a property.

Matching is strict-first — we would rather leave a document unresolved (still a
valid lead, exported with its name + doc number) than pin it to the wrong
parcel:
    Tier 1  exact full-name match, unique parcel
    Tier 2  last-name + first-name-prefix, unique parcel
    (ambiguous / business / trustee names -> unresolved)

Run standalone to annotate a docs file in place (debugging / one-off):
    python pipeline/match_recorder.py            # uses data/maricopa_recorder_docs.jsonl
"""
from __future__ import annotations

import json
import logging
import re
import sys
from collections import defaultdict
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DOCS_IN = DATA_DIR / "maricopa_recorder_docs.jsonl"
PARCELS = DATA_DIR / "maricopa_parcels.jsonl"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("match_recorder")

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
ENTITY_SUFFIXES = {
    "LLC", "INC", "LP", "LLP", "PLLC", "CORP", "CO", "LTD", "PA", "PC", "TRUST",
    "ASSOCIATION", "ASSN", "CHURCH", "FOUNDATION", "PARTNERS", "PARTNERSHIP",
    "GROUP", "HOLDINGS", "PROPERTIES", "INVESTMENTS", "INVESTMENT", "VENTURES",
    "ENTERPRISES", "REALTY", "DEVELOPMENT",
}
PUNCT_RE = re.compile(r"[.,&'\-/\\]")
WS_RE = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    if not name:
        return ""
    n = WS_RE.sub(" ", PUNCT_RE.sub(" ", name.upper())).strip()
    return " ".join(t for t in n.split() if t not in NAME_SUFFIXES)


def is_business_name(name: str) -> bool:
    u = (name or "").upper().strip()
    if not u:
        return False
    for tok in BUSINESS_TOKENS:
        if len(tok) <= 3:
            if tok in u.replace(",", " ").split():
                return True
        elif tok in u:
            return True
    tokens = u.replace(",", " ").replace(".", " ").split()
    if tokens and (tokens[-1] in ENTITY_SUFFIXES or
                   (len(tokens) >= 2 and tokens[-2] in ENTITY_SUFFIXES)):
        return True
    return False


def parse_parcel_owner(owner: str) -> list[tuple[str, str, str]]:
    if not owner or not normalize_name(owner):
        return []
    parts = [p.strip() for p in re.split(r" / | & |/| & ", owner) if p.strip()]
    out, primary_last = [], None
    for idx, p in enumerate(parts):
        toks = normalize_name(p).split()
        if not toks:
            continue
        if idx == 0:
            last = toks[0]
            primary_last = last
            first = " ".join(toks[1:]) if len(toks) > 1 else ""
        elif primary_last:
            last, first = primary_last, " ".join(toks)
        else:
            last = toks[0]
            first = " ".join(toks[1:]) if len(toks) > 1 else ""
        out.append((last, first, f"{last} {first}".strip()))
    return out


def parse_recorder_name(name: str) -> tuple[str, str, str] | None:
    if not name or is_business_name(name):
        return None
    toks = normalize_name(name).split()
    if len(toks) < 2:
        return None
    return (toks[0], " ".join(toks[1:]), " ".join(toks))


# ---------------------------------------------------------------------------
# Index + resolve API (imported by build_docleads.py)
# ---------------------------------------------------------------------------
def build_name_index(parcels: list[dict]) -> tuple[dict, dict]:
    """From a list of Maricopa parcel dicts, build (exact_full_idx, by_last_idx).

    Indexes both the primary owner (OWNER_NAME) and the in-care-of party
    (owner_2 / INCAREOF) — the latter is populated on ~13% of parcels and often
    carries a real person name the owner field doesn't."""
    exact: dict[str, list[dict]] = defaultdict(list)
    by_last: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    skipped = incareof = 0
    for p in parcels:
        for field, is_secondary in (("owner", False), ("owner_2", True)):
            name = p.get(field) or ""
            if not name or is_business_name(name):
                if field == "owner" and name:
                    skipped += 1
                continue
            for last, first, full in parse_parcel_owner(name):
                if not first or len(last) < 3:
                    continue
                exact[full].append(p)
                by_last[last].append((first, p))
                if is_secondary:
                    incareof += 1
    log.info(f"name index: {len(parcels):,} parcels, {skipped:,} business/LLC skipped, "
             f"{len(exact):,} exact names ({incareof:,} from in-care-of), "
             f"{len(by_last):,} last names")
    return exact, by_last


def match_name(last: str, first: str, full: str, exact_idx, by_last_idx) -> tuple[dict | None, int]:
    if not last or len(last) < 3 or not first:
        return None, 0
    hits = exact_idx.get(full, [])
    if hits:
        if len({p.get("apn") for p in hits}) == 1:
            return hits[0], 1
        return None, 0
    pool = by_last_idx.get(last, [])
    if not pool:
        return None, 0
    first_tok = first.split()[0] if first else ""
    if len(first_tok) < 2:
        return None, 0
    cands, seen = [], set()
    for f, p in pool:
        if not f or not f.split():
            continue
        ft = f.split()[0]
        if (ft.startswith(first_tok) or first_tok.startswith(ft)) and p.get("apn") not in seen:
            seen.add(p.get("apn"))
            cands.append(p)
    return (cands[0], 2) if len(cands) == 1 else (None, 0)


def candidate_parcels(last: str, first: str, full: str, exact_idx, by_last_idx) -> dict[str, dict]:
    """All parcels a person name *could* be, as {apn: parcel}. Looser than
    match_name (it returns the whole ambiguous set instead of rejecting it) —
    used only for co-party intersection."""
    out: dict[str, dict] = {}
    if not last or len(last) < 3 or not first:
        return out
    for p in exact_idx.get(full, []):
        if p.get("apn"):
            out[p["apn"]] = p
    if out:
        return out
    first_tok = first.split()[0] if first else ""
    if len(first_tok) < 2:
        return out
    for f, p in by_last_idx.get(last, []):
        if f and f.split():
            ft = f.split()[0]
            if (ft.startswith(first_tok) or first_tok.startswith(ft)) and p.get("apn"):
                out[p["apn"]] = p
    return out


def resolve_by_coparty(names: list[str], exact_idx, by_last_idx, repeat_signers: set[str]
                       ) -> dict | None:
    """When a document lists multiple person parties (e.g. a borrower couple),
    each name alone may be ambiguous, but the parcel they SHARE is usually
    unique. Intersect the candidate parcels of the doc's person names; if
    exactly one parcel is common to 2+ names, that's a confident resolve."""
    per_name = []
    for n in names or []:
        parsed = parse_recorder_name(n)
        if not parsed:
            continue
        last, first, full = parsed
        if full in repeat_signers:
            continue
        cands = candidate_parcels(last, first, full, exact_idx, by_last_idx)
        if cands:
            per_name.append(cands)
    if len(per_name) < 2:
        return None
    # intersect APN sets across the contributing names
    common = set(per_name[0])
    for c in per_name[1:]:
        common &= set(c)
    if len(common) == 1:
        apn = next(iter(common))
        # return the parcel object from whichever name carried it
        for c in per_name:
            if apn in c:
                return c[apn]
    return None


def detect_repeat_signers(docs: list[dict], threshold: int = 5) -> set[str]:
    """Names appearing on N+ filings are trustees/attorneys/agents, not owners."""
    freq: dict[str, int] = defaultdict(int)
    for rec in docs:
        for n in rec.get("names") or []:
            if not is_business_name(n):
                nn = normalize_name(n)
                if nn:
                    freq[nn] += 1
    return {n for n, c in freq.items() if c >= threshold}


def resolve_names(names: list[str], exact_idx, by_last_idx, repeat_signers: set[str]
                  ) -> tuple[dict | None, int]:
    """Try each party name; return (parcel, tier) for the best (lowest-tier) hit.
    Tiers: 1 exact-unique, 2 lastname+firstprefix-unique, 3 co-party intersection."""
    best_parcel, best_tier = None, 0
    for n in names or []:
        parsed = parse_recorder_name(n)
        if not parsed:
            continue
        last, first, full = parsed
        if full in repeat_signers:
            continue
        parcel, tier = match_name(last, first, full, exact_idx, by_last_idx)
        if parcel and (best_tier == 0 or tier < best_tier):
            best_parcel, best_tier = parcel, tier
            if tier == 1:
                break
    if best_parcel:
        return best_parcel, best_tier
    # No single name resolved cleanly — try the co-party intersection (tier 3)
    parcel = resolve_by_coparty(names, exact_idx, by_last_idx, repeat_signers)
    if parcel:
        return parcel, 3
    return None, 0


# ---------------------------------------------------------------------------
# Standalone CLI (debugging): annotate the docs file in place
# ---------------------------------------------------------------------------
def main() -> None:
    if not DOCS_IN.exists() or not PARCELS.exists():
        log.error(f"need {DOCS_IN.name} and {PARCELS.name}")
        sys.exit(1)
    parcels = [json.loads(l) for l in PARCELS.open() if l.strip()]
    parcels = [p for p in parcels if p.get("county") == "Maricopa"]
    exact_idx, by_last_idx = build_name_index(parcels)
    docs = [json.loads(l) for l in DOCS_IN.open() if l.strip()]
    repeat = detect_repeat_signers(docs)
    log.info(f"{len(repeat):,} repeat-signer names filtered as trustees/attorneys")
    matched = 0
    for rec in docs:
        parcel, tier = resolve_names(rec.get("names", []), exact_idx, by_last_idx, repeat)
        if parcel:
            rec["apn"] = parcel.get("apn")
            rec["apn_norm"] = parcel.get("apn_norm")
            rec["match_tier"] = tier
            rec["resolved"] = True
            matched += 1
        else:
            rec["resolved"] = False
    with DOCS_IN.open("w") as f:
        for rec in docs:
            f.write(json.dumps(rec) + "\n")
    log.info(f"✓ matched {matched:,}/{len(docs):,} ({100*matched/max(len(docs),1):.1f}%)")


if __name__ == "__main__":
    main()
