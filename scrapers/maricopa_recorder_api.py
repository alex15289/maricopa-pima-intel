#!/usr/bin/env python3
"""
Maricopa County Recorder — public API document scraper (doc-type edition).

Endpoint:
    GET https://publicapi.recorder.maricopa.gov/documents/search

Query params: documentCode (2-letter QUERY code — see below), beginDate,
endDate (ISO), pageSize/maxResults (<=200), pageNumber. Deep pagination past
the totalResults=501 display cap works. Detail endpoint /documents/{rn}
returns party names + restricted flag. Coverage back to 1871.

IMPORTANT — query codes vs display codes:
    The API filters on the 2-letter codes from the county's own search
    dropdown (legacy.recorder.maricopa.gov/recdocdata/). The `documentCode`
    string in RESULTS is a different abbreviation (e.g. query "NS" returns
    display "N/TR SALE"). Never query with display strings. The old scraper
    got this wrong: "STL" actually matched SUBSTITUTION OF TRUSTEE and "FTL"
    matched nothing. Full 268-code map captured 2026-08-11 in Phase 0 recon.

Incremental model:
    Each run scrapes a short window (default 3 days) and MERGES into the
    cumulative store data/maricopa_recorder_docs.jsonl, deduping by
    recording_number and pruning records older than --retention days.
    First-time backfill: --days 30 (or more) --retention 180.

Usage:
    python scrapers/maricopa_recorder_api.py --days 3            # daily run
    python scrapers/maricopa_recorder_api.py --days 30           # backfill
    python scrapers/maricopa_recorder_api.py --days 7 --codes NS,CQ
    python scrapers/maricopa_recorder_api.py --days 3 --no-details

Output:
    data/maricopa_recorder_docs.jsonl   (cumulative document store)
"""
from __future__ import annotations
import argparse
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE = "https://publicapi.recorder.maricopa.gov"
SEARCH_URL = f"{BASE}/documents/search"
DETAIL_URL = f"{BASE}/documents/{{rn}}"

HEADERS = {
    "User-Agent": "MaricopaIntel/1.0 (public-records research)",
    "Accept": "application/json",
    "Origin": "https://recorder.maricopa.gov",
    "Referer": "https://recorder.maricopa.gov/",
}

# Doc-type registry — QUERY code -> canonical lead type.
# Codes verified live against the API on 2026-08-11 (Phase 0 recon).
# `category` groups the dashboard sidebar. `kill` marks docs that close out
# other leads instead of only being leads themselves.
DOC_TYPES = {
    # Foreclosure family
    "NS": {"label": "Notice of Trustee Sale",  "category": "Foreclosure"},
    "CQ": {"label": "NTS Cancelled",           "category": "Foreclosure", "kill": "cancelled"},
    "TD": {"label": "Trustee's Deed",          "category": "Foreclosure", "kill": "completed"},
    "RQ": {"label": "Request for Notice of Sale", "category": "Foreclosure"},
    "SM": {"label": "Statement of Breach",     "category": "Foreclosure"},
    # Legal pressure
    "LP": {"label": "Lis Pendens",             "category": "Legal"},
    "JG": {"label": "Judgment",                "category": "Legal"},
    "DV": {"label": "Divorce Decree",          "category": "Legal"},
    "BK": {"label": "Bankruptcy",              "category": "Legal"},
    # Tax & liens
    "FL": {"label": "Federal Tax Lien",        "category": "Tax & Liens"},
    "SL": {"label": "State Tax Lien",          "category": "Tax & Liens"},
    "AH": {"label": "AHCCCS Lien",             "category": "Tax & Liens"},
    "ML": {"label": "Mechanics Lien",          "category": "Tax & Liens"},
    "AV": {"label": "Assessment/Violation",    "category": "Tax & Liens"},
    # Death & estate
    "DC": {"label": "Death Certificate",       "category": "Estate"},
    "OS": {"label": "Death Certificate (Out of State)", "category": "Estate"},
    "PJ": {"label": "Probate Judgment",        "category": "Estate"},
    "PD": {"label": "Probate Deed",            "category": "Estate"},
    # Estate planning — pre-death counterpart to the Estate types. A beneficiary
    # (transfer-on-death) deed signals an aging owner arranging succession; the
    # property will pass to heirs. Added from the Part A recon (Aug 2026): >500/mo,
    # all-family parties. Does NOT vest the parcel (title stays with the owner
    # until death), so it resolves by name, not the deed# join.
    "BB": {"label": "Beneficiary Deed",        "category": "Estate Planning"},
    # HOA / assessment delinquency. Real signal but routine (Sun City rec-center
    # + HOA assessment liens), so it ships OFF by default in the dashboard.
    "LN": {"label": "HOA/Assessment Lien",     "category": "Tax & Liens"},
    # Transfers worth watching
    "QD": {"label": "Quit Claim Deed",         "category": "Transfers"},
}

