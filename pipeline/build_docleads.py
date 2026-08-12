#!/usr/bin/env python3
"""
Build the doc-type lead list — Universal County Intelligence Framework v5.5.0.

DOCTRINE: every lead is a real recorded document. The lead TYPE is the document
TYPE. There are no scores, weights, combo bonuses, distress patterns, or tiers.
The parcel master is demoted to an ENRICHMENT lookup (APN -> address / owner /
mailing); it no longer generates leads. Heuristic flags (absentee, out-of-state,
long-hold) are optional display ANNOTATIONS, never lead generators.

Inputs (document stores):
    data/maricopa_recorder_docs.jsonl   Maricopa recorder API (names, no APN)
    data/pima_recorder_docs.jsonl       Pima GIS layer-12 deed transfers (resolved)
    data/pima_tax_docs.jsonl            Pima treasurer delinquency (resolved, optional)

Enrichment:
    data/maricopa_parcels.jsonl, data/pima_parcels.jsonl

Foreclosure lifecycle: a "NTS Cancelled" (CQ) or "Trustee's Deed" (TD) document
is not a standalone lead — it CLOSES its matching Notice of Trustee Sale (NS),
marking that lead cancelled or completed. Matching is by resolved APN or by
shared party name, NS on/before the closing doc, within a lookback window.

Output:
    data/leads.json   documents as leads, newest first, with per-doc-type counts
"""
from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from pipeline.match_recorder import (
    build_name_index, resolve_names, detect_repeat_signers, normalize_name,
    is_business_name,
)

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("build_docleads")

FRESH_DAYS = 14         # "New" rotation pool = documents recorded in the last N days
                        # (14 not 7: the Pima GIS deed feed lags ~2 weeks, so a
                        #  tighter window would surface Maricopa only)
DASHBOARD_CAP = 60_000  # generous; doc-leads are far smaller than the old score pool

# Foreclosure lifecycle
NS_CODE = "NS"
CLOSERS = {"CQ": "cancelled", "TD": "completed"}
KILL_LOOKBACK_DAYS = 400   # an NS may sit up to ~13 months before sale/cancel
# The cumulative doc store starts empty and fills over successive daily runs
# (retention-capped). Until it has observed a full lookback of closer history,
# an "active" NS can't be confirmed — a cancellation/completion may exist in the
# unscanned past. We mark those NS provisional so the UI never reads them as
# definitively active. Self-heals once coverage reaches this many days.
CONFIRM_COVERAGE_DAYS = 180


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------
def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        log.info(f"missing (skipping): {path.name}")
        return []
    out = []
    with path.open() as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    log.info(f"loaded {len(out):,} from {path.name}")
    return out


def load_parcels(path: Path) -> list[dict]:
    if not path.exists():
        log.warning(f"missing parcel master: {path.name}")
        return []
    out = []
    with path.open() as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    log.info(f"loaded {len(out):,} parcels from {path.name}")
    return out


def parse_date(s) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_year(s) -> int:
    try:
        return int(str(s).strip()[:4])
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Enrichment (parcel master -> address/owner/mail + display annotations)
# ---------------------------------------------------------------------------
def enrich_from_parcel(lead: dict, parcel: dict | None, today: date) -> None:
    """Fill address/owner/mail from the parcel master, then compute display-only
    annotations. Never changes whether something is a lead."""
    if parcel:
        for src, dst in [("owner", "owner"), ("site_address", "site_address"),
                         ("site_city", "site_city"), ("site_zip", "site_zip"),
                         ("mail_address", "mail_address"), ("mail_city", "mail_city"),
                         ("mail_state", "mail_state"), ("mail_zip", "mail_zip"),
                         ("fcv", "fcv"), ("year_built", "year_built"),
                         ("last_sale_date", "last_sale_date"),
                         ("latitude", "latitude"), ("longitude", "longitude")]:
            if lead.get(dst) in (None, "") and parcel.get(src) not in (None, ""):
                lead[dst] = parcel.get(src)

    ann = {}
    mail_state = (lead.get("mail_state") or "").strip().upper()
    site_city = (lead.get("site_city") or "").strip().upper()
    mail_city = (lead.get("mail_city") or "").strip().upper()
    if mail_city and site_city and mail_city != site_city:
        ann["absentee"] = True
    if mail_state and mail_state != "AZ":
        ann["out_of_state"] = True
    lsd = parse_date(lead.get("last_sale_date"))
    if lsd and (today - lsd).days >= 3652:      # 10+ years
        ann["long_hold"] = True
    if ann:
        lead["annotations"] = ann


