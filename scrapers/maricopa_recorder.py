"""
Maricopa County Recorder signal scraper (Playwright-based).

Portal:     https://recorder.maricopa.gov/recording/document-search.html
Coverage:   online search exposes only the last ~2 years of data.

Pulls recorded documents matching motivated-seller doc codes, then emits
a normalized JSONL file. Downstream pipeline steps resolve each doc to a
property via APN (preferred) or owner-name lookup against the parcel master.

IMPORTANT: Recorder portal selectors occasionally drift when the county
updates the UI. On failure this scraper writes a screenshot to
data/_debug/<doc_code>-<timestamp>.png so the selectors can be repaired
without re-running the whole job.

Requirements:
    pip install playwright
    python -m playwright install chromium

Usage:
    python maricopa_recorder.py --days 30
    python maricopa_recorder.py --doc-codes NTS,LP,AFDT --days 90

Output:
    data/maricopa_recorder_raw.jsonl
"""

import argparse
import asyncio
import json
import logging
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

try:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
except ImportError:
    print("ERROR: pip install playwright && python -m playwright install chromium", file=sys.stderr)
    sys.exit(1)

PORTAL_URL = "https://recorder.maricopa.gov/recording/document-search.html"

BASE_DIR = Path(__file__).resolve().parent.parent
OUT_PATH = BASE_DIR / "data" / "maricopa_recorder_raw.jsonl"
DEBUG_DIR = BASE_DIR / "data" / "_debug"

# Motivated-seller doc codes.
DOC_CODES = {
    "NTS":  "Notice of Trustee Sale",
    "LP":   "Lis Pendens",
    "AFDT": "Affidavit of Death",
    "AFFD": "Affidavit",
    "FTL":  "Federal Tax Lien",
    "STL":  "State Tax Lien",
    "MEL":  "Mechanics Lien",
    "HOA":  "HOA Lien",
}

# Maricopa APN pattern: 3 digits - 2 digits - 3 digits optional letter.
APN_RE = re.compile(r"\b\d{3}-\d{2}-\d{3}[A-Z]?\b")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("maricopa_recorder")


async def _save_debug(page, tag: str):
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = DEBUG_DIR / f"maricopa-{tag}-{ts}.png"
    try:
        await page.screenshot(path=str(path), full_page=True)
        log.warning("debug screenshot -> %s", path)
    except Exception as e:
        log.warning("could not save screenshot: %s", e)


async def _fill_form(page, doc_code: str, date_from: str, date_to: str) -> bool:
    """Populate the search form. Returns True if submission looks successful."""
    await page.goto(PORTAL_URL, wait_until="domcontentloaded")
    try:
        await page.wait_for_selector(
            "input[name='docCode'], #docCode, select[name='docCode']",
            timeout=20_000,
        )
    except PWTimeout:
        await _save_debug(page, f"noform-{doc_code}")
        return False

    await page.evaluate(
        """(code) => {
            const el = document.querySelector("input[name='docCode'], #docCode, select[name='docCode']");
            if (!el) return;
            el.value = code;
            el.dispatchEvent(new Event('input',  {bubbles:true}));
            el.dispatchEvent(new Event('change', {bubbles:true}));
        }""",
        doc_code,
    )

    for selector, value in [
        ("input[name='dateFrom'], #dateFrom", date_from),
        ("input[name='dateTo'],   #dateTo",   date_to),
    ]:
        await page.evaluate(
            """([s, v]) => {
                const el = document.querySelector(s);
                if (!el) return;
                el.value = v;
                el.dispatchEvent(new Event('input',  {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
            }""",
            [selector, value],
        )

    submit = await page.query_selector(
        "button[type='submit'], #searchBtn, input[type='submit']"
    )
    if not submit:
        await _save_debug(page, f"nosubmit-{doc_code}")
        return False
    await submit.click()
    try:
        await page.wait_for_selector("table, .results, #resultsTable", timeout=30_000)
    except PWTimeout:
        await _save_debug(page, f"noresults-{doc_code}")
        return False
    return True


