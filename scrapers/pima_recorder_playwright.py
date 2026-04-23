#!/usr/bin/env python3
"""
pima_recorder_playwright.py
───────────────────────────
Scrape Pima County Recorder (Tyler Technologies portal) behind AWS WAF.

Uses real Chromium via Playwright so the WAF human-verification JS challenge
resolves automatically. First run will prompt you to accept the disclaimer
checkbox manually — after that, the session cookie is reused.

Output:
  • data/pima_recorder_raw.jsonl — one record per line, same schema as Maricopa

Distress doc codes targeted (Tyler internal codes):
  NOTS    Notice Sale (pre-foreclosure)        +55
  LIS     Lis Pendens (lawsuit on title)       +35
  CTDTH   Certificate Death (probate)          +50
  AFFSUC  Affidavit Succession (probate)       +50
  AFFTJT  Affidavit Term JT/CP (spousal death) +45
  JGM     Judgment                             +25
  FEDL    Federal Lien                         +30
  STL     State Lien                           +25
  AHCCCS  AHCCCS Lien (Medicaid)               +20
  HOSPL   Hospital Lien                        +18
  MECHL   Mechanics Lien                       +15
  CITYL   City Lien                            +20
  SHRFD   Sheriffs Deed                        +40
  BKDSCH  Bankruptcy Discharge                 +30
  DIVORCE Dissolution Marriage                 +25

USAGE:
  1. First run, you'll be prompted to solve the disclaimer page manually
     (check the reCAPTCHA + click "I Accept"). The script then proceeds.
  2. Subsequent runs reuse the saved browser profile so no manual step needed.
  3. Optional flags:
       --days N        window size (default 30)
       --headless      run without visible browser (after first setup)
       --codes CODE1,CODE2  restrict to specific codes for testing
"""
import argparse
import json
import logging
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
except ImportError:
    print("ERROR: playwright not installed. Run:")
    print("  pip install playwright --break-system-packages")
    print("  playwright install chromium")
    sys.exit(1)


logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("pima_recorder")


BASE_URL = "https://pimacountyaz-web.tylerhost.net"
SEARCH_ID = "DOCSEARCH55S8"
SEARCH_URL = f"{BASE_URL}/web/search/{SEARCH_ID}"
SEARCH_POST = f"{BASE_URL}/web/searchPost/{SEARCH_ID}"
RESULTS_URL = f"{BASE_URL}/web/searchResults/{SEARCH_ID}"

# Profile dir persists session between runs — WAF cookie, disclaimer accepted
ROOT = Path(__file__).resolve().parent.parent
PROFILE_DIR = ROOT / ".playwright-pima"
OUT_FILE = ROOT / "data" / "pima_recorder_raw.jsonl"


# Tyler internal codes → our signal taxonomy (mirrors Maricopa schema)
DOC_CODES = {
    "NOTS":    {"signal_type": "Notice of Trustee Sale", "label": "Pre-foreclosure"},
    "LIS":     {"signal_type": "Lis Pendens",            "label": "Lawsuit on title"},
    "CTDTH":   {"signal_type": "Affidavit of Death",     "label": "Owner died"},
    "AFFSUC":  {"signal_type": "Affidavit of Death",     "label": "Succession filing"},
    "AFFTJT":  {"signal_type": "Affidavit of Death",     "label": "Spousal death"},
    "JGM":     {"signal_type": "Judgment Lien",          "label": "Judgment"},
    "FEDL":    {"signal_type": "Federal Tax Lien",       "label": "IRS lien"},
    "STL":     {"signal_type": "State Tax Lien",         "label": "AZ tax lien"},
    "AHCCCS":  {"signal_type": "Medicaid Lien",          "label": "AHCCCS"},
    "HOSPL":   {"signal_type": "Hospital Lien",          "label": "Hospital lien"},
    "MECHL":   {"signal_type": "Mechanics Lien",         "label": "Contractor lien"},
    "CITYL":   {"signal_type": "City Lien",              "label": "City lien"},
    "SHRFD":   {"signal_type": "Sheriffs Deed",          "label": "Post-foreclosure"},
    "BKDSCH":  {"signal_type": "Bankruptcy Discharge",   "label": "Bankruptcy"},
    "DIVORCE": {"signal_type": "Divorce",                "label": "Dissolution"},
}

