#!/usr/bin/env python3
"""
Maricopa County Recorder — public API scraper.

Target endpoint:
    GET https://publicapi.recorder.maricopa.gov/documents/search

Discovered via DevTools Network tab on recorder.maricopa.gov. The county exposes
a public JSON API for their document search. No authentication, no CAPTCHA, no
Playwright required.

Query parameters (GET):
    documentCode    Doc code filter (e.g. "NS" for Notice of Trustee Sale)
    beginDate       ISO date YYYY-MM-DD
    endDate         ISO date YYYY-MM-DD
    pageNumber      1-indexed
    pageSize        Up to 500
    maxResults      Soft cap (500)
    businessNames, firstNames, lastNames, middleNameIs   Left blank

Response shape:
    {
      "searchResults": [
        {"recordingNumber": 20260189366, "recordingDate": "4-01-2026",
         "recordingSuffix": "", "names": "", ...}
      ],
      "totalResults": 501
    }

Note that `names` is empty in the search response — the full name list is
fetched from a per-document detail endpoint (see fetch_detail below).

If a date range returns >=500 results we split it in half and recurse. This
keeps every window under the API's hard cap.

Doc codes we care about (AZ is a trustee-sale foreclosure state):
    NS      Notice of Trustee Sale       → preforeclosure
    LP      Lis Pendens                  → active lawsuit
    AFDT    Affidavit of Death           → probate / heirs
    FTL     Federal Tax Lien             → IRS lien
    STL     State Tax Lien               → AZ lien
    JGM     Judgment

Usage:
    python scrapers/maricopa_recorder_api.py --days 30
    python scrapers/maricopa_recorder_api.py --days 90 --codes NS,LP
    python scrapers/maricopa_recorder_api.py --days 30 --no-details   # fast, skip name lookup

Output:
    data/maricopa_recorder_raw.jsonl
"""
from __future__ import annotations
import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE = "https://publicapi.recorder.maricopa.gov"
SEARCH_URL = f"{BASE}/documents/search"
# Detail endpoint discovered from the modal flow on recorder.maricopa.gov.
# Two known patterns, we try them in order:
DETAIL_URL_TEMPLATES = [
    f"{BASE}/documents/{{rn}}",                # preferred
    f"{BASE}/document/{{rn}}",
    f"{BASE}/documents/detail/{{rn}}",
]

HEADERS = {
    "User-Agent": "MaricopaIntel/1.0 (public-records research)",
    "Accept": "application/json",
    "Origin": "https://recorder.maricopa.gov",
    "Referer": "https://recorder.maricopa.gov/",
}

# Signal_type strings must match keys in pipeline/build_leads.py SIGNAL_WEIGHTS.
DOC_CODE_MAP = {
    "NS":   {"signal_type": "Notice of Trustee Sale", "label": "Pre-foreclosure"},
    "LP":   {"signal_type": "Lis Pendens",            "label": "Lawsuit filed"},
    "AFDT": {"signal_type": "Affidavit of Death",     "label": "Owner died"},
    "AFFT": {"signal_type": "Affidavit of Death",     "label": "Owner died"},   # alt code
    "FTL":  {"signal_type": "Federal Tax Lien",       "label": "IRS lien"},
    "STL":  {"signal_type": "State Tax Lien",         "label": "AZ tax lien"},
    "JGM":  {"signal_type": "Judgment Lien",          "label": "Judgment"},
    "JUDG": {"signal_type": "Judgment Lien",          "label": "Judgment"},     # alt code
}

DEFAULT_CODES = ["NS", "LP", "AFDT", "FTL", "STL", "JGM"]

PAGE_SIZE = 200       # API hard-caps at 500 per page
RESULT_CAP = 200      # API caps totalResults-returned-per-query at this
REQUEST_DELAY = 0.6   # polite throttle between requests (sec)
MAX_RETRIES = 4       # exponential backoff on transient errors

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEBUG_DIR = DATA_DIR / "_debug"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DEBUG_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_PATH = DATA_DIR / "maricopa_recorder_raw.jsonl"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("maricopa_recorder")


