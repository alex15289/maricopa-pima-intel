"""
Pima County parcel master scraper — v2, with dynamic OID field discovery.

v1 hardcoded OBJECTID, which fails on Pima's FeatureServer because the OID
field is named differently. v2 reads the layer describe JSON first, finds the
actual OID field, and uses that for ordering. Also probes all candidate
endpoints and picks the one with the most records.

Usage:
    python pima_parcels.py
    python pima_parcels.py --url <override>
    python pima_parcels.py --limit 5000
"""

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path
from typing import Iterator, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

CANDIDATE_URLS = [
    # LandRecords layer 12 "Parcels - Regional" is the attribute-rich layer:
    # PARCEL + MAIL1-5 owner/mailing block + ADDRESS_OL + PARCEL_USE + FCV.
    # Layers 0/1/2 are geometry-only (centroids/access/lines) — never usable.
    "https://gisdata.pima.gov/arcgis1/rest/services/GISOpenData/LandRecords/MapServer/12",
    "https://gisdata.pima.gov/arcgis1/rest/services/GISOpenData/LandRecords/MapServer/13",
    "https://services1.arcgis.com/Ezk9fcjSUkeadg6u/arcgis/rest/services/Parcels_Regional/FeatureServer/0",
    "https://services1.arcgis.com/Ezk9fcjSUkeadg6u/arcgis/rest/services/ParcelsRegional/FeatureServer/0",
]

PAGE_SIZE = 2000
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
    try:
        info = _get_json(base_url, {"f": "json"})
        if "error" in info or info.get("type") != "Feature Layer":
            return None
        count_data = _get_json(f"{base_url}/query", {
            "where": "1=1", "returnCountOnly": "true", "f": "json"
        })
        count = int(count_data.get("count", 0))
        oid_field = info.get("objectIdField")
        if not oid_field:
            for f in info.get("fields", []):
                if f.get("type") == "esriFieldTypeOID":
                    oid_field = f["name"]
                    break
        return {
            "url":    base_url,
            "name":   info.get("name", "?"),
            "fields": [f["name"] for f in info.get("fields", [])],
            "oid":    oid_field,
            "count":  count,
        }
    except Exception as e:
        print(f"  probe failed: {e}", file=sys.stderr)
        return None


def pick_best_endpoint(override: Optional[str]) -> dict:
    if override:
        info = probe_endpoint(override)
        if not info:
            raise RuntimeError(f"Override URL didn't respond: {override}")
        return info

    print("Probing Pima endpoints...")
    candidates = []
    for url in CANDIDATE_URLS:
        short = "/".join(url.split("/")[-3:])
        info = probe_endpoint(url)
        if info:
            print(f"  ✓ {info['name']}: {info['count']:,} records, OID={info['oid']}")
            candidates.append(info)
        else:
            print(f"  ✗ {short}")

    # A usable layer must carry ownership + parcel-id attributes. Record count
    # alone is a trap: the geometry layers (parcel LINES) have 3x the records
    # of the real parcel roll and zero assessor data.
    owner_fields = set(FIELD_MAP["owner"]) | {"MAIL1"}
    apn_fields = set(FIELD_MAP["apn"])
    usable = [c for c in candidates
              if owner_fields & set(c["fields"]) and apn_fields & set(c["fields"])]

    if not usable:
        raise RuntimeError(
            "No Pima endpoint with ownership attributes (owner/MAIL1 + parcel id). "
            "Refusing to pull a geometry-only layer. Go to https://gisopendata.pima.gov, "
            "find the Parcels - Regional dataset, click 'View Data Source', and "
            "pass that URL with --url."
        )

    best = max(usable, key=lambda x: x["count"])
    print(f"\nUsing: {best['name']} ({best['count']:,} records)")
    return best


def fetch_page(url: str, offset: int, oid_field: str) -> list:
    params = {
        "where": "1=1",
        "outFields": "*",
        "f": "json",
        "resultOffset": offset,
        "resultRecordCount": PAGE_SIZE,
        "orderByFields": f"{oid_field} ASC",
        "returnGeometry": "false",
    }
    data = _get_json(f"{url}/query", params)
    if "error" in data:
        raise RuntimeError(f"API error: {data['error']}")
    return [feat.get("attributes", {}) for feat in data.get("features", [])]


def iter_all(endpoint: dict, limit: Optional[int]) -> Iterator[dict]:
    total = endpoint["count"]
    target = min(total, limit) if limit else total
    print(f"Pima: fetching {target:,} parcels")
    fetched, offset = 0, 0
    while fetched < target:
        batch = fetch_page(endpoint["url"], offset, endpoint["oid"])
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


