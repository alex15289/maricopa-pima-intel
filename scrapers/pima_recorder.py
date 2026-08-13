#!/usr/bin/env python3
"""
Pima County Recorder — ATTENDED production scraper (Tyler EagleWeb portal).

The Pima recorder portal (pimacountyaz-web.tylerhost.net) gates every search
behind a once-per-session disclaimer + reCAPTCHA, and the doc-type filter only
commits through a real-keystroke autocomplete. So this scraper is ATTENDED:

    ONE human action — you accept the disclaimer and solve the reCAPTCHA in the
    browser window this opens. Then press Enter in the terminal and the script
    drives every search itself.

This runs LOCALLY and ON DEMAND (a wholesaler runs it when they want fresh Pima
recorder distress docs). It is deliberately NOT part of the 4am GitHub Actions
automation — a headless runner can't solve the reCAPTCHA.

Guarantees:
  - FAIL LOUD on session loss. If the portal 302s back to /web/user/disclaimer
    mid-run, we stop with SESSION_EXPIRED, save a checkpoint, and tell you to
    re-accept — never a silent zero.
  - CHECKPOINT / RESUME. Each doc type's records are written as they're parsed,
    and completed types are recorded in a per-run state file. Re-running the
    same day resumes where it stopped.
  - PARTY-CHECK. For each doc type it prints a grantor→grantee sample so a
    label-that-lies (like the criminal RESTITUTION LIEN or medical NOTICE LIEN
    traps) is obvious; flags anything whose grantee looks like a single agency.

Output (canonical JSONL, same shape the pipeline consumes):
    data/pima_recorder_docs_portal.jsonl

Usage:
    python scrapers/pima_recorder.py                 # last 3 days, all ON types
    python scrapers/pima_recorder.py --days 30       # wider window (backfill)
    python scrapers/pima_recorder.py --types NOTICE SALE,LIS PENDENS
    python scrapers/pima_recorder.py --include-off   # also pull off-by-default types

Requires:  pip install playwright ; python -m playwright install chromium
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    sys.exit("playwright required: pip install playwright && python -m playwright install chromium")

PORTAL = "https://pimacountyaz-web.tylerhost.net/web/user/disclaimer"
DISCLAIMER_RX = "user/disclaimer"

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_PATH = DATA_DIR / "pima_recorder_docs_portal.jsonl"
STATE_PATH = DATA_DIR / "_pima_recorder_state.json"

# Approved Pima doc types (Part B recon). `code` aligns foreclosure types with
# the NS/CQ/TD lifecycle the pipeline already runs; `deed` marks vesting deeds
# that resolve by the SEQ_NUM_D exact join (the rest resolve by name).
# `on` = default-on in a routine run (off types still pullable with --include-off).
DOC_TYPES: dict[str, dict] = {
    "NOTICE SALE":                 {"code": "NS",       "doc_type": "Notice of Trustee Sale",       "category": "Foreclosure",     "on": True},
    "CANCELLATION NOTICE SALE":    {"code": "CQ",       "doc_type": "NTS Cancelled",                "category": "Foreclosure",     "on": True, "kill": "cancelled"},
    "TRUSTEES DEED":               {"code": "TD",       "doc_type": "Trustee's Deed",               "category": "Foreclosure",     "on": True, "kill": "completed", "deed": True},
    "LIS PENDENS":                 {"code": "LISP",     "doc_type": "Lis Pendens",                  "category": "Legal",           "on": True},
    "CERTIFICATE DEATH":           {"code": "CTFDTH",   "doc_type": "Death Certificate",            "category": "Estate",          "on": True},
    "AFFIDAVIT TERM JT/CP":        {"code": "AFFTJT",   "doc_type": "Affidavit Terminating JT/CP",  "category": "Estate",          "on": True},
    "DEED DISTRIBUTION":           {"code": "DEEDDIST", "doc_type": "Deed of Distribution",         "category": "Estate",          "on": True, "deed": True},
    "AFFIDAVIT SUCCESSION":        {"code": "AFFSUC",   "doc_type": "Affidavit of Succession",      "category": "Estate",          "on": True},
    "FEDERAL LIEN":                {"code": "FED",      "doc_type": "Federal Tax Lien",             "category": "Tax & Liens",     "on": True},
    "STATE LIEN":                  {"code": "STATE",    "doc_type": "State Tax Lien",               "category": "Tax & Liens",     "on": True},
    "CITY LIEN":                   {"code": "CITY",     "doc_type": "City Lien",                    "category": "Tax & Liens",     "on": True},
    "BENEFICIARY DEED":            {"code": "BENE",     "doc_type": "Beneficiary Deed",             "category": "Estate Planning", "on": True},
    "REVOCATION BENEFICIARY DEED": {"code": "REVBENE",  "doc_type": "Beneficiary Deed Revocation",  "category": "Estate Planning", "on": True},
    "DISCLAIMER DEED":             {"code": "DISC",     "doc_type": "Disclaimer Deed",              "category": "Legal",           "on": True},
    # off by default (pulled only with --include-off; shown off in the dashboard)
    "JUDGMENT":                    {"code": "JDG",      "doc_type": "Judgment",                     "category": "Legal",           "on": False},
    "AHCCCS LIEN":                 {"code": "AHCS",     "doc_type": "AHCCCS Lien",                  "category": "Tax & Liens",     "on": False},
    "MECHANICS LIEN":              {"code": "MECH",     "doc_type": "Mechanics Lien",               "category": "Tax & Liens",     "on": False},
}


class SessionExpired(RuntimeError):
    pass


def log(msg: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S')} {msg}", flush=True)


# ---------------------------------------------------------------------------
# session state / checkpoint
# ---------------------------------------------------------------------------
def load_state(run_date: str) -> dict:
    if STATE_PATH.exists():
        try:
            s = json.loads(STATE_PATH.read_text())
            if s.get("run_date") == run_date:
                return s
        except Exception:
            pass
    return {"run_date": run_date, "completed": []}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=1))


def _norm_date(s: str) -> str | None:
    s = (s or "").strip()
    for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d", "%b %d, %Y"):
        try:
            return datetime.strptime(s[:11].strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# portal navigation
# ---------------------------------------------------------------------------
def assert_session(page) -> None:
    """Fail loud if the portal has bounced us back to the disclaimer."""
    if DISCLAIMER_RX in page.url:
        raise SessionExpired()


def enter_doc_type_search(page) -> None:
    """Home -> Official Records Search -> Document Type Search (mints the ctx)."""
    page.goto("https://pimacountyaz-web.tylerhost.net/web/", wait_until="domcontentloaded")
    assert_session(page)
    page.get_by_text("Official Records Search - Web", exact=False).first.click()
    page.get_by_text("Document Type Search - Web", exact=False).first.click()
    page.wait_for_selector("#field_selfservice_documentTypes-containsInput", timeout=15000)
    assert_session(page)


# The doc-type autocomplete combo only commits through fragile real-keystroke
# interaction, and even then the server search is stateful/laggy. Far more
# robust: run an UNFILTERED date-range search (the grid renders every doc type
# with its label) and filter to the approved types in code. A wide window
# exceeds the render cap, so we chunk the date range into CHUNK_DAYS-day slices.
CHUNK_DAYS = 7           # ~2,900 records/slice — renders and paginates cleanly
PAGE_SIZE_GUESS = 100    # portal renders 100 rows/page

# Structural parser + paginator, run inside the page. Reads each result row's
# labeled columns (Recording Date / Grantor (n) / Grantee (n)) — innerText regex
# fails on fetched pages (no layout), so we walk the column elements directly.
_PAGE_JS = r"""
async (args) => {
  const [ctx, from, to] = args;
  const parseRow = (row) => {
    const seq = (row.textContent.match(/\b(\d{11})\b/) || [])[1];
    const type = ((row.textContent.match(/\d{11}\s*[•·]\s*([A-Z][A-Z0-9 \/&.\-]+)/) || [])[1] || '').trim();
    const els = [...row.querySelectorAll('.selfServiceSearchResultColumn, .selfServiceSearchResultCollapsed, .selfServiceSearchFullResult')];
    let mode = null, dt = ''; const g = [], e = [];
    for (const el of els) {
      const cls = el.className || '', txt = el.textContent.trim();
      if (/selfServiceSearchResultColumn/.test(cls)) {
        mode = /Grantor/i.test(txt) ? 'g' : /Grantee/i.test(txt) ? 'e' : /Recording Date/i.test(txt) ? 'd' : null;
        continue;
      }
      if (!txt || txt === 'View') continue;
      if (mode === 'd') dt = dt || (txt.match(/[0-9\/]+/) || [''])[0];
      else if (mode === 'g') g.push(txt);
      else if (mode === 'e') e.push(txt);
    }
    return { seq, type, dt, grantors: g, grantees: e };
  };
  const all = [];
  for (let p = 1; p <= 60; p++) {
    const r = await fetch(`/web/searchResults/${ctx}?page=${p}&_=` + Date.now(), { cache: 'no-store' });
    if (/user\/disclaimer/i.test(r.url)) return { expired: true, records: all };
    const html = await r.text();
    const doc = new DOMParser().parseFromString(html, 'text/html');
    const rows = [...doc.querySelectorAll('.selfServiceSearchRowRight')].filter(x => /\d{11}/.test(x.textContent));
    if (!rows.length) break;
    for (const row of rows) { const rec = parseRow(row); if (rec.seq) all.push(rec); }
  }
  return { expired: false, records: all };
}
"""


def _ctx(page) -> str:
    import re
    m = re.search(r"DOCSEARCH\d+S\d+", page.url)
    return m.group(0) if m else ""


def search_and_collect(page, begin: str, end: str) -> list[dict]:
    """Unfiltered date-range search, then paginate + structurally parse all rows.
    Returns every record (all doc types) in the window. Raises SessionExpired."""
    enter_doc_type_search(page)
    page.fill("#field_RecordingDateID_DOT_StartDate", begin)
    page.fill("#field_RecordingDateID_DOT_EndDate", end)
    page.click("#searchButton")
    page.wait_for_timeout(3500)
    assert_session(page)
    res = page.evaluate(_PAGE_JS, [_ctx(page), begin, end])
    if res.get("expired"):
        raise SessionExpired()
    # dedupe by seq within the window
    seen, out = set(), []
    for r in res["records"]:
        if r["seq"] and r["seq"] not in seen:
            seen.add(r["seq"])
            out.append(r)
    return out


# ---------------------------------------------------------------------------
# record building + party check
# ---------------------------------------------------------------------------
def build_record(raw: dict, meta: dict, label: str) -> dict | None:
    seq = (raw.get("seq") or "").strip()
    recd = _norm_date(raw.get("dt", ""))
    if not seq or not recd:
        return None
    names = (raw.get("grantors") or []) + (raw.get("grantees") or [])
    return {
        "county":        "Pima",
        "source":        "pima_recorder_portal",
        "doc_code":      meta["code"],
        "doc_type":      meta["doc_type"],
        "category":      meta["category"],
        "doc_number":    seq,
        "recorded_date": recd,
        "names":         names,
        "grantors":      raw.get("grantors") or [],
        "grantees":      raw.get("grantees") or [],
        "is_vesting_deed": bool(meta.get("deed")),
        **({"kill": meta["kill"]} if meta.get("kill") else {}),
        "fetched_at":    datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


def party_check(label: str, records: list[dict]) -> None:
    """Print a grantor->grantee sample; warn if grantees are a single agency
    (the RESTITUTION/NOTICE-lien trap pattern)."""
    if not records:
        return
    sample = records[: min(4, len(records))]
    log(f"    party-check [{label}]:")
    for r in sample:
        g = ", ".join(r["grantors"][:1]) or "?"
        e = ", ".join(r["grantees"][:1]) or "?"
        log(f"      {g}  ->  {e}")
    # Trap signature = the lien attaches to a PERSON (settlement/judgment), not
    # their real property: criminal restitution (grantee ARIZONA STATE) or medical
    # injury liens (grantee hospital). NOT plain agency grantees — city/tax liens
    # legitimately name a govt grantee while the GRANTOR is the property owner
    # (verified: CITY LIEN grantor = owner, grantee TUCSON CITY OF).
    grantees = [ (r["grantees"] or [""])[0].upper() for r in records if r["grantees"] ]
    TRAP = ("ARIZONA STATE", "STATE OF ARIZONA", "MEDICAL CENTER", "MEDICAL CTR",
            "HOSPITAL", "HEALTHCARE", "HEALTH CARE")
    if grantees and sum(any(a in g for a in TRAP) for g in grantees) / len(grantees) > 0.8:
        log(f"    ⚠ TRAP? [{label}] >80% of grantees are the State or a hospital — "
            f"likely a person-lien with no property nexus (like RESTITUTION/NOTICE liens). "
            f"Review before trusting; consider dropping.")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def _date_chunks(begin: date, end: date, size: int):
    """Yield (start,end) MM/DD/YYYY slices of <= size days across [begin,end]."""
    cur = begin
    while cur <= end:
        stop = min(cur + timedelta(days=size - 1), end)
        yield cur.strftime("%m/%d/%Y"), stop.strftime("%m/%d/%Y"), cur.isoformat()
        cur = stop + timedelta(days=1)


def run(labels: list[str], days: int) -> None:
    end = date.today()
    begin = end - timedelta(days=days)
    run_date = end.isoformat()
    state = load_state(run_date)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # APPROVED = portal grid label -> meta. We search UNFILTERED and keep only
    # these labels, so we never touch the fragile doc-type combo.
    approved = {lbl: DOC_TYPES[lbl] for lbl in labels}

    mode = "a" if state["completed"] and OUT_PATH.exists() else "w"
    out_f = OUT_PATH.open(mode)
    written = 0
    per_type_samples: dict[str, list[dict]] = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto(PORTAL, wait_until="domcontentloaded")

        print("\n" + "=" * 68)
        print("  PIMA RECORDER — ONE HUMAN ACTION REQUIRED")
        print("  In the browser window: accept the disclaimer + solve the")
        print("  reCAPTCHA, then come back here and press Enter.")
        print("=" * 68)
        input("  [Enter] once you've accepted the disclaimer... ")
        try:
            enter_doc_type_search(page)
        except SessionExpired:
            sys.exit("SESSION not accepted — still on the disclaimer. Re-run and accept it.")

        chunks = list(_date_chunks(begin, end, CHUNK_DAYS))
        log(f"session live. window {begin}..{end} ({days}d) in {len(chunks)} "
            f"{CHUNK_DAYS}-day chunks. keeping {len(approved)} doc types.")
        try:
            for a, b, key in chunks:
                if key in state["completed"]:
                    log(f"▶ chunk {a}..{b}: already done this run — skipping")
                    continue
                raw = search_and_collect(page, a, b)          # all types in window
                kept = 0
                for r in raw:
                    meta = approved.get(r.get("type"))
                    if not meta:
                        continue
                    rec = build_record(r, meta, r["type"])
                    if not rec:
                        continue
                    out_f.write(json.dumps(rec) + "\n")
                    kept += 1
                    per_type_samples.setdefault(rec["doc_type"], []).append(rec)
                out_f.flush()
                written += kept
                log(f"▶ chunk {a}..{b}: {len(raw)} docs in window, {kept} approved kept")
                state["completed"].append(key)
                save_state(state)

        except SessionExpired:
            out_f.close()
            save_state(state)
            print("\n" + "!" * 68)
            print("  SESSION_EXPIRED — the portal bounced back to the disclaimer.")
            print(f"  Progress saved ({written} records, {len(state['completed'])}/{len(chunks)} chunks).")
            print("  Re-run the same command, re-accept the disclaimer, and it resumes.")
            print("!" * 68)
            browser.close()
            sys.exit(2)

        out_f.close()
        browser.close()

    # per-type party check (flag the restitution/medical trap signature)
    for dt, recs in sorted(per_type_samples.items()):
        party_check(dt, recs)

    (DATA_DIR / "_pima_recorder_last_run.json").write_text(json.dumps({
        "last_run": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "run_date": run_date, "window_days": days,
        "types": sorted(per_type_samples), "records": written,
    }, indent=1))
    log(f"✓ done. {written} records → {OUT_PATH.name}. "
        f"{len(state['completed'])}/{len(chunks)} chunks complete.")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=3, help="Days back to scan (default 3; use 30 for backfill)")
    ap.add_argument("--types", type=str, default=None,
                    help="Comma-separated portal labels to pull (default: all default-on types)")
    ap.add_argument("--include-off", action="store_true",
                    help="Also pull the off-by-default types (Judgment/AHCCCS/Mechanics)")
    ap.add_argument("--fresh", action="store_true", help="Ignore any same-day checkpoint and start over")
    args = ap.parse_args(argv)

    if args.types:
        labels = [t.strip().upper() for t in args.types.split(",") if t.strip()]
        unknown = [l for l in labels if l not in DOC_TYPES]
        if unknown:
            sys.exit(f"unknown doc types: {unknown}\nknown: {list(DOC_TYPES)}")
    else:
        labels = [l for l, m in DOC_TYPES.items() if m["on"] or args.include_off]

    if args.fresh and STATE_PATH.exists():
        STATE_PATH.unlink()
    run(labels, args.days)


if __name__ == "__main__":
    main()
