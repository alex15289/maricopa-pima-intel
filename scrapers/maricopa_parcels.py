"""
Maricopa County parcel master scraper.

Source: https://gis.mcassessor.maricopa.gov/arcgis/rest/services/Parcels/MapServer/0
Pulls the full parcel roll in paginated batches and writes a normalized
CSV + JSONL of APN -> site address, mailing address, owner, and core
property characteristics. This is the JOIN TABLE that every motivated
seller signal resolves against.

Usage:
    python maricopa_parcels.py
    python maricopa_parcels.py --limit 5000          # test run
    python maricopa_parcels.py --where "CITY='PHOENIX'"  # filtered pull

Output:
    data/maricopa_parcels.jsonl  (one parcel per line)
    data/maricopa_parcels.csv    (spreadsheet friendly)
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

PARCELS_URL = (
    "https://gis.mcassessor.maricopa.gov/arcgis/rest/services/"
    "MaricopaDynamicQueryService/MapServer/0/query"
)
# Fallback: the leaner Parcels/MapServer/0 endpoint if the Dynamic one is down.
PARCELS_URL_FALLBACK = (
    "https://gis.mcassessor.maricopa.gov/arcgis/rest/services/"
    "Parcels/MapServer/0/query"
)

# Page size. Maricopa advertises MaxRecordCount: 1000 for these layers.
PAGE_SIZE = 1000
REQUEST_TIMEOUT = 60
RETRY_BACKOFF = [2, 5, 15, 45]  # seconds
USER_AGENT = "maricopa-pima-intel/1.0 (+real estate intel)"

OUT_DIR = Path(__file__).resolve().parent.parent / "data"
OUT_JSONL = OUT_DIR / "maricopa_parcels.jsonl"
OUT_CSV = OUT_DIR / "maricopa_parcels.csv"


def _get_json(url: str, params: dict) -> dict:
    """GET with retry + exponential backoff."""
    query = urlencode(params)
    full_url = f"{url}?{query}"
    last_err: Optional[Exception] = None
    for wait in [0] + RETRY_BACKOFF:
        if wait:
            time.sleep(wait)
        try:
            req = Request(full_url, headers={"User-Agent": USER_AGENT})
            with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                payload = resp.read().decode("utf-8")
                return json.loads(payload)
        except (HTTPError, URLError, json.JSONDecodeError, TimeoutError) as e:
            last_err = e
            print(f"  retry after error: {e}", file=sys.stderr)
    raise RuntimeError(f"failed to fetch {full_url}: {last_err}")


def count_records(url: str, where: str) -> int:
    """Returns total matching records for the where clause."""
    params = {"where": where, "returnCountOnly": "true", "f": "json"}
    data = _get_json(url, params)
    return int(data.get("count", 0))


def fetch_fields(url: str) -> list:
    """Returns the list of field names the layer exposes."""
    params = {"f": "json"}
    # Strip the trailing /query for the layer describe call
    describe_url = url[:-6] if url.endswith("/query") else url
    try:
        data = _get_json(describe_url, params)
        return [f["name"] for f in data.get("fields", [])]
    except Exception:
        return []


def fetch_page(url: str, where: str, offset: int, out_fields: str = "*") -> list:
    """Pulls one page of features."""
    params = {
        "where": where,
        "outFields": out_fields,
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


def iter_all_parcels(
    where: str = "1=1",
    limit: Optional[int] = None,
    url: str = PARCELS_URL,
) -> Iterator[dict]:
    """Generator: yields every parcel attribute row."""
    total = count_records(url, where)
    target = min(total, limit) if limit else total
    print(f"Maricopa: {total:,} parcels match — fetching {target:,}")
    fetched = 0
    offset = 0
    while fetched < target:
        try:
            batch = fetch_page(url, where, offset)
        except Exception as e:
            print(f"  page {offset} failed: {e} — trying fallback endpoint", file=sys.stderr)
            batch = fetch_page(PARCELS_URL_FALLBACK, where, offset)
        if not batch:
            break
        for row in batch:
            yield row
            fetched += 1
            if limit and fetched >= limit:
                return
        offset += PAGE_SIZE
        if offset % 10000 == 0:
            print(f"  progress: {fetched:,}/{target:,}")


# -----------------------------------------------------------------------------
# Normalization: map Maricopa's field names to a unified schema.
# -----------------------------------------------------------------------------

FIELD_MAP = {
    # destination: [candidate source field names — first match wins]
    "apn":             ["APN", "PARCEL", "PARCELNO", "PARCEL_NUM"],
    "owner":           ["OWNER_NAME", "OWNER", "OWNER_NM"],
    "owner_2":         ["OWNER_NAME2", "OWNER2"],
    "site_address":    ["SITE_ADDRESS", "SITUS_ADDR", "PHYSICAL_ADDR", "ADDRESS"],
    "site_city":       ["SITE_CITY", "CITY"],
    "site_zip":        ["SITE_ZIP", "ZIP", "ZIPCODE"],
    "mail_address":    ["OWNER_MAILING_ADDRESS", "MAIL_ADDR", "MAILING_ADDR"],
    "mail_city":       ["OWNER_MAILING_CITY", "MAIL_CITY"],
    "mail_state":      ["OWNER_MAILING_STATE", "MAIL_STATE"],
    "mail_zip":        ["OWNER_MAILING_ZIP", "MAIL_ZIP"],
    "subdivision":     ["SUBDIVISION", "SUB_NAME"],
    "use_code":        ["USE_CODE", "PROPERTY_USE", "PUC"],
    "use_desc":        ["USE_DESC", "USE_DESCRIPTION", "PUC_DESC"],
    "year_built":      ["YEAR_BUILT", "YRBLT"],
    "living_sqft":     ["LIVING_SQFT", "LIVABLE_SQFT", "SQFT"],
    "lot_sqft":        ["LOT_SIZE", "LOT_SQFT", "LAND_SIZE"],
    "bedrooms":        ["BEDROOMS", "BEDS"],
    "bathrooms":       ["BATHROOMS", "BATHS"],
    "fcv":             ["FULL_CASH_VALUE", "FCV", "TOTAL_FCV"],
    "lpv":             ["LIMITED_PROPERTY_VALUE", "LPV"],
    "last_sale_date":  ["LAST_SALE_DATE", "SALE_DATE", "DEED_DATE"],
    "last_sale_price": ["LAST_SALE_PRICE", "SALE_PRICE", "DEED_PRICE"],
}

UNIFIED_FIELDS = list(FIELD_MAP.keys()) + ["county", "source_objectid"]


def normalize(row: dict) -> dict:
    out = {"county": "Maricopa", "source_objectid": row.get("OBJECTID")}
    for dest, candidates in FIELD_MAP.items():
        val = None
        for cand in candidates:
            if cand in row and row[cand] not in (None, ""):
                val = row[cand]
                break
        out[dest] = val
    # Derived flags
    mail = (out.get("mail_state") or "").strip().upper()
    site_city = (out.get("site_city") or "").strip().upper()
    mail_city = (out.get("mail_city") or "").strip().upper()
    out["absentee"] = bool(mail_city and site_city and mail_city != site_city)
    out["out_of_state"] = bool(mail and mail != "AZ")
    # Normalized APN (strip dashes, uppercase)
    apn = (out.get("apn") or "").replace("-", "").upper()
    out["apn_norm"] = apn
    return out


def main():
    ap = argparse.ArgumentParser(description="Maricopa County parcel master scraper")
    ap.add_argument("--where", default="1=1", help="ArcGIS where clause")
    ap.add_argument("--limit", type=int, default=None, help="Stop after N records (testing)")
    ap.add_argument("--url", default=PARCELS_URL, help="Override endpoint URL")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Peek at the schema once so we know what we're getting.
    fields = fetch_fields(args.url)
    if fields:
        print(f"Fields exposed: {len(fields)} — sampling 10: {fields[:10]}")

    count = 0
    with OUT_JSONL.open("w", encoding="utf-8") as jf, OUT_CSV.open("w", newline="", encoding="utf-8") as cf:
        writer: Optional[csv.DictWriter] = None
        csv_fields = UNIFIED_FIELDS + ["absentee", "out_of_state", "apn_norm"]
        writer = csv.DictWriter(cf, fieldnames=csv_fields, extrasaction="ignore")
        writer.writeheader()
        for raw in iter_all_parcels(args.where, args.limit, args.url):
            rec = normalize(raw)
            jf.write(json.dumps(rec, default=str) + "\n")
            writer.writerow(rec)
            count += 1

    print(f"\nDone. Wrote {count:,} records to:")
    print(f"  {OUT_JSONL}")
    print(f"  {OUT_CSV}")


if __name__ == "__main__":
    main()