DEFAULT_CODES = list(DOC_TYPES.keys())

PAGE_SIZE = 200        # API hard limit: pageSize/maxResults <= 200
REQUEST_DELAY = 0.55
MAX_RETRIES = 4
MAX_PAGES_PER_WINDOW = 60   # 12K records/window safety ceiling
RECHECK_DAYS = 14          # re-fetch details for still-nameless docs recorded
                           # within this window (recorder name-index lag backfill)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
STORE_PATH = DATA_DIR / "maricopa_recorder_docs.jsonl"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("maricopa_recorder")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def get_json(session: requests.Session, url: str, params: dict | None = None) -> dict | None:
    for attempt in range(MAX_RETRIES):
        try:
            r = session.get(url, params=params, timeout=30)
        except requests.RequestException as e:
            wait = (2 ** attempt) * 1.5
            log.warning(f"network error: {e}; retrying in {wait:.1f}s")
            time.sleep(wait)
            continue
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                log.error(f"non-JSON 200 from {url}")
                return None
        if r.status_code in (429, 502, 503, 504):
            retry_after = int(r.headers.get("Retry-After", "0")) or (2 ** attempt) * 2
            log.warning(f"HTTP {r.status_code}; sleeping {retry_after}s")
            time.sleep(retry_after)
            continue
        if r.status_code == 404:
            return None
        log.error(f"HTTP {r.status_code} on {url} — {r.text[:200]}")
        return None
    log.error(f"gave up on {url}")
    return None


# ---------------------------------------------------------------------------
# Search — paginate the whole window (deep pagination works past the cap)
# ---------------------------------------------------------------------------
def search_code(session: requests.Session, code: str, begin: date, end: date) -> list[dict]:
    out: list[dict] = []
    page = 1
    while page <= MAX_PAGES_PER_WINDOW:
        params = {
            "businessNames": "", "firstNames": "", "lastNames": "", "middleNameIs": "",
            "documentCode": code,
            "beginDate": begin.isoformat(), "endDate": end.isoformat(),
            "pageSize": PAGE_SIZE, "pageNumber": page, "maxResults": PAGE_SIZE,
        }
        data = get_json(session, SEARCH_URL, params=params)
        time.sleep(REQUEST_DELAY)
        results = (data or {}).get("searchResults") or []
        if not results:
            break
        out.extend(results)
        if len(results) < PAGE_SIZE:
            break
        page += 1
    if page > MAX_PAGES_PER_WINDOW:
        log.warning(f"  {code}: pagination ceiling hit — window too wide, results truncated")
    return out


