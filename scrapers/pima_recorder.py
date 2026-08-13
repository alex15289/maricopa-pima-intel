#!/usr/bin/env python3
"""
Pima County Recorder — ATTENDED production scraper (Tyler EagleWeb portal).

The Pima recorder portal (pimacountyaz-web.tylerhost.net) gates every search
behind a once-per-session disclaimer + reCAPTCHA, and the doc-type filter only
commits through a real-keystroke autocomplete. So this scraper is ATTENDED:

    ONE human action — you accept the disclaimer and solve the reCAPTCHA in the
    browser window this opens. The script watches that window, detects the
    acceptance itself, and then drives every search — no terminal interaction.

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


def wait_for_acceptance(page, timeout_s: int = 900) -> None:
    """Block until the disclaimer is accepted IN THIS BROWSER — on acceptance
    the page navigates off /user/disclaimer. Watching the page (rather than
    trusting a terminal keypress) makes an accept in the wrong window
    impossible to mistake for success: we just keep waiting and keep saying so."""
    log("waiting for you to accept the disclaimer in the Chrome-for-Testing window...")
    waited = 0
    while DISCLAIMER_RX in page.url:
        if waited >= timeout_s:
            sys.exit(f"Gave up after {timeout_s // 60} min waiting for the disclaimer.")
        page.wait_for_timeout(2000)
        waited += 2
        if waited % 30 == 0:
            log(f"  still waiting ({waited}s) — the accept + captcha must happen in the "
                f"'Chrome for Testing' window this script opened, not your regular Chrome")
    page.wait_for_load_state("domcontentloaded")
    log("disclaimer accepted — session live.")


def _visible_clickables(page) -> list[str]:
    """Texts of links/buttons on the page — logged when menu navigation fails
    so a failed run says exactly what the portal showed instead."""
    try:
        return page.evaluate(
            "() => [...document.querySelectorAll('a, button, [onclick]')]"
            ".filter(e => e.offsetParent !== null)"
            ".map(e => e.textContent.trim().replace(/\\s+/g,' '))"
            ".filter(t => t && t.length < 80)")
    except Exception:
        return []


def _click_menu(page, text: str):
    """Click a menu entry by (lax) text and return the page the UI continued
    on — the Tyler portal sometimes opens the next step in a new tab, which a
    single page handle would never see."""
    loc = page.get_by_text(text, exact=False).first
    loc.wait_for(state="visible", timeout=60000)
    before = list(page.context.pages)
    loc.click()
    page.wait_for_timeout(2000)
    new = [p for p in page.context.pages if p not in before]
    if new:
        new[0].wait_for_load_state("domcontentloaded")
        log(f"    (portal opened a new tab for '{text}' — following it)")
        return new[0]
    return page


def _answer_continue_modal(page) -> None:
    """Tyler interposes a session-restore modal ('Yes - Continue / No - Start
    Over') that blocks the search menu from rendering until answered. It can
    appear on any /web/ load, so every navigation must clear it."""
    try:
        page.wait_for_timeout(1500)
        cont = page.get_by_text("Yes - Continue", exact=False).first
        if cont.count() and cont.is_visible():
            log("    (answering the portal's continue-session modal)")
            cont.click()
            page.wait_for_timeout(1500)
    except Exception:
        pass


# Direct URL of the Official Records Search submenu (the action group that
# lists Document Type Search). The home page's menu tiles are rendered from a
# /web/homeActions XHR that never renders in an automated browser session, but
# this action-group page is server-routed and mints its session instance (S1)
# on first visit — so we skip the tiles entirely and land here.
ACTION_GROUP_URL = "https://pimacountyaz-web.tylerhost.net/web/action/ACTIONGROUP55S1"


def enter_doc_type_search(page):
    """Official Records action group -> Document Type Search (mints the ctx).
    Returns the page the search UI lives on (may differ from the one passed in
    if the portal opened a tab)."""
    page.goto(ACTION_GROUP_URL, wait_until="domcontentloaded")
    assert_session(page)
    _answer_continue_modal(page)
    try:
        try:
            page.get_by_text("Document Type Search", exact=False).first.wait_for(
                state="visible", timeout=15000)
        except PWTimeout:
            # bounced ("your options have changed") — fall back to the tiles
            log("    direct action-group route bounced; trying the home menu tiles")
            page.goto("https://pimacountyaz-web.tylerhost.net/web/", wait_until="domcontentloaded")
            assert_session(page)
            _answer_continue_modal(page)
            page = _click_menu(page, "Official Records Search")
            assert_session(page)
        page = _click_menu(page, "Document Type Search")
        # ready = the field we actually use. (NOT the doc-type autocomplete —
        # that widget doesn't exist for fresh anonymous sessions, and this
        # scraper searches unfiltered by date anyway.)
        page.wait_for_selector("#field_RecordingDateID_DOT_StartDate", timeout=60000)
    except PWTimeout:
        log(f"  ✗ menu navigation failed at {page.url}")
        log(f"    visible clickables there: {_visible_clickables(page)[:40]}")
        try:
            log("    page text: " + repr(page.evaluate(
                "() => document.body.innerText.replace(/\\s+/g,' ').slice(0,500)")))
        except Exception:
            pass
        raise
    assert_session(page)
    return page


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
    page = enter_doc_type_search(page)
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
def load_store() -> dict[str, dict]:
    """Cumulative store keyed by doc_number (same model as the Maricopa
    scraper): a routine --days 3 run merges into the backfill instead of
    truncating it."""
    docs: dict[str, dict] = {}
    if OUT_PATH.exists():
        with OUT_PATH.open() as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    docs[rec["doc_number"]] = rec
                except Exception:
                    continue
    return docs


def flush_store(store: dict[str, dict]) -> None:
    with OUT_PATH.open("w") as f:
        for rec in sorted(store.values(), key=lambda r: r.get("recorded_date") or "", reverse=True):
            f.write(json.dumps(rec) + "\n")


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

    store = load_store()
    log(f"cumulative store: {len(store):,} existing docs")
    new_count = 0
    per_type_samples: dict[str, list[dict]] = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto(PORTAL, wait_until="domcontentloaded")

        print("\n" + "=" * 68)
        print("  PIMA RECORDER — ONE HUMAN ACTION REQUIRED")
        print("  In the 'Chrome for Testing' window that just opened: accept the")
        print("  disclaimer + solve the reCAPTCHA. The script detects it and")
        print("  continues by itself — nothing to press here.")
        print("=" * 68)
        wait_for_acceptance(page)
        # The menu only renders for a session whose disclaimer is truly accepted
        # (menus are AJAX; an unaccepted session gets bare site chrome). If it
        # still doesn't render, bounce THIS window back to the disclaimer and
        # let the human retry — one more captcha, not a whole relaunch.
        for attempt in range(3):
            try:
                page = enter_doc_type_search(page)
                break
            except (SessionExpired, PWTimeout):
                if attempt == 2:
                    browser.close()
                    sys.exit("PORTAL_TIMEOUT — search menu never rendered after 3 accepts. "
                             "Check the clickables dump above; the portal may have changed.")
                print("\n  Menu didn't render — reloading the disclaimer; accept it again.")
                page.goto(PORTAL, wait_until="domcontentloaded")
                wait_for_acceptance(page)

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
                    if rec["doc_number"] not in store:
                        new_count += 1
                    store[rec["doc_number"]] = rec
                    kept += 1
                    per_type_samples.setdefault(rec["doc_type"], []).append(rec)
                flush_store(store)
                log(f"▶ chunk {a}..{b}: {len(raw)} docs in window, {kept} approved kept "
                    f"({new_count} new this run)")
                state["completed"].append(key)
                save_state(state)

        except SessionExpired:
            save_state(state)
            print("\n" + "!" * 68)
            print("  SESSION_EXPIRED — the portal bounced back to the disclaimer.")
            print(f"  Progress saved ({new_count} new records, {len(state['completed'])}/{len(chunks)} chunks).")
            print("  Re-run the same command, re-accept the disclaimer, and it resumes.")
            print("!" * 68)
            browser.close()
            sys.exit(2)
        except PWTimeout:
            save_state(state)
            print("\n" + "!" * 68)
            print("  PORTAL_TIMEOUT — a page or search never rendered (slow portal or a")
            print(f"  changed menu; clickables dump above). Progress saved "
                  f"({new_count} new records, {len(state['completed'])}/{len(chunks)} chunks).")
            print("  Re-run the same command to resume from the last completed chunk.")
            print("!" * 68)
            browser.close()
            sys.exit(3)

        browser.close()

    # per-type party check (flag the restitution/medical trap signature)
    for dt, recs in sorted(per_type_samples.items()):
        party_check(dt, recs)

    (DATA_DIR / "_pima_recorder_last_run.json").write_text(json.dumps({
        "last_run": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "run_date": run_date, "window_days": days,
        "types": sorted(per_type_samples), "records": len(store), "new": new_count,
    }, indent=1))
    log(f"✓ done. {new_count} new records this run, store now {len(store):,} → {OUT_PATH.name}. "
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