# Full names for the holder-value field (from the dropdown JSON)
DOC_FULL_NAMES = {
    "NOTS":    "NOTICE SALE",
    "LIS":     "LIS PENDENS",
    "CTDTH":   "CERTIFICATE DEATH",
    "AFFSUC":  "AFFIDAVIT SUCCESSION",
    "AFFTJT":  "AFFIDAVIT TERM JT/CP",
    "JGM":     "JUDGMENT",
    "FEDL":    "FEDERAL LIEN",
    "STL":     "STATE LIEN",
    "AHCCCS":  "AHCCCS LIEN",
    "HOSPL":   "HOSPITAL LIEN",
    "MECHL":   "MECHANICS LIEN",
    "CITYL":   "CITY LIEN",
    "SHRFD":   "SHERIFFS DEED",
    "BKDSCH":  "BANKRUPTCY DISCHARGE",
    "DIVORCE": "DISSOLUTION MARRIAGE",
}


def parse_records(html):
    """
    Extract (recording_number, recording_date, doc_type, grantor_list, grantee_list)
    tuples from a search-results HTML page.

    Tyler portals render each record inside an <li> or <div> block. We try a few
    known selectors and fall back to regex if structure differs.
    """
    records = []

    # Try BeautifulSoup parse first
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        log.warning("bs4 not installed; falling back to regex. pip install beautifulsoup4")
        return regex_parse(html)

    soup = BeautifulSoup(html, "html.parser")

    # Pattern 1: <div class="ss-search-item"> ... </div>
    items = soup.select("div.ss-search-item, li.ss-search-item, .search-result-item")
    if not items:
        # Pattern 2: anchor wrappers
        items = soup.select("[id^='sr-'], [data-instrumentnumber]")

    if not items:
        log.warning("No result items found in HTML — structure may have changed")
        log.debug(html[:500])
        return []

    for item in items:
        rec = {}

        # Recording number — usually a link or prominent number
        # Format: 20261040313
        text = item.get_text(" ", strip=True)
        m = re.search(r'\b(20\d{9})\b', text)
        if m:
            rec["recording_number"] = m.group(1)

        # Recording date
        m = re.search(r'(\d{1,2}/\d{1,2}/\d{4})', text)
        if m:
            rec["recording_date"] = m.group(1)

        # Doc type — all caps phrase between recording# and "Recording Date" label
        # Look for known doc names
        for name in DOC_FULL_NAMES.values():
            if name in text:
                rec["doc_name"] = name
                break

        # Grantor/Grantee — look for labeled segments
        for label in ("Grantor", "Grantee"):
            m = re.search(rf'{label}\s+([A-Z][A-Z0-9 &\'\.\-,/]+?)(?=\s+(?:Grantor|Grantee|Recording|$))', text)
            if m:
                rec[label.lower()] = m.group(1).strip()

        if rec.get("recording_number"):
            records.append(rec)

    return records


def regex_parse(html):
    """Fallback regex parser when bs4 isn't available."""
    records = []
    # Split on recording numbers as anchors
    chunks = re.split(r'(?=\b20\d{9}\b)', html)
    for chunk in chunks:
        m = re.match(r'.*?\b(20\d{9})\b', chunk, re.DOTALL)
        if not m:
            continue
        rec = {"recording_number": m.group(1)}
        text = re.sub(r'<[^>]+>', ' ', chunk)
        text = re.sub(r'\s+', ' ', text)

        m = re.search(r'(\d{1,2}/\d{1,2}/\d{4})', text)
        if m:
            rec["recording_date"] = m.group(1)

        for label in ("Grantor", "Grantee"):
            m = re.search(rf'{label}\s+([A-Z][A-Z0-9 &\'\.\-,/]+?)(?=\s+(?:Grantor|Grantee|Recording|$))', text)
            if m:
                rec[label.lower()] = m.group(1).strip()

        records.append(rec)
    return records


