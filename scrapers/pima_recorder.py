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


def run_search(page, label: str, begin: str, end: str) -> int:
    """Filter to one doc type via the real-keystroke autocomplete, set the date
    range, submit. Returns the total result count. Raises SessionExpired."""
    enter_doc_type_search(page)
    combo = page.locator("#field_selfservice_documentTypes-containsInput")
    combo.click()
    combo.fill("")
    combo.type(label, delay=40)                 # real keystrokes trigger the AJAX
    # click the exact-match option in the autocomplete dropdown
    page.wait_for_timeout(900)
    option = page.locator("#field_selfservice_documentTypes-aclist li, "
                          ".ui-menu-item, [role=option]").filter(has_text=label).first
    option.click(timeout=8000)
    page.fill("#field_RecordingDateID_DOT_StartDate", begin)
    page.fill("#field_RecordingDateID_DOT_EndDate", end)
    page.click("#searchButton")
    page.wait_for_timeout(2500)
    assert_session(page)
    total = page.evaluate("""() => {
        const m = document.body.innerText.match(/for\\s+(\\d[\\d,]*)\\s+Total Results/i);
        return m ? parseInt(m[1].replace(/,/g,'')) : 0;
    }""")
    return total


def parse_page(page) -> list[dict]:
    """Extract the rendered result rows: seq#, doc type, recording datetime,
    grantor(s), grantee(s). Structural DOM read (labels 'Grantor'/'Grantee')."""
    return page.evaluate("""() => {
        const out = [];
        // each record starts with an 11-digit sequence number heading
        const heads = [...document.querySelectorAll('h1,h2,h3,h4,a,div,span')]
            .filter(e => /^\\d{11}\\s*$/.test((e.textContent||'').trim()));
        for (const h of heads) {
            // climb to the record container (holds Grantor + Grantee)
            let rec = h;
            for (let i=0;i<6 && rec && !/Grantor/.test(rec.innerText||''); i++) rec = rec.parentElement;
            if (!rec) continue;
            const t = rec.innerText.replace(/\\u00a0/g,' ');
            const seq = (h.textContent||'').trim();
            const type = ((t.match(/·\\s*([A-Z][A-Z0-9 \\/&.\\-]+)/)||[])[1]||'').trim();
            const dt = (t.match(/Recording Date\\s*([0-9\\/]+\\s*[0-9:]*\\s*[AP]?M?)/i)||[])[1]||'';
            const gr = (t.match(/Grantor[^\\n]*\\n([\\s\\S]*?)\\n\\s*Grantee/i)||[])[1]||'';
            const ge = (t.match(/Grantee[^\\n]*\\n([\\s\\S]*?)(\\n\\s*(Related|Recording|View|$))/i)||[])[1]||'';
            const clean = s => s.split('\\n').map(x=>x.trim()).filter(Boolean);
            out.push({seq, type, dt, grantors: clean(gr), grantees: clean(ge)});
        }
        // dedupe by seq
        const seen = new Set(); return out.filter(r => r.seq && !seen.has(r.seq) && seen.add(r.seq));
    }""")


def next_page(page) -> bool:
    """Advance to the next results page if one exists."""
    nxt = page.locator("a[aria-label='Next'], a.next, button[aria-label='Next'], "
                       ".pagination-next:not(.disabled)").first
    try:
        if nxt.count() and nxt.is_enabled():
            nxt.click()
            page.wait_for_timeout(1500)
            return True
    except Exception:
        pass
    return False


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
def run(labels: list[str], days: int) -> None:
    end = date.today()
    begin = end - timedelta(days=days)
    begin_s, end_s = begin.strftime("%m/%d/%Y"), end.strftime("%m/%d/%Y")
    run_date = end.isoformat()
    state = load_state(run_date)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # fresh file for a new run date; append when resuming the same day
    mode = "a" if state["completed"] and OUT_PATH.exists() else "w"
    out_f = OUT_PATH.open(mode)
    written = 0

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

        # confirm the human actually got past the disclaimer
        try:
            enter_doc_type_search(page)
        except SessionExpired:
            sys.exit("SESSION not accepted — still on the disclaimer. Re-run and accept it.")
        log(f"session live. window {begin_s}..{end_s} ({days}d). "
            f"{len(labels)} doc types, {len(state['completed'])} already done.")

        try:
            for label in labels:
                if label in state["completed"]:
                    log(f"▶ {label}: already done this run — skipping")
                    continue
                meta = DOC_TYPES[label]
                try:
                    total = run_search(page, label, begin_s, end_s)
                except SessionExpired:
                    raise
                except Exception as e:
                    log(f"▶ {label}: search failed ({e}) — skipping, will retry next run")
                    continue

                records: list[dict] = []
                pages = 0
                while True:
                    for raw in parse_page(page):
                        rec = build_record(raw, meta, label)
                        if rec:
                            records.append(rec)
                    pages += 1
                    assert_session(page)
                    if not next_page(page) or pages >= 60:
                        break

                for rec in records:
                    out_f.write(json.dumps(rec) + "\n")
                out_f.flush()
                written += len(records)
                log(f"▶ {label}: {total} reported, {len(records)} parsed ({pages} pages)")
                party_check(label, records)
                state["completed"].append(label)
                save_state(state)

        except SessionExpired:
            out_f.close()
            save_state(state)
            print("\n" + "!" * 68)
            print("  SESSION_EXPIRED — the portal bounced back to the disclaimer.")
            print(f"  Progress saved ({written} records, {len(state['completed'])} types done).")
            print("  Re-run the same command, re-accept the disclaimer, and it resumes.")
            print("!" * 68)
            browser.close()
            sys.exit(2)

        out_f.close()
        browser.close()

    # stamp the run so the pipeline/dashboard can show freshness honestly
    (DATA_DIR / "_pima_recorder_last_run.json").write_text(json.dumps({
        "last_run": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "run_date": run_date, "window_days": days,
        "types": state["completed"], "records": written,
    }, indent=1))
    log(f"✓ done. {written} records → {OUT_PATH.name}. "
        f"{len(state['completed'])}/{len(labels)} types complete.")


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
