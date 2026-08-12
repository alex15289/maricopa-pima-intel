#!/usr/bin/env python3
"""
Pima County deed-transfer documents — via the public GIS LandRecords service.

The Pima recorder search portal is a stateful, session-gated Tyler EagleWeb app
(disclaimer + reCAPTCHA per session) — not automatable unattended. But the free
ArcGIS layer carries the recorder document number and recording date of each
parcel's most-recent deed, so we harvest deed TRANSFERS as first-class recorded
documents without touching the portal.

Source layer (verified 2026-08-11, Phase 0 recon):
    https://gisdata.pima.gov/arcgis1/rest/services/GISOpenData/LandRecords/MapServer/12
    "Parcels - Regional" — 448K parcels, one row per parcel, reflecting the
    latest recorded deed:
        PARCEL     9-char taxcode (APN)
        SEQ_NUM_D  recorder sequence/document number of the latest deed
        RECORDDATE YYYYMMDD of that recording
        MAIL1-5    owner + mailing block
        ADDRESS_OL situs address, JURIS_OL jurisdiction
        PARCEL_USE use code, FCV full cash value, LAT/LON

Each parcel whose RECORDDATE falls in the scan window is emitted as one
"Deed Transfer" document lead. Doc number = SEQ_NUM_D, recorded_date =
RECORDDATE. Owner/APN/address come straight from the layer (already resolved —
no name matching needed, unlike Maricopa).

Incremental model mirrors the Maricopa scraper: merge into a cumulative store,
dedupe by doc_number, prune past --retention days.

Usage:
    python scrapers/pima_deeds.py --days 30            # daily/backfill window
    python scrapers/pima_deeds.py --days 400 --retention 400   # deep backfill

Output:
    data/pima_recorder_docs.jsonl   (cumulative document store)
"""
from __future__ import annotations
import argparse
import json
import logging
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

LAYER = ("https://gisdata.pima.gov/arcgis1/rest/services/"
         "GISOpenData/LandRecords/MapServer/12/query")
PAGE_SIZE = 2000
REQUEST_TIMEOUT = 60
RETRY_BACKOFF = [2, 5, 15, 45, 90, 180, 300]
USER_AGENT = "maricopa-pima-intel/1.0 (public-records research)"

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STORE_PATH = DATA_DIR / "pima_recorder_docs.jsonl"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("pima_deeds")

STATE_RE = re.compile(r"\b([A-Z]{2})\b\s*$")


def _get_json(params: dict) -> dict:
    url = f"{LAYER}?{urlencode(params)}"
    last = None
    for wait in [0] + RETRY_BACKOFF:
        if wait:
            time.sleep(wait)
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (HTTPError, URLError, json.JSONDecodeError, TimeoutError) as e:
            last = e
            log.warning(f"  retry after error: {e}")
    raise RuntimeError(f"failed to fetch {url}: {last}")


def _clean(s) -> str:
    return "" if s is None else str(s).strip().strip(".").strip()


def parse_owner_block(row: dict) -> dict:
    """MAIL1 = owner name; MAIL2-5 end in a 'CITY ST' line. (Same logic as the
    parcel-master scraper.)"""
    lines = [_clean(row.get(f"MAIL{i}")) for i in range(1, 6)]
    owner = lines[0]
    body = [x for x in lines[1:] if x and x != "."]
    mail_city = mail_state = mail_address = ""
    idx = -1
    for i in range(len(body) - 1, -1, -1):
        m = STATE_RE.search(body[i])
        if m:
            mail_state = m.group(1).upper()
            mail_city = body[i][:m.start()].strip()
            idx = i
            break
    if idx >= 0 and body[:idx]:
        mail_address = body[:idx][-1]
    elif idx < 0:
        mail_address = " ".join(body)
    return {"owner": owner or None, "mail_address": mail_address or None,
            "mail_city": mail_city or None, "mail_state": mail_state or None}


def dash_apn(raw: str) -> str:
    raw = _clean(raw)
    if raw and "-" not in raw and len(raw) >= 8:
        return f"{raw[:3]}-{raw[3:5]}-{raw[5:]}"
    return raw


