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
    "Parcels/MapServer/0/query"
)

# Page size. Maricopa advertises MaxRecordCount: 1000 for these layers.
PAGE_SIZE = 1000
REQUEST_TIMEOUT = 60
RETRY_BACKOFF = [2, 5, 15, 45, 90, 180, 300]  # seconds — rides out multi-minute network blips
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
    start_offset: int = 0,
) -> Iterator[dict]:
    """Generator: yields every parcel attribute row (from start_offset on)."""
    total = count_records(url, where)
    target = min(total, limit) if limit else total
    print(f"Maricopa: {total:,} parcels match — fetching {target:,}"
          + (f" (resuming at {start_offset:,})" if start_offset else ""))
    fetched = start_offset
    offset = start_offset
    while fetched < target:
        try:
            batch = fetch_page(url, where, offset)
        except Exception as e:
            print(f"  page {offset} failed: {e} — one more attempt", file=sys.stderr)
            batch = fetch_page(url, where, offset)
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
    "apn":             ["APN", "APN_DASH"],
    "owner":           ["OWNER_NAME"],
    "owner_2":         ["INCAREOF"],
    "site_address":    ["PHYSICAL_ADDRESS"],
    "site_city":       ["PHYSICAL_CITY"],
    "site_zip":        ["PHYSICAL_ZIP"],
    "mail_address":    ["MAIL_ADDRESS", "MAIL_ADDR1"],
    "mail_city":       ["MAIL_CITY"],
    "mail_state":      ["MAIL_STATE"],
    "mail_zip":        ["MAIL_ZIP"],
    "subdivision":     ["SUBNAME"],
    "use_code":        ["PUC"],
    "use_desc":        ["LC_CUR"],
    "year_built":      ["CONST_YEAR"],
    "living_sqft":     ["LIVING_SPACE"],
    "lot_sqft":        ["LAND_SIZE"],
    "bedrooms":        [],
    "bathrooms":       [],
    "fcv":             ["FCV_CUR"],
    "lpv":             ["LPV_CUR"],
    "last_sale_date":  ["SALE_DATE", "DEED_DATE"],
    "last_sale_price": ["SALE_PRICE"],
    # Recording number of the deed that currently vests the parcel. Lets recorder
    # deed documents resolve to a parcel by exact identifier (doc# -> APN) instead
    # of fuzzy name matching. Assessor lags ~4 weeks processing a new deed in here.
    "deed_number":     ["DEED_NUMBER"],
    "latitude":        ["LATITUDE"],
    "longitude":       ["LONGITUDE"],
    "jurisdiction":    ["JURISDICTION"],
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
    ap.add_argument("--resume", action="store_true",
                    help="Continue an interrupted pull from the last full page in the jsonl")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Peek at the schema once so we know what we're getting.
    fields = fetch_fields(args.url)
    if fields:
        print(f"Fields exposed: {len(fields)} — sampling 10: {fields[:10]}")

    # Resume: keep whole pages already on disk, drop any partial trailing page
    # (page-aligned offsets), and append from there.
    start_offset = 0
    mode = "w"
    if args.resume and OUT_JSONL.exists():
        with OUT_JSONL.open("rb") as f:
            existing = sum(1 for _ in f)
        start_offset = (existing // PAGE_SIZE) * PAGE_SIZE
        if existing != start_offset:
            pos = 0
            with OUT_JSONL.open("rb") as f:
                for _ in range(start_offset):
                    pos += len(f.readline())
            with OUT_JSONL.open("rb+") as f:
                f.truncate(pos)
        mode = "a"
        print(f"Resuming: {existing:,} rows on disk -> continuing at offset {start_offset:,}")

    count = start_offset
    with OUT_JSONL.open(mode, encoding="utf-8") as jf:
        for raw in iter_all_parcels(args.where, args.limit, args.url, start_offset):
            rec = normalize(raw)
            jf.write(json.dumps(rec, default=str) + "\n")
            count += 1

    # CSV mirrors the jsonl; rebuilding it wholesale is resume-safe and cheap
    csv_fields = UNIFIED_FIELDS + ["absentee", "out_of_state", "apn_norm"]
    with OUT_JSONL.open(encoding="utf-8") as jf, \
         OUT_CSV.open("w", newline="", encoding="utf-8") as cf:
        writer = csv.DictWriter(cf, fieldnames=csv_fields, extrasaction="ignore")
        writer.writeheader()
        for line in jf:
            try:
                writer.writerow(json.loads(line))
            except json.JSONDecodeError:
                print("  skipping corrupt jsonl line in CSV rebuild", file=sys.stderr)

    print(f"\nDone. Wrote {count:,} records to:")
    print(f"  {OUT_JSONL}")
    print(f"  {OUT_CSV}")


if __name__ == "__main__":
    main()