async def _scrape_current_page(page) -> list:
    return await page.evaluate(
        """() => {
            const out = [];
            const tables = document.querySelectorAll('table');
            for (const t of tables) {
                const headerRow = t.querySelector('thead tr, tr');
                const headers = headerRow
                    ? Array.from(headerRow.querySelectorAll('th, td')).map(h => (h.innerText || '').trim())
                    : [];
                for (const row of t.querySelectorAll('tbody tr')) {
                    const cells = Array.from(row.querySelectorAll('td')).map(c => (c.innerText || '').trim());
                    if (cells.length < 2) continue;
                    const rec = {};
                    cells.forEach((v, i) => { rec[headers[i] || ('col' + i)] = v; });
                    rec.__raw_text = cells.join(' | ');
                    // Try to capture a document detail link if present.
                    const link = row.querySelector('a[href*="document"]');
                    if (link) rec.__doc_url = link.href;
                    out.push(rec);
                }
            }
            return out;
        }"""
    )


async def _next_page(page) -> bool:
    """Advances to the next result page. Returns False when no more pages."""
    btn = await page.query_selector("a.next, button.next, [aria-label='Next']")
    if not btn:
        return False
    disabled = await btn.get_attribute("disabled")
    aria_disabled = await btn.get_attribute("aria-disabled")
    if disabled or aria_disabled == "true":
        return False
    await btn.click()
    await page.wait_for_timeout(1500)
    return True


async def search_doc_code(page, doc_code: str, date_from: str, date_to: str,
                          max_pages: int = 100) -> list:
    """Runs a single doc-code search and returns all raw rows across pages."""
    log.info("search %s  %s..%s", doc_code, date_from, date_to)
    ok = await _fill_form(page, doc_code, date_from, date_to)
    if not ok:
        log.warning("  form/submit failed for %s — see debug screenshot", doc_code)
        return []

    rows = await _scrape_current_page(page)
    for i in range(1, max_pages):
        if not await _next_page(page):
            break
        rows.extend(await _scrape_current_page(page))
    log.info("  %s: %d rows", doc_code, len(rows))
    return rows


def extract_apns(text: str) -> list:
    return list(set(APN_RE.findall(text or "")))


def normalize_row(raw: dict, doc_code: str) -> dict:
    """Convert raw scraped cell dict into a unified signal record."""
    text = raw.get("__raw_text", "")
    return {
        "county":        "Maricopa",
        "signal_type":   DOC_CODES.get(doc_code, doc_code),
        "doc_code":      doc_code,
        "doc_number":    raw.get("Document Number") or raw.get("Doc Number") or raw.get("col0"),
        "record_date":   raw.get("Recording Date") or raw.get("Date") or raw.get("col1"),
        "grantor":       raw.get("Grantor") or raw.get("From"),
        "grantee":       raw.get("Grantee") or raw.get("To"),
        "legal_desc":    raw.get("Legal Description") or raw.get("Description"),
        "apns_found":    extract_apns(text),
        "doc_url":       raw.get("__doc_url"),
        "raw_text":      text,
        "scraped_at":    datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }


async def run(days: int, doc_codes: list):
    date_to = date.today()
    date_from = date_to - timedelta(days=days)
    df = date_from.strftime("%m/%d/%Y")
    dt = date_to.strftime("%m/%d/%Y")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (compatible; MaricopaPimaIntel/1.0)"
        )
        page = await context.new_page()

        all_records = []
        for code in doc_codes:
            try:
                rows = await search_doc_code(page, code, df, dt)
                for r in rows:
                    all_records.append(normalize_row(r, code))
            except Exception as e:
                log.error("failed doc_code=%s: %s", code, e)
                await _save_debug(page, f"crash-{code}")

        await browser.close()

    with OUT_PATH.open("w", encoding="utf-8") as f:
        for rec in all_records:
            f.write(json.dumps(rec, default=str) + "\n")

    log.info("wrote %d records -> %s", len(all_records), OUT_PATH)


def main():
    ap = argparse.ArgumentParser(description="Maricopa Recorder signal scraper")
    ap.add_argument("--days", type=int, default=30,
                    help="How many days back to pull (default 30)")
    ap.add_argument("--doc-codes", default=",".join(DOC_CODES.keys()),
                    help="Comma-separated doc codes (default: all)")
    args = ap.parse_args()
    codes = [c.strip().upper() for c in args.doc_codes.split(",") if c.strip()]
    asyncio.run(run(args.days, codes))


if __name__ == "__main__":
    main()