# ---------------------------------------------------------------------------
# HTTP primitives
# ---------------------------------------------------------------------------
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def get_json(session: requests.Session, url: str, params: dict | None = None) -> dict | None:
    """GET with retry + exponential backoff. Returns parsed JSON or None on failure."""
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
                log.error(f"non-JSON 200 response from {url}")
                return None

        if r.status_code in (429, 502, 503, 504):
            retry_after = int(r.headers.get("Retry-After", "0")) or (2 ** attempt) * 2
            log.warning(f"HTTP {r.status_code} on {url}; sleeping {retry_after}s")
            time.sleep(retry_after)
            continue

        if r.status_code == 404:
            return None

        log.error(f"HTTP {r.status_code} on {url} — body: {r.text[:300]}")
        return None

    log.error(f"gave up on {url} after {MAX_RETRIES} retries")
    return None


# ---------------------------------------------------------------------------
# Search + pagination
# ---------------------------------------------------------------------------
def search_page(session: requests.Session, code: str, begin: date, end: date,
                page: int = 1) -> tuple[list[dict], int]:
    """Single page of search results. Returns (results, total_reported_by_api)."""
    params = {
        "businessNames": "",
        "firstNames":    "",
        "lastNames":     "",
        "middleNameIs":  "",
        "documentCode":  code,
        "beginDate":     begin.isoformat(),
        "endDate":       end.isoformat(),
        "pageSize":      PAGE_SIZE,
        "pageNumber":    page,
        "maxResults":    RESULT_CAP,
    }
    data = get_json(session, SEARCH_URL, params=params)
    time.sleep(REQUEST_DELAY)
    if not data:
        return [], 0
    results = data.get("searchResults") or []
    total = int(data.get("totalResults") or 0)
    return results, total


def search_range(session: requests.Session, code: str, begin: date, end: date) -> list[dict]:
    """
    Exhaustively fetch every record for a doc code + date range.

    API caps results at 500 per query, so if we hit the cap we split the date
    range in half and recurse. Worst-case a very busy day may need further
    splitting, but that's vanishingly rare for distress codes.
    """
    if begin > end:
        return []

    results, total = search_page(session, code, begin, end, page=1)
    log.info(f"  {code} {begin}→{end}: page1 {len(results)} / total {total}")

    # If API says there are >RESULT_CAP total, we must split the date range.
    # (Pagination beyond maxResults=500 silently returns empty pages.)
    if total > RESULT_CAP and (end - begin).days >= 1:
        mid = begin + (end - begin) / 2
        mid_date = date(mid.year, mid.month, mid.day) if isinstance(mid, datetime) else mid
        left  = search_range(session, code, begin, mid_date)
        right = search_range(session, code, mid_date + timedelta(days=1), end)
        return left + right

    # Otherwise paginate through what we have
    all_results = list(results)
    fetched = len(results)
    page = 2
    while fetched < total and fetched < RESULT_CAP:
        page_results, _ = search_page(session, code, begin, end, page=page)
        if not page_results:
            break
        all_results.extend(page_results)
        fetched += len(page_results)
        page += 1
        if page > 20:   # safety ceiling
            log.warning(f"  pagination ceiling hit on {code} {begin}→{end}")
            break

    return all_results


# ---------------------------------------------------------------------------
# Detail fetch (names come from the detail endpoint, not search)
# ---------------------------------------------------------------------------
_DETAIL_URL_CACHE: str | None = None


def fetch_detail(session: requests.Session, recording_number: int | str) -> dict | None:
    """
    Fetch the names + document codes for a single recording.

    The detail URL isn't in our DevTools capture, so we probe the common
    patterns once and remember which one worked. If none work, we fall back
    to parsing the public modal HTML at recorder.maricopa.gov (slower).
    """
    global _DETAIL_URL_CACHE
    rn = str(recording_number)

    if _DETAIL_URL_CACHE:
        data = get_json(session, _DETAIL_URL_CACHE.format(rn=rn))
        time.sleep(REQUEST_DELAY)
        return data

    for template in DETAIL_URL_TEMPLATES:
        url = template.format(rn=rn)
        data = get_json(session, url)
        time.sleep(REQUEST_DELAY)
        if data:
            log.info(f"detail endpoint resolved: {template}")
            _DETAIL_URL_CACHE = template
            return data

    log.warning(
        f"could not resolve detail endpoint after probing: {DETAIL_URL_TEMPLATES}. "
        "Skipping detail fetch — records will be emitted without name lists."
    )
    _DETAIL_URL_CACHE = ""   # "probed, nothing found" sentinel
    return None


