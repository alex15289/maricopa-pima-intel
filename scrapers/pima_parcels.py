"""
Pima County parcel master scraper.

Pima hosts its parcel layer via ArcGIS Online (services1.arcgis.com) under the
'Ezk9fcjSUkeadg6u' org ID — same REST semantics as Maricopa, different org.
The exact parcel layer URL can change; the open data portal at
https://gisopendata.pima.gov publishes the canonical "View Data Source" URL
for each dataset. We default to the most common parcels endpoint and let the
operator override with --url if it's been renamed.

Common Pima parcel endpoints to try in order:
  1. services1.arcgis.com/Ezk9fcjSUkeadg6u/arcgis/rest/services/Parcels/FeatureServer/0
  2. services1.arcgis.com/Ezk9fcjSUkeadg6u/arcgis/rest/services/Parcel/FeatureServer/0
  3. services1.arcgis.com/Ezk9fcjSUkeadg6u/arcgis/rest/services/MHArrangements/FeatureServer/0

Usage:
    python pima_parcels.py
    python pima_parcels.py --url <override>
    python pima_parcels.py --limit 5000

Output:
    data/pima_parcels.jsonl
    data/pima_parcels.csv
"""

import argparse
import csv
import json
import sys
import time
from pathlib import Path
from typing import Iterator, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

CANDIDATE_URLS = [
    "https://services1.arcgis.com/Ezk9fcjSUkeadg6u/arcgis/rest/services/Parcels/FeatureServer/0",
    "https://services1.arcgis.com/Ezk9fcjSUkeadg6u/arcgis/rest/services/Parcel/FeatureServer/0",
    "https://services1.arcgis.com/Ezk9fcjSUkeadg6u/arcgis/rest/services/MHArrangements/FeatureServer/0",
]

PAGE_SIZE = 2000  # Pima advertises MaxRecordCount: 2000 on MHArrangements
REQUEST_TIMEOUT = 60
RETRY_BACKOFF = [2, 5, 15, 45]
USER_AGENT = "maricopa-pima-intel/1.0 (+real estate intel)"

OUT_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_JSONL = OUT_DIR / "pima_parcels.jsonl"
OUT_CSV = OUT_DIR / "pima_parcels.csv"


def _get_json(url: str, params: dict) -> dict:
    query = urlencode(params)
    full_url = f"{url}?{query}"
    last_err: Optional[Exception] = None
    for wait in [0] + RETRY_BACKOFF:
        if wait:
            time.sleep(wait)
        try:
            req = Request(full_url, headers={"User-Agent": USER_AGENT})
            with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (HTTPError, URLError, json.JSONDecodeError, TimeoutError) as e:
            last_err = e
            print(f"  retry after error: {e}", file=sys.stderr)
    raise RuntimeError(f"failed to fetch {full_url}: {last_err}")


def probe_endpoint(base_url: str) -> Optional[dict]:
    """Returns the layer describe JSON if reachable, else None."""
    try:
        data = _get_json(base_url, {"f": "json"})
        if "error" not in data and data.get("type") == "Feature Layer":
            return data
    except Exception:
        pass
    return None


def pick_endpoint(override: Optional[str]) -> tuple:
    """Returns (query_url, describe_json). Tries candidates until one works."""
    candidates = [override] if override else CANDIDATE_URLS
    for base in candidates:
        if not base:
            continue
        info = probe_endpoint(base)
        if info:
            print(f"Using endpoint: {base}")
            print(f"  Layer: {info.get('name', '?')} — {len(info.get('fields', []))} fields")
            return f"{base}/query", info
    raise RuntimeError(
        "No working Pima parcel endpoint found. Visit "
        "https://gisopendata.pima.gov, find the Parcels dataset, click "
        "'View Data Source', copy the URL (without /query), and pass it "
        "with --url."
    )


def count_records(url: str, where: str) -> int:
    data = _get_json(url, {"where": where, "returnCountOnly": "true", "f": "json"})
    return int(data.get("count", 0))


def fetch_page(url: str, where: str, offset: int) -> list:
    params = {
        "where": where,
        "outFields": "*",
        "f": "json",
        "resultOffset": offset,
        "resultRecordCount": PAGE_SIZE,
        "orderByFields": "OBJECTID ASC",
        "returnGeometry": "false",
    }
    data = _get_json(url, params)
    if "error" in data:
        raise RuntimeError(f"API error: {data['error']}")
    return [feat.get("attributes", {}) for feat in data.get("features", [])]