def fetch_names(session: requests.Session, rn: str) -> tuple[list[str], bool]:
    """Returns (names, restricted) from the per-document detail endpoint."""
    data = get_json(session, DETAIL_URL.format(rn=rn))
    time.sleep(REQUEST_DELAY)
    if not data:
        return [], False
    names = data.get("names") or []
    if isinstance(names, str):
        names = [n.strip() for n in names.split(",") if n.strip()]
    return [n.strip() for n in names if isinstance(n, str) and n.strip()], bool(data.get("restricted"))


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------
def parse_recording_date(s: str | None) -> str | None:
    if not s:
        return None
    for fmt in ("%m-%d-%Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return s.strip()


def build_record(raw: dict, code: str, names: list[str], restricted: bool) -> dict:
    meta = DOC_TYPES.get(code, {"label": code, "category": "Other"})
    return {
        "county":        "Maricopa",
        "source":        "maricopa_recorder_api",
        "doc_code":      code,
        "doc_type":      meta["label"],
        "category":      meta["category"],
        "display_code":  raw.get("documentCode") or "",
        "doc_number":    str(raw.get("recordingNumber", "")),
        "recorded_date": parse_recording_date(raw.get("recordingDate")),
        "names":         names,
        "restricted":    restricted,
        "fetched_at":    datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


def load_store() -> dict[str, dict]:
    docs: dict[str, dict] = {}
    if STORE_PATH.exists():
        with STORE_PATH.open() as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    docs[rec["doc_number"]] = rec
                except Exception:
                    continue
    return docs


def run(codes: list[str], days_back: int, retention_days: int, fetch_details: bool) -> None:
    end = date.today()
    begin = end - timedelta(days=days_back)
    log.info(f"window {begin} → {end} | codes: {','.join(codes)} | retention {retention_days}d")

    store = load_store()
    log.info(f"cumulative store: {len(store):,} existing docs")

    session = make_session()
    # Backfill window: the recorder's party-name index lags recording by a day
    # or two, so a doc pulled the day it was recorded often comes back with no
    # names. Re-fetch details for docs already in the store that are still
    # nameless and were recorded within this many days — until they resolve or
    # age out. Without this the scraper only pulls forward and those docs stay
    # permanently nameless (and therefore unresolvable).
    recheck_after = (end - timedelta(days=RECHECK_DAYS)).isoformat()

    new_count = recheck_count = 0
    for code in codes:
        meta = DOC_TYPES.get(code, {"label": code})
        results = search_code(session, code, begin, end)
        to_detail = []      # (raw, is_new)
        for r in results:
            rn = str(r.get("recordingNumber", ""))
            if not rn:
                continue
            existing = store.get(rn)
            if existing is None:
                to_detail.append((r, True))
            elif not existing.get("names") and (existing.get("recorded_date") or "") >= recheck_after:
                to_detail.append((r, False))   # recent + still nameless -> retry
        n_new = sum(1 for _, isnew in to_detail if isnew)
        n_re = len(to_detail) - n_new
        log.info(f"▶ {code} ({meta['label']}): {len(results):,} in window, "
                 f"{n_new:,} new, {n_re:,} nameless re-checks")
        for i, (raw, isnew) in enumerate(to_detail, 1):
            rn = str(raw.get("recordingNumber", ""))
            names, restricted = ([], False)
            if fetch_details:
                names, restricted = fetch_names(session, rn)
            store[rn] = build_record(raw, code, names, restricted)
            if isnew:
                new_count += 1
            else:
                recheck_count += 1
                if names:
                    pass  # resolved on retry
            if i % 100 == 0:
                log.info(f"    {code}: {i}/{len(to_detail)} detailed")

    # Prune to retention window
    cutoff = (end - timedelta(days=retention_days)).isoformat()
    kept = {rn: r for rn, r in store.items()
            if (r.get("recorded_date") or "9999") >= cutoff}
    pruned = len(store) - len(kept)

    still_nameless = sum(1 for r in kept.values() if not r.get("names")
                         and (r.get("recorded_date") or "") >= recheck_after)
    with STORE_PATH.open("w") as f:
        for rec in sorted(kept.values(), key=lambda r: r.get("recorded_date") or "", reverse=True):
            f.write(json.dumps(rec) + "\n")
    log.info(f"✓ store: {len(kept):,} docs ({new_count:,} new, {recheck_count:,} re-checked, "
             f"{pruned:,} pruned) → {STORE_PATH}")
    if still_nameless:
        log.info(f"  {still_nameless:,} recent docs still nameless "
                 f"(recorder index lag; will retry within {RECHECK_DAYS}d)")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=3,
                    help="Days back to scan this run (default 3; use 30+ for backfill)")
    ap.add_argument("--retention", type=int, default=180,
                    help="Prune stored docs older than this many days (default 180)")
    ap.add_argument("--codes", type=str, default=",".join(DEFAULT_CODES))
    ap.add_argument("--no-details", action="store_true",
                    help="Skip per-doc name lookup (names needed for APN matching)")
    args = ap.parse_args(argv)

    codes = [c.strip().upper() for c in args.codes.split(",") if c.strip()]
    unknown = [c for c in codes if c not in DOC_TYPES]
    if unknown:
        log.warning(f"codes not in DOC_TYPES registry (will query anyway): {unknown}")
    run(codes=codes, days_back=args.days, retention_days=args.retention,
        fetch_details=not args.no_details)


if __name__ == "__main__":
    main()