# ---------------------------------------------------------------------------
# Foreclosure lifecycle (CQ/TD close their matching NS)
# ---------------------------------------------------------------------------
def person_names(names: list[str]) -> set[str]:
    return {normalize_name(n) for n in (names or [])
            if n and not is_business_name(n) and normalize_name(n)}


def apply_foreclosure_lifecycle(docs: list[dict], today: date) -> tuple[list[dict], dict]:
    """Return (leads_kept, stats). NS docs get a status; CQ/TD docs are consumed
    as closers of their matching NS and removed from the lead list."""
    ns_leads = [d for d in docs if d.get("doc_code") == NS_CODE]
    closers = [d for d in docs if d.get("doc_code") in CLOSERS]
    others = [d for d in docs if d.get("doc_code") != NS_CODE and d.get("doc_code") not in CLOSERS]

    # How far back does our foreclosure (NS + closer) visibility reach? An active
    # NS is only *confirmed* active if we have observed closers continuously from
    # its recording date to now — i.e. coverage extends at least CONFIRM_COVERAGE_DAYS.
    fore_dates = [parse_date(d.get("recorded_date")) for d in ns_leads + closers]
    fore_dates = [d for d in fore_dates if d]
    coverage_start = min(fore_dates) if fore_dates else today
    coverage_days = (today - coverage_start).days
    history_confident = coverage_days >= CONFIRM_COVERAGE_DAYS

    for n in ns_leads:
        n["status"] = "active"
        # provisional unless we have confirmed closer history back past this NS
        nd = parse_date(n.get("recorded_date"))
        n["status_provisional"] = (not history_confident) or (nd is not None and nd < coverage_start)

    # index NS by APN and by each person-name
    by_apn: dict[str, list[dict]] = defaultdict(list)
    by_name: dict[str, list[dict]] = defaultdict(list)
    for n in ns_leads:
        if n.get("apn_norm"):
            by_apn[n["apn_norm"]].append(n)
        for pn in person_names(n.get("names")):
            by_name[pn].append(n)

    matched_closers = 0
    for c in closers:
        cdate = parse_date(c.get("recorded_date"))
        cand: list[dict] = []
        if c.get("apn_norm") and c["apn_norm"] in by_apn:
            cand = by_apn[c["apn_norm"]]
        else:
            seen = set()
            for pn in person_names(c.get("names")):
                for n in by_name.get(pn, []):
                    if id(n) not in seen:
                        seen.add(id(n))
                        cand.append(n)
        # nearest prior NS within the lookback
        best, best_gap = None, None
        for n in cand:
            ndate = parse_date(n.get("recorded_date"))
            if not ndate or not cdate:
                continue
            gap = (cdate - ndate).days
            if 0 <= gap <= KILL_LOOKBACK_DAYS and (best_gap is None or gap < best_gap):
                best, best_gap = n, gap
        if best is not None:
            best["status"] = CLOSERS[c["doc_code"]]
            best["status_provisional"] = False   # closed by an observed document
            best["status_doc"] = {
                "doc_type": c.get("doc_type"), "doc_number": c.get("doc_number"),
                "recorded_date": c.get("recorded_date"),
            }
            matched_closers += 1

    stats = {
        "ns_total": len(ns_leads),
        "ns_cancelled": sum(1 for n in ns_leads if n.get("status") == "cancelled"),
        "ns_completed": sum(1 for n in ns_leads if n.get("status") == "completed"),
        "ns_active_confirmed": sum(1 for n in ns_leads
                                   if n.get("status") == "active" and not n.get("status_provisional")),
        "ns_active_provisional": sum(1 for n in ns_leads
                                     if n.get("status") == "active" and n.get("status_provisional")),
        "closers_total": len(closers),
        "closers_matched": matched_closers,
        "closers_dropped": len(closers) - matched_closers,
        "coverage_start": coverage_start.isoformat(),
        "coverage_days": coverage_days,
        "history_confident": history_confident,
        "confirm_coverage_days": CONFIRM_COVERAGE_DAYS,
    }
    return others + ns_leads, stats


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def build(limit_dashboard: int = DASHBOARD_CAP) -> dict:
    now = datetime.now(timezone.utc)
    today = now.date()
    now_iso = now.isoformat(timespec="seconds").replace("+00:00", "Z")

    # ---- parcel masters -> enrichment indexes --------------------------------
    maricopa_parcels = load_parcels(DATA_DIR / "maricopa_parcels.jsonl")
    pima_parcels = load_parcels(DATA_DIR / "pima_parcels.jsonl")

    apn_index: dict[tuple, dict] = {}
    for p in maricopa_parcels + pima_parcels:
        k = (p.get("county"), (p.get("apn_norm") or p.get("apn") or "").upper())
        if k[1]:
            apn_index[k] = p

    # ---- document stores -----------------------------------------------------
    mar_docs = load_jsonl(DATA_DIR / "maricopa_recorder_docs.jsonl")
    pima_docs = load_jsonl(DATA_DIR / "pima_recorder_docs.jsonl")
    tax_docs = load_jsonl(DATA_DIR / "pima_tax_docs.jsonl")

    # ---- resolve Maricopa docs by name --------------------------------------
    if mar_docs:
        exact_idx, by_last_idx = build_name_index(
            [p for p in maricopa_parcels if p.get("county") == "Maricopa"])
        repeat = detect_repeat_signers(mar_docs)
        log.info(f"{len(repeat):,} repeat-signer (trustee/attorney) names filtered")
        res_count = 0
        for d in mar_docs:
            parcel, tier = resolve_names(d.get("names", []), exact_idx, by_last_idx, repeat)
            if parcel:
                d["apn"] = parcel.get("apn")
                d["apn_norm"] = parcel.get("apn_norm")
                d["match_tier"] = tier
                d["resolved"] = True
                res_count += 1
            else:
                d["resolved"] = False
        log.info(f"Maricopa: resolved {res_count:,}/{len(mar_docs):,} "
                 f"({100*res_count/max(len(mar_docs),1):.1f}%) to a parcel")

    all_docs = mar_docs + pima_docs + tax_docs

    # ---- foreclosure lifecycle (CQ/TD close NS) ------------------------------
    leads, life_stats = apply_foreclosure_lifecycle(all_docs, today)
    log.info(f"foreclosure lifecycle: {life_stats['ns_total']:,} NS "
             f"({life_stats['ns_cancelled']:,} cancelled, {life_stats['ns_completed']:,} completed, "
             f"{life_stats['ns_active_confirmed']:,} active-confirmed, "
             f"{life_stats['ns_active_provisional']:,} active-provisional); "
             f"{life_stats['closers_matched']:,}/{life_stats['closers_total']:,} closers matched")
    if not life_stats["history_confident"]:
        log.info(f"  ⚠ closer history only spans {life_stats['coverage_days']}d "
                 f"(need {life_stats['confirm_coverage_days']}d) — active NS shown provisional until then")

    # ---- enrichment + annotations -------------------------------------------
    for lead in leads:
        parcel = apn_index.get((lead.get("county"), (lead.get("apn_norm") or "").upper()))
        enrich_from_parcel(lead, parcel, today)

    # ---- freshness rotation (pool = recorded in last N days) -----------------
    fresh_cut = (today - timedelta(days=FRESH_DAYS)).isoformat()
    fresh = 0
    for lead in leads:
        if (lead.get("recorded_date") or "") >= fresh_cut:
            lead["daily_opportunity"] = True
            fresh += 1
    log.info(f"fresh pool (recorded since {fresh_cut}): {fresh:,} documents")

    # ---- sort newest first ---------------------------------------------------
    leads.sort(key=lambda d: (d.get("recorded_date") or "", d.get("doc_number") or ""),
               reverse=True)

    # ---- per-doc-type counts per county -------------------------------------
    counts: dict[str, Counter] = defaultdict(Counter)
    cat_of: dict[str, str] = {}
    for lead in leads:
        counts[lead["county"]][lead["doc_type"]] += 1
        cat_of[lead["doc_type"]] = lead.get("category", "Other")
    doc_type_counts = {c: dict(t.most_common()) for c, t in counts.items()}

    resolved = sum(1 for l in leads if l.get("resolved"))
    dates = [l["recorded_date"] for l in leads if l.get("recorded_date")]

    out = {
        "generated_at": now_iso,
        "doctrine": "UCIF v5.5.0 — lead = recorded document; type = doc type; no scores/tiers",
        "counties": ["Maricopa", "Pima"],
        "total_leads": len(leads),
        "resolved": resolved,
        "unresolved": len(leads) - resolved,
        "fresh_days": FRESH_DAYS,
        "date_range": {"min": min(dates) if dates else None, "max": max(dates) if dates else None},
        "doc_type_counts": doc_type_counts,
        "doc_type_category": cat_of,
        "foreclosure_lifecycle": life_stats,
        "leads": leads[:limit_dashboard],
    }
    out_path = DATA_DIR / "leads.json"
    out_path.write_text(json.dumps(out, default=str))
    log.info(f"✓ wrote {len(leads[:limit_dashboard]):,} leads → {out_path}")
    return out


if __name__ == "__main__":
    build()