def fetch_window(cutoff_yyyymmdd: str) -> list[dict]:
    where = f"RECORDDATE >= '{cutoff_yyyymmdd}'"
    out_fields = ",".join([
        "PARCEL", "SEQ_NUM_D", "RECORDDATE", "MAIL1", "MAIL2", "MAIL3", "MAIL4",
        "MAIL5", "ZIP", "ADDRESS_OL", "JURIS_OL", "PARCEL_USE", "FCV", "LAT", "LON",
    ])
    # total count first
    cnt = _get_json({"where": where, "returnCountOnly": "true", "f": "json"})
    total = int(cnt.get("count", 0))
    log.info(f"Pima deeds recorded on/after {cutoff_yyyymmdd}: {total:,}")

    rows, offset = [], 0
    while offset < total:
        data = _get_json({
            "where": where, "outFields": out_fields, "f": "json",
            "resultOffset": offset, "resultRecordCount": PAGE_SIZE,
            "orderByFields": "OBJECTID ASC", "returnGeometry": "false",
        })
        feats = data.get("features", [])
        if not feats:
            break
        rows.extend(f.get("attributes", {}) for f in feats)
        offset += PAGE_SIZE
        if offset % 20000 == 0:
            log.info(f"  fetched {min(offset, total):,}/{total:,}")
    return rows


def normalize(raw: dict) -> dict | None:
    seq = raw.get("SEQ_NUM_D")
    rec = _clean(raw.get("RECORDDATE"))
    if not seq or not rec or len(rec) < 8:
        return None
    doc_number = str(int(seq)) if isinstance(seq, float) else str(seq)
    recorded = f"{rec[:4]}-{rec[4:6]}-{rec[6:8]}"
    ob = parse_owner_block(raw)
    juris = _clean(raw.get("JURIS_OL")).upper()
    site_city = juris if juris and "UNINCORPORATED" not in juris and juris != "PIMA COUNTY" else None
    apn = dash_apn(raw.get("PARCEL"))
    fcv = raw.get("FCV")
    return {
        "county":        "Pima",
        "source":        "pima_gis_layer12",
        "doc_type":      "Deed Transfer",
        "doc_code":      "DEED",
        "category":      "Transfers",
        "doc_number":    doc_number,
        "recorded_date": recorded,
        "names":         [ob["owner"]] if ob["owner"] else [],
        # Pima deeds are already parcel-resolved — carry enrichment inline
        "apn":           apn or None,
        "apn_norm":      (apn or "").replace("-", "").upper() or None,
        "resolved":      bool(apn),
        "owner":         ob["owner"],
        "site_address":  _clean(raw.get("ADDRESS_OL")) or None,
        "site_city":     site_city,
        "site_zip":      None,
        "mail_address":  ob["mail_address"],
        "mail_city":     ob["mail_city"],
        "mail_state":    ob["mail_state"],
        "mail_zip":      (_clean(raw.get("ZIP"))[:5] or None),
        "use_code":      _clean(raw.get("PARCEL_USE")) or None,
        "fcv":           fcv if isinstance(fcv, (int, float)) else None,
        "latitude":      raw.get("LAT"),
        "longitude":     raw.get("LON"),
        "fetched_at":    datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


def load_store() -> dict[str, dict]:
    docs: dict[str, dict] = {}
    if STORE_PATH.exists():
        with STORE_PATH.open() as f:
            for line in f:
                try:
                    r = json.loads(line)
                    docs[r["doc_number"]] = r
                except Exception:
                    continue
    return docs


def run(days_back: int, retention_days: int) -> None:
    end = date.today()
    cutoff_scan = (end - timedelta(days=days_back)).strftime("%Y%m%d")
    log.info(f"scan window: RECORDDATE >= {cutoff_scan} ({days_back} days back)")

    store = load_store()
    log.info(f"cumulative store: {len(store):,} existing deed docs")

    rows = fetch_window(cutoff_scan)
    new = 0
    for raw in rows:
        rec = normalize(raw)
        if not rec:
            continue
        if rec["doc_number"] not in store:
            new += 1
        store[rec["doc_number"]] = rec

    cutoff_keep = (end - timedelta(days=retention_days)).isoformat()
    kept = {k: r for k, r in store.items() if (r.get("recorded_date") or "9999") >= cutoff_keep}
    pruned = len(store) - len(kept)

    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STORE_PATH.open("w") as f:
        for r in sorted(kept.values(), key=lambda x: x.get("recorded_date") or "", reverse=True):
            f.write(json.dumps(r) + "\n")
    log.info(f"✓ store: {len(kept):,} deeds ({new:,} new, {pruned:,} pruned) → {STORE_PATH}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=30,
                    help="Days back from today to scan RECORDDATE (default 30)")
    ap.add_argument("--retention", type=int, default=180,
                    help="Prune stored deeds older than this many days (default 180)")
    args = ap.parse_args(argv)
    run(days_back=args.days, retention_days=args.retention)


if __name__ == "__main__":
    main()
