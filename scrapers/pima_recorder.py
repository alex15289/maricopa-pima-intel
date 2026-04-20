"""
Pima County Recorder signal scraper (Playwright-based).

Portal: https://www.recorder.pima.gov/PublicSearch

Same target signals as Maricopa (NTS, Lis Pendens, Affidavit of Death,
tax liens, mechanics liens). Pima exposes records back to 1982 through
this portal, so the historical window is much larger than Maricopa's
two-year limit.

Requirements:
    pip install playwright
    python -m playwright install chromium

Usage:
    python pima_recorder.py --days 30
    python pima_recorder.py --doc-codes NTS,LP --days 365

Output:
    data/pima_recorder_raw.jsonl
"""

import argparse
import asyncio
import json
import logging
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
except ImportError:
    print("ERROR: pip install playwright && python -m playwright install chromium", file=sys.stderr)
    sys.exit(1)

PORTAL_URL = "https://www.recorder.pima.gov/PublicSearch"

BASE_DIR = Path(__file__).resolve().parent.parent
OUT_PATH = BASE_DIR / "data" / "pima_recorder_raw.jsonl"
DEBUG_DIR = BASE_DIR / "data" / "_debug"

# Pima doc code labels differ slightly; map to canonical signal labels.
DOC_CODES = {
    "NTS":  "Notice of Trustee Sale",
    "LP":   "Lis Pendens",
    "AFDT": "Affidavit of Death",
    "AFF":  "Affidavit",
    "FTL":  "Federal Tax Lien",
    "STL":  "State Tax Lien",
    "MEL":  "Mechanics Lien",
    "HOA":  "HOA Lien",
}

# Pima APN pattern: book-map-parcel (3-2-3 with optional letter).
APN_RE = re.compile(r"\b\d{3}-\d{2}-\d{3}[A-Z]?\b")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("pima_recorder")


async def _save_debug(page, tag: str):
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = DEBUG_DIR / f"pima-{tag}-{ts}.png"
    try:
        await page.screenshot(path=str(path), full_page=True)
        log.warning("debug screenshot -> %s", path)
    except Exception:
        pass


async def _search(page, doc_code: str, date_from: str, date_to: str) -> bool:
    await page.goto(PORTAL_URL, wait_until="domcontentloaded")
    # Pima portal uses a tabbed search; wait for the main search panel.
    try:
        await page.wait_for_selector(
            "input[name='DocumentType'], #DocumentType, select[name='DocumentType'], "
            "input[placeholder*='Doc'], input[name='docType']",
            timeout=20_000,
        )
    except PWTimeout:
        await _save_debug(page, f"noform-{doc_code}")
        return False

    # Try several field-name variants — Pima's vendor app has changed labels.
    await page.evaluate(
        """(code) => {
            const candidates = [
                "input[name='DocumentType']", "#DocumentType",
                "select[name='DocumentType']", "input[name='docType']",
                "input[placeholder*='Doc']"
            ];
            for (const sel of candidates) {
                const el = document.querySelector(sel);
                if (el) {
                    el.value = code;
                    el.dispatchEvent(new Event('input',  {bubbles:true}));
                    el.dispatchEvent(new Event('change', {bubbles:true}));
                    break;
                }
            }
        }""",
        doc_code,
    )

    for sel_list, value in [
        (["input[name='DateFrom']", "#DateFrom", "input[name='dateFrom']"], date_from),
        (["input[name='DateTo']",   "#DateTo",   "input[name='dateTo']"],   date_to),
    ]:
        await page.evaluate(
            """([sels, v]) => {
                for (const s of sels) {
                    const el = document.querySelector(s);
                    if (el) {
                        el.value = v;
                        el.dispatchEvent(new Event('input',  {bubbles:true}));
                        el.dispatchEvent(new Event('change', {bubbles:true}));
                        break;
                    }
                }
            }""",
            [sel_list, value],
        )

    btn = await page.query_selector("button[type='submit'], #searchBtn, input[type='submit']")
    if not btn:
        await _save_debug(page, f"nosubmit-{doc_code}")
        return False
    await btn.click()
    try:
        await page.wait_for_selector("table, .results, [role='grid']", timeout=30_000)
    except PWTimeout:
        await _save_debug(page, f"noresults-{doc_code}")
        return False
    return True


async def _scrape_rows(page) -> list:
    return await page.evaluate(
        """() => {
            const out = [];
            for (const t of document.querySelectorAll('table, [role="grid"]')) {
                const headerRow = t.querySelector('thead tr, tr[role="row"]:first-child, tr');
                const headers = headerRow
                    ? Array.from(headerRow.querySelectorAll('th, [role="columnheader"], td'))
                           .map(h => (h.innerText || '').trim())
                    : [];
                for (const row of t.querySelectorAll('tbody tr, [role="row"]')) {
                    const cells = Array.from(row.querySelectorAll('td, [role="gridcell"]'))
                                       .map(c => (c.innerText || '').trim());
                    if (cells.length < 2) continue;
                    const rec = {};
                    cells.forEach((v, i) => { rec[headers[i] || ('col' + i)] = v; });
                    rec.__raw_text = cells.join(' | ');
                    const link = row.querySelector('a[href]');
                    if (link) rec.__doc_url = link.href;
                    out.push(rec);
                }
            }
            return out;
        }"""
    )


async def _next_page(page) -> bool:
    btn = await page.query_selector(
        "a.next, button.next, [aria-label='Next'], [aria-label='Next Page']"
    )
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
    log.info("search %s  %s..%s", doc_code, date_from, date_to)
    ok = await _search(page, doc_code, date_from, date_to)
    if not ok:
        log.warning("  form/submit failed for %s", doc_code)
        return []
    rows = await _scrape_rows(page)
    for _ in range(1, max_pages):
        if not await _next_page(page):
            break
        rows.extend(await _scrape_rows(page))
    log.info("  %s: %d rows", doc_code, len(rows))
    return rows


def extract_apns(text: str) -> list:
    return list(set(APN_RE.findall(text or "")))


def normalize_row(raw: dict, doc_code: str) -> dict:
    text = raw.get("__raw_text", "")
    return {
        "county":        "Pima",
        "signal_type":   DOC_CODES.get(doc_code, doc_code),
        "doc_code":      doc_code,
        "doc_number":    raw.get("Document Number") or raw.get("Seq Number") or raw.get("col0"),
        "record_date":   raw.get("Recording Date") or raw.get("Date Recorded") or raw.get("col1"),
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
    ap = argparse.ArgumentParser(description="Pima Recorder signal scraper")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--doc-codes", default=",".join(DOC_CODES.keys()))
    args = ap.parse_args()
    codes = [c.strip().upper() for c in args.doc_codes.split(",") if c.strip()]
    asyncio.run(run(args.days, codes))


if __name__ == "__main__":
    main()