def extract_names(detail: dict) -> list[str]:
    """Pull the party-name list out of a detail payload.
    Maricopa returns: {"names": ["NAME 1", "NAME 2", ...]}
    """
    if not detail or not isinstance(detail, dict):
        return []

    # Preferred key: "names" (confirmed for Maricopa 2026)
    names = detail.get("names")

    # Defensive fallbacks in case payload shape changes
    if not names:
        for key in ("partyNames", "parties", "partyList"):
            names = detail.get(key)
            if names:
                break

    if not names:
        return []

    # Normalize: accept list[str], list[dict], or comma-separated str
    out = []
    if isinstance(names, list):
        for item in names:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict):
                n = item.get("name") or item.get("fullName") or item.get("partyName")
                if n and isinstance(n, str):
                    out.append(n.strip())
    elif isinstance(names, str):
        out = [s.strip() for s in names.split(",") if s.strip()]

    return out


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------
def parse_recording_date(s: str | None) -> str | None:
    """Recorder returns dates like '4-01-2026'. Normalize to ISO YYYY-MM-DD."""
    if not s:
        return None
    for fmt in ("%m-%d-%Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return s.strip()


def build_record(raw: dict, code: str, names: list[str]) -> dict:
    meta = DOC_CODE_MAP.get(code, {"signal_type": code, "label": code})
    return {
        "county":           "Maricopa",
        "source":           "maricopa_recorder_api",
        "doc_code":         code,
        "signal_type":      meta["signal_type"],
        "label":            meta["label"],
        "recording_number": str(raw.get("recordingNumber", "")),
        "recording_suffix": raw.get("recordingSuffix") or "",
        "recorded_date":    parse_recording_date(raw.get("recordingDate")),
        "names":            names,       # filled in by match_recorder.py
        "fetched_at":       datetime.utcnow().isoformat() + "Z",
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run(codes: list[str], days_back: int, fetch_names: bool) -> None:
    end = date.today()
    begin = end - timedelta(days=days_back)
    log.info(f"date window: {begin} → {end} ({days_back} days)")
    log.info(f"doc codes: {codes}")

    session = make_session()
    out_f = OUTPUT_PATH.open("w")
    total_written = 0

    try:
        for code in codes:
            meta = DOC_CODE_MAP.get(code, {"signal_type": code, "label": code})
            log.info(f"▶ {code} ({meta['signal_type']})")
            results = search_range(session, code, begin, end)
            log.info(f"  {code}: {len(results):,} records found")

            for i, raw in enumerate(results, 1):
                names: list[str] = []
                if fetch_names and raw.get("recordingNumber"):
                    detail = fetch_detail(session, raw["recordingNumber"])
                    names = extract_names(detail)

                rec = build_record(raw, code, names)
                out_f.write(json.dumps(rec) + "\n")
                total_written += 1

                if i % 100 == 0:
                    log.info(f"    {code}: {i}/{len(results)} processed")
    finally:
        out_f.close()

    log.info(f"✓ wrote {total_written:,} records → {OUTPUT_PATH}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=30,
                    help="Days back from today to scan (default: 30)")
    ap.add_argument("--codes", type=str, default=",".join(DEFAULT_CODES),
                    help="Comma-separated doc codes (default: all distress codes)")
    ap.add_argument("--no-details", action="store_true",
                    help="Skip per-doc name lookup (faster but records have no names)")
    args = ap.parse_args(argv)

    codes = [c.strip().upper() for c in args.codes.split(",") if c.strip()]
    unknown = [c for c in codes if c not in DOC_CODE_MAP]
    if unknown:
        log.warning(f"unknown doc codes (will still be queried): {unknown}")

    run(codes=codes, days_back=args.days, fetch_names=not args.no_details)


if __name__ == "__main__":
    main()
