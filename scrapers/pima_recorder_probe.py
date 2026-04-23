#!/usr/bin/env python3
"""
pima_recorder_probe.py
──────────────────────
Hits the Pima Tyler Technologies recorder search with a real browser session
cookie and prints the raw response so we can learn the HTML structure.

Usage:
  python scrapers/pima_recorder_probe.py

You'll need to paste your current JSESSIONID value below. Get it from DevTools:
  Application tab → Cookies → https://pimacountyaz-web.tylerhost.net
  Copy the JSESSIONID value (long hex string)

If the session expires (shows tiny/empty response), refresh the search in
your browser and grab a fresh cookie value.
"""
import requests
from pathlib import Path

# ─── PASTE FRESH COOKIE HERE ─────────────────────────────────────────────────
JSESSIONID = "99D094DE408CEA5BEE82B0AD1B032FCB"

BASE = "https://pimacountyaz-web.tylerhost.net"
SEARCH_ID = "DOCSEARCH55S8"

COOKIES = {
    "JSESSIONID": JSESSIONID,
    "disclaimerAccepted": "true",
}

HEADERS_COMMON = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "ajaxrequest": "true",
    "referer": f"{BASE}/web/search/{SEARCH_ID}",
    "user-agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/147.0.0.0 Safari/537.36"),
    "x-requested-with": "XMLHttpRequest",
}


def setup_search(session, start_date, end_date, doc_codes):
    """
    POST the search criteria to the server.
    doc_codes = list of (code, full_name) tuples
    """
    url = f"{BASE}/web/searchPost/{SEARCH_ID}"

    # Build the payload using ordered list of tuples so multi-value keys
    # (repeated keys) encode correctly.
    data = [
        ("field_RecordingDateID_DOT_StartDate", start_date),
        ("field_RecordingDateID_DOT_EndDate",   end_date),
    ]
    for code, name in doc_codes:
        data.append(("field_selfservice_documentTypes-holderInput", code))
        data.append(("field_selfservice_documentTypes-holderValue", name))
    data.append(("field_selfservice_documentTypes-containsInput", "Contains Any"))
    data.append(("field_selfservice_documentTypes", ""))

    headers = {
        **HEADERS_COMMON,
        "accept": "application/json, text/javascript, */*; q=0.01",
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "origin": BASE,
    }

    r = session.post(url, data=data, headers=headers, cookies=COOKIES, timeout=30)
    print(f"[setup] POST {url}")
    print(f"[setup] status={r.status_code}  size={len(r.text)}  content-type={r.headers.get('content-type','')}")
    print(f"[setup] body (first 500 chars):")
    print(r.text[:500])
    print("-" * 70)
    return r


def fetch_page(session, page_num):
    """GET a specific results page."""
    url = f"{BASE}/web/searchResults/{SEARCH_ID}"
    params = {"page": page_num}
    r = session.get(url, params=params, headers=HEADERS_COMMON, cookies=COOKIES, timeout=30)
    print(f"[page {page_num}] GET {r.url}")
    print(f"[page {page_num}] status={r.status_code}  size={len(r.text)}  content-type={r.headers.get('content-type','')}")
    return r


def main():
    print("=" * 70)
    print("PIMA RECORDER PROBE — Apr 1-21, 2026, NOTICE SALE only (test)")
    print("=" * 70)

    session = requests.Session()

    # Set up search for just one doc type to keep the probe simple
    doc_codes_to_test = [
        ("NOTS", "NOTICE SALE"),  # pre-foreclosure
    ]

    setup_r = setup_search(session, "4/1/2026", "4/21/2026", doc_codes_to_test)

    if setup_r.status_code != 200:
        print(f"✗ Setup failed with status {setup_r.status_code}")
        return

    # Pull page 1
    page1 = fetch_page(session, 1)

    if page1.status_code != 200 or len(page1.text) < 1000:
        print("✗ Page 1 response too small — session probably expired.")
        print("  Open a fresh search in browser, grab new JSESSIONID, paste at top of script.")
        return

    # Save full response for Claude to parse
    outpath = Path("/tmp/pima_page1_response.html")
    outpath.write_text(page1.text, encoding="utf-8")
    print(f"\n✓ Saved full response ({len(page1.text):,} bytes) to {outpath}")

    # Show the first ~2000 chars so we can eyeball the structure
    print("\n" + "=" * 70)
    print("FIRST 2000 CHARS OF RESPONSE:")
    print("=" * 70)
    print(page1.text[:2000])
    print()
    print("=" * 70)
    print("SEARCHING FOR KEY MARKERS:")
    print("=" * 70)
    # Look for patterns we care about
    import re
    markers = [
        r'class="ss-search-item"',
        r'class="ss-result',
        r'Recording Number',
        r'Recording Date',
        r'Grantor',
        r'Grantee',
        r'DOCSEARCH',
        r'data-instrumentNumber',
        r'id="sr-\d+"',
        r'<li.*?class=',
    ]
    for m in markers:
        matches = re.findall(m, page1.text)
        print(f"  {m!r} → {len(matches)} matches")

    # Show the first record's HTML chunk
    print("\n" + "=" * 70)
    print("LOOKING FOR FIRST RECORD BLOCK:")
    print("=" * 70)
    # Try a few likely patterns
    patterns = [
        (r'(<div[^>]*class="[^"]*ss-search-item[^"]*"[^>]*>.*?</div>\s*</div>)', "ss-search-item div"),
        (r'(<li[^>]*id="sr-\d+"[^>]*>.*?</li>)', "li#sr-N"),
        (r'(<li[^>]*data-instrumentnumber[^>]*>.*?</li>)', "li[data-instrumentnumber]"),
    ]
    for pat, label in patterns:
        m = re.search(pat, page1.text, re.DOTALL | re.IGNORECASE)
        if m:
            print(f"✓ Found pattern '{label}':")
            print(m.group(1)[:1500])
            print("-" * 70)
            break
    else:
        print("✗ None of the expected patterns matched. Manual inspection needed.")
        print("  Full response saved to /tmp/pima_page1_response.html — open it in a browser.")


if __name__ == "__main__":
    main()