def run_scrape(days_back, codes_filter=None, headless=False):
    PROFILE_DIR.mkdir(exist_ok=True)
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    end_dt = date.today()
    start_dt = end_dt - timedelta(days=days_back)
    start_str = start_dt.strftime("%-m/%-d/%Y")
    end_str   = end_dt.strftime("%-m/%-d/%Y")

    codes_to_scrape = codes_filter or list(DOC_CODES.keys())
    log.info(f"Window: {start_str} → {end_str} ({days_back} days)")
    log.info(f"Doc codes: {codes_to_scrape}")

    all_records = []
    seen = set()

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=headless,
            viewport={"width": 1280, "height": 900},
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/147.0.0.0 Safari/537.36"),
        )
        page = context.new_page()

        # Navigate to search — handles disclaimer/WAF if needed
        log.info("Navigating to search page…")
        page.goto(SEARCH_URL, wait_until="networkidle", timeout=60000)

        # Wait for the search form to appear. If stuck on disclaimer, the URL
        # will contain "/disclaimer" or body will have "I Accept"
        if "disclaimer" in page.url or "I Accept" in page.content():
            log.warning("=" * 60)
            log.warning("DISCLAIMER / CAPTCHA DETECTED")
            log.warning("Please solve it in the browser window that just opened,")
            log.warning("then press ENTER here to continue…")
            log.warning("=" * 60)
            input("> Press ENTER after accepting disclaimer: ")
            page.wait_for_url(f"**/search/{SEARCH_ID}*", timeout=60000)

        log.info("On search page, ready to query")

        for code in codes_to_scrape:
            if code not in DOC_CODES:
                log.warning(f"unknown code: {code}")
                continue
            full_name = DOC_FULL_NAMES[code]
            sig_info  = DOC_CODES[code]

            log.info(f"▶ {code} ({sig_info['signal_type']})")

            # Submit search via fetch() inside the page context — inherits cookies + WAF token
            form_body_parts = [
                ("field_RecordingDateID_DOT_StartDate", start_str),
                ("field_RecordingDateID_DOT_EndDate",   end_str),
                ("field_selfservice_documentTypes-holderInput", code),
                ("field_selfservice_documentTypes-holderValue", full_name),
                ("field_selfservice_documentTypes-containsInput", "Contains Any"),
                ("field_selfservice_documentTypes", ""),
            ]
            body = urlencode(form_body_parts)

            setup_resp = page.evaluate(
                """async ([url, body]) => {
                    const r = await fetch(url, {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                            'ajaxrequest': 'true',
                            'x-requested-with': 'XMLHttpRequest',
                        },
                        body: body,
                        credentials: 'include',
                    });
                    return { status: r.status, text: await r.text() };
                }""",
                [SEARCH_POST, body],
            )
            if setup_resp["status"] != 200:
                log.error(f"  setup failed status={setup_resp['status']}: {setup_resp['text'][:200]}")
                continue

            # Now paginate results
            code_count = 0
            for page_num in range(1, 30):  # safety cap
                res = page.evaluate(
                    """async (url) => {
                        const r = await fetch(url, {
                            headers: {
                                'accept': '*/*',
                                'ajaxrequest': 'true',
                                'x-requested-with': 'XMLHttpRequest',
                            },
                            credentials: 'include',
                        });
                        return { status: r.status, text: await r.text() };
                    }""",
                    f"{RESULTS_URL}?page={page_num}",
                )
                if res["status"] != 200:
                    log.warning(f"  page {page_num}: HTTP {res['status']}")
                    break
                html = res["text"]
                if len(html) < 1000:
                    log.info(f"  page {page_num}: empty, stopping")
                    break

                recs = parse_records(html)
                if not recs:
                    log.info(f"  page {page_num}: 0 records parsed, stopping")
                    break

                new_this_page = 0
                for r in recs:
                    rn = r.get("recording_number")
                    if not rn or rn in seen:
                        continue
                    seen.add(rn)
                    new_this_page += 1

                    # Build output record in Maricopa-compatible schema
                    rec_out = {
                        "county":          "Pima",
                        "source":          "pima_recorder_tyler",
                        "doc_code":        code,
                        "signal_type":     sig_info["signal_type"],
                        "label":           sig_info["label"],
                        "recording_number": rn,
                        "recorded_date":   r.get("recording_date", ""),
                        "names":           [n for n in (r.get("grantor"), r.get("grantee")) if n],
                        "raw_grantor":     r.get("grantor", ""),
                        "raw_grantee":     r.get("grantee", ""),
                    }
                    all_records.append(rec_out)

                code_count += new_this_page
                log.info(f"  page {page_num}: {len(recs)} records, {new_this_page} new")

                if len(recs) < 25:   # last page usually < full page
                    break
                time.sleep(0.5)

            log.info(f"  ✓ {code}: {code_count} records")
            time.sleep(1)

        context.close()

    # Write output
    with OUT_FILE.open("w", encoding="utf-8") as f:
        for r in all_records:
            f.write(json.dumps(r) + "\n")

    log.info("=" * 60)
    log.info(f"✓ wrote {len(all_records):,} records → {OUT_FILE}")
    breakdown = {}
    for r in all_records:
        k = r["signal_type"]
        breakdown[k] = breakdown.get(k, 0) + 1
    for sig, n in sorted(breakdown.items(), key=lambda x: -x[1]):
        log.info(f"   {sig}: {n:,}")
    log.info("=" * 60)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30, help="Days back from today")
    ap.add_argument("--headless", action="store_true", help="Run without visible browser")
    ap.add_argument("--codes", type=str, default=None, help="Comma-sep codes to restrict to")
    args = ap.parse_args()

    codes_filter = None
    if args.codes:
        codes_filter = [c.strip().upper() for c in args.codes.split(",") if c.strip()]

    run_scrape(days_back=args.days, codes_filter=codes_filter, headless=args.headless)


if __name__ == "__main__":
    main()