FIELD_MAP = {
    "apn":             ["TAXCODE", "PARCEL", "PARCELNUM", "APN", "PARCELNO", "PARCEL_NUM"],
    "owner":           ["OWNER_NAME", "OWNER", "OWNERSHIP", "OWNER1", "TAXPAYER"],
    "owner_2":         ["OWNER2", "CO_OWNER", "OWNER_NAME2"],
    "site_address":    ["SITUS_ADDR", "SITUS_ADDRESS", "SITE_ADDR", "PHYSICAL_ADDR", "ADDRESS", "PROPERTY_ADDR"],
    "site_city":       ["SITUS_CITY", "SITE_CITY", "CITY"],
    "site_zip":        ["SITUS_ZIP", "SITE_ZIP", "ZIP", "ZIPCODE"],
    "mail_address":    ["MAIL_ADDR", "MAILING_ADDRESS", "OWNER_ADDR", "MAIL_ADDRESS", "TAXPAYER_ADDR"],
    "mail_city":       ["MAIL_CITY", "OWNER_CITY", "TAXPAYER_CITY"],
    "mail_state":      ["MAIL_STATE", "OWNER_STATE", "TAXPAYER_STATE"],
    "mail_zip":        ["MAIL_ZIP", "OWNER_ZIP", "TAXPAYER_ZIP"],
    "subdivision":     ["SUBDIVISION", "SUB_NAME", "SUBNAME"],
    "use_code":        ["USE_CODE", "LAND_USE", "PROP_TYPE", "PARCEL_USE", "PUC"],
    "use_desc":        ["USE_DESC", "USE_DESCRIPTION", "LANDCLASS", "IMPCLASS"],
    "year_built":      ["YEAR_BUILT", "YRBLT", "YEAR_BLT"],
    "living_sqft":     ["LIV_SQFT", "LIVING_SQFT", "SQFT", "LIVING_AREA"],
    "lot_sqft":        ["LOT_SQFT", "LOT_SIZE", "LAND_SIZE", "ACREAGE"],
    "bedrooms":        ["BEDROOMS", "BEDS"],
    "bathrooms":       ["BATHROOMS", "BATHS"],
    "fcv":             ["FCV", "FULL_CASH_VALUE", "FULLCASH"],
    "lpv":             ["LPV", "LIMITED_VALUE", "LIMITED_PROPERTY_VALUE"],
    "last_sale_date":  ["SALE_DATE", "DEED_DATE", "LAST_SALE"],
    "last_sale_price": ["SALE_PRICE", "DEED_PRICE", "LAST_SALE_AMT"],
    "latitude":        ["LATITUDE", "LAT"],
    "longitude":       ["LONGITUDE", "LON", "LONG"],
}

UNIFIED_FIELDS = list(FIELD_MAP.keys()) + ["county", "source_objectid",
                                           "absentee", "out_of_state", "apn_norm"]


STATE_RE = re.compile(r"\b([A-Z]{2})\b\s*$")   # 2-letter state at end of line


def _clean(s) -> str:
    if s is None:
        return ""
    return str(s).strip().strip(".").strip()


def parse_mail_block(row: dict) -> dict:
    """
    LandRecords layer 12 ships owner + mailing as a 5-line mail block:
    MAIL1 = owner name; MAIL2-5 = street/attn lines ending in a "CITY ST"
    line. Walk backwards for the city/state line; the line above it is the
    street. (Ported from pipeline/enrich_pima_parcels.py.)
    """
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
    if idx >= 0:
        street_lines = body[:idx]
        if street_lines:
            mail_address = street_lines[-1]
    else:
        mail_address = " ".join(body)
    return {"owner": owner, "mail_address": mail_address,
            "mail_city": mail_city, "mail_state": mail_state}


def normalize(row: dict, oid_field: str) -> dict:
    out = {"county": "Pima", "source_objectid": row.get(oid_field)}
    for dest, candidates in FIELD_MAP.items():
        val = None
        for cand in candidates:
            if cand in row and row[cand] not in (None, ""):
                val = row[cand]
                break
        out[dest] = val

    # LandRecords layer 12: owner/mailing live in the MAIL1-5 block, ZIP is
    # the MAILING zip (not situs), site address is ADDRESS_OL / JURIS_OL.
    if row.get("MAIL1"):
        mb = parse_mail_block(row)
        out["owner"] = out.get("owner") or mb["owner"] or None
        out["mail_address"] = out.get("mail_address") or mb["mail_address"] or None
        out["mail_city"] = out.get("mail_city") or mb["mail_city"] or None
        out["mail_state"] = out.get("mail_state") or mb["mail_state"] or None
        out["mail_zip"] = out.get("mail_zip") or (_clean(row.get("ZIP"))[:5] or None)
        # generic FIELD_MAP wrongly grabs the mailing ZIP as site_zip here
        if row.get("ZIP") and out.get("site_zip") == row.get("ZIP"):
            out["site_zip"] = None
        if not out.get("site_address"):
            out["site_address"] = _clean(row.get("ADDRESS_OL")) or None
        juris = _clean(row.get("JURIS_OL")).upper()
        # Unincorporated county is not a city — leaving site_city blank keeps
        # the absentee check (mail city != site city) from false-firing
        if not out.get("site_city") and juris and "UNINCORPORATED" not in juris and juris != "PIMA COUNTY":
            out["site_city"] = juris
        # PARCEL is the 9-char taxcode; display dashed book-map-parcel
        apn_raw = _clean(out.get("apn"))
        if apn_raw and "-" not in apn_raw and len(apn_raw) >= 8:
            out["apn"] = f"{apn_raw[:3]}-{apn_raw[3:5]}-{apn_raw[5:]}"

    mail = (out.get("mail_state") or "").strip().upper()
    site_city = (out.get("site_city") or "").strip().upper()
    mail_city = (out.get("mail_city") or "").strip().upper()
    out["absentee"] = bool(mail_city and site_city and mail_city != site_city)
    out["out_of_state"] = bool(mail and mail != "AZ")
    out["apn_norm"] = (out.get("apn") or "").replace("-", "").upper()
    return out


def main():
    ap = argparse.ArgumentParser(description="Pima County parcel master scraper v2")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--url", default=None)
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    endpoint = pick_best_endpoint(args.url)

    count = 0
    with OUT_JSONL.open("w", encoding="utf-8") as jf, OUT_CSV.open("w", newline="", encoding="utf-8") as cf:
        writer = csv.DictWriter(cf, fieldnames=UNIFIED_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for raw in iter_all(endpoint, args.limit):
            rec = normalize(raw, endpoint["oid"])
            jf.write(json.dumps(rec, default=str) + "\n")
            writer.writerow(rec)
            count += 1

    print(f"\nDone. Wrote {count:,} records to:")
    print(f"  {OUT_JSONL}")
    print(f"  {OUT_CSV}")


if __name__ == "__main__":
    main()