def iter_all(query_url: str, where: str, limit: Optional[int]) -> Iterator[dict]:
    total = count_records(query_url, where)
    target = min(total, limit) if limit else total
    print(f"Pima: {total:,} parcels match — fetching {target:,}")
    fetched, offset = 0, 0
    while fetched < target:
        batch = fetch_page(query_url, where, offset)
        if not batch:
            break
        for row in batch:
            yield row
            fetched += 1
            if limit and fetched >= limit:
                return
        offset += PAGE_SIZE
        if offset % 20000 == 0:
            print(f"  progress: {fetched:,}/{target:,}")


# Pima field names tend to use TAXCODE, OWNER_NAME, SITUS_ADDR, etc.
FIELD_MAP = {
    "apn":             ["TAXCODE", "PARCEL", "APN", "PARCELNO"],
    "owner":           ["OWNER_NAME", "OWNER", "OWNERSHIP"],
    "owner_2":         ["OWNER2", "CO_OWNER"],
    "site_address":    ["SITUS_ADDR", "SITE_ADDR", "PHYSICAL_ADDR", "ADDRESS"],
    "site_city":       ["SITUS_CITY", "SITE_CITY", "CITY"],
    "site_zip":        ["SITUS_ZIP", "SITE_ZIP", "ZIP"],
    "mail_address":    ["MAIL_ADDR", "MAILING_ADDRESS", "OWNER_ADDR"],
    "mail_city":       ["MAIL_CITY", "OWNER_CITY"],
    "mail_state":      ["MAIL_STATE", "OWNER_STATE"],
    "mail_zip":        ["MAIL_ZIP", "OWNER_ZIP"],
    "subdivision":     ["SUBDIVISION", "SUB_NAME"],
    "use_code":        ["USE_CODE", "LAND_USE", "PROP_TYPE"],
    "use_desc":        ["USE_DESC", "USE_DESCRIPTION"],
    "year_built":      ["YEAR_BUILT", "YRBLT"],
    "living_sqft":     ["LIV_SQFT", "LIVING_SQFT", "SQFT"],
    "lot_sqft":        ["LOT_SQFT", "LOT_SIZE", "LAND_SIZE"],
    "bedrooms":        ["BEDROOMS", "BEDS"],
    "bathrooms":       ["BATHROOMS", "BATHS"],
    "fcv":             ["FCV", "FULL_CASH_VALUE"],
    "lpv":             ["LPV", "LIMITED_VALUE"],
    "last_sale_date":  ["SALE_DATE", "DEED_DATE"],
    "last_sale_price": ["SALE_PRICE", "DEED_PRICE"],
}

UNIFIED_FIELDS = list(FIELD_MAP.keys()) + ["county", "source_objectid",
                                           "absentee", "out_of_state", "apn_norm"]


def normalize(row: dict) -> dict:
    out = {"county": "Pima", "source_objectid": row.get("OBJECTID")}
    for dest, candidates in FIELD_MAP.items():
        val = None
        for cand in candidates:
            if cand in row and row[cand] not in (None, ""):
                val = row[cand]
                break
        out[dest] = val
    mail = (out.get("mail_state") or "").strip().upper()
    site_city = (out.get("site_city") or "").strip().upper()
    mail_city = (out.get("mail_city") or "").strip().upper()
    out["absentee"] = bool(mail_city and site_city and mail_city != site_city)
    out["out_of_state"] = bool(mail and mail != "AZ")
    out["apn_norm"] = (out.get("apn") or "").replace("-", "").upper()
    return out


def main():
    ap = argparse.ArgumentParser(description="Pima County parcel master scraper")
    ap.add_argument("--where", default="1=1")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--url", default=None, help="Override parcel layer base URL (no /query)")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    query_url, _ = pick_endpoint(args.url)

    count = 0
    with OUT_JSONL.open("w", encoding="utf-8") as jf, OUT_CSV.open("w", newline="", encoding="utf-8") as cf:
        writer = csv.DictWriter(cf, fieldnames=UNIFIED_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for raw in iter_all(query_url, args.where, args.limit):
            rec = normalize(raw)
            jf.write(json.dumps(rec, default=str) + "\n")
            writer.writerow(rec)
            count += 1

    print(f"\nDone. Wrote {count:,} records to:")
    print(f"  {OUT_JSONL}")
    print(f"  {OUT_CSV}")


if __name__ == "__main__":
    main()
