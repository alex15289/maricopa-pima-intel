#!/usr/bin/env python3
"""Collect active City of Tucson code-enforcement cases from the official ArcGIS layer.

The City of Tucson is within Pima County. This source does not cover unincorporated
Pima County, whose official Accela portal has no documented bulk API.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
OUT_PATH = DATA_DIR / "tucson_code_violation_docs.jsonl"
QUERY_URL = "https://mapdata.tucsonaz.gov/arcgis/rest/services/PublicMaps/PermitsCode/MapServer/102/query"
PAGE_SIZE = 2000
OUT_FIELDS = ",".join([
    "OBJECTID", "CASENUMBER", "status", "OPENEDDATE", "CLOSEDDATE",
    "LASTCHANGEDON", "DESCRIPTION", "type", "PREFIX", "PRO_URL",
    "MainAddress", "UNITORSUITE", "CITY", "POSTALCODE", "District",
    "DATASOURCE", "PARCEL", "NHA", "LAT", "LON", "casetype",
])
USER_AGENT = "EmpireHousing-PimaCodeCases/1.0 (official public-data collector)"


def is_open_case(status: object) -> bool:
    value = str(status or "").strip().casefold()
    return bool(value) and not value.startswith("closed")


def _date_from_epoch_ms(value: object) -> str | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _normalize_apn(value: object) -> str | None:
    normalized = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    return normalized if re.fullmatch(r"[A-Z0-9]{9}", normalized) else None


def feature_to_record(feature: dict, *, fetched_at: str) -> dict:
    attrs = feature.get("attributes") or {}
    case_number = str(attrs.get("CASENUMBER") or "").strip()
    if not case_number:
        raise ValueError("feature missing CASENUMBER")
    opened = _date_from_epoch_ms(attrs.get("OPENEDDATE"))
    if not opened:
        raise ValueError(f"case {case_number} missing valid OPENEDDATE")
    apn = _normalize_apn(attrs.get("PARCEL"))
    status = str(attrs.get("status") or "").strip()
    return {
        "county": "Pima",
        "jurisdiction": "City of Tucson",
        "source": "city_of_tucson_code_enforcement_arcgis",
        "source_url": attrs.get("PRO_URL") or QUERY_URL,
        "doc_code": "CODE",
        "doc_type": "Code Violation",
        "category": "Code Violations",
        "doc_number": case_number,
        "recorded_date": opened,
        "case_status": status,
        "active_case": is_open_case(status),
        "closed_date": _date_from_epoch_ms(attrs.get("CLOSEDDATE")),
        "last_changed_date": _date_from_epoch_ms(attrs.get("LASTCHANGEDON")),
        "description": attrs.get("DESCRIPTION"),
        "case_type": attrs.get("casetype"),
        "site_address": attrs.get("MainAddress"),
        "site_city": attrs.get("CITY") or "Tucson",
        "site_zip": str(attrs.get("POSTALCODE") or "").strip() or None,
        "unit": attrs.get("UNITORSUITE"),
        "district": attrs.get("District"),
        "apn": attrs.get("PARCEL"),
        "apn_norm": apn,
        "resolved": bool(apn),
        "latitude": attrs.get("LAT"),
        "longitude": attrs.get("LON"),
        "object_id": attrs.get("OBJECTID"),
        "fetched_at": fetched_at,
    }


def fetch_active_cases(*, timeout: float = 30.0, pace: float = 0.25) -> list[dict]:
    session = requests.Session()
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    records: dict[str, dict] = {}
    offset = 0
    while True:
        params = {
            "where": "1=1",
            "outFields": OUT_FIELDS,
            "returnGeometry": "false",
            "orderByFields": "OBJECTID ASC",
            "resultOffset": offset,
            "resultRecordCount": PAGE_SIZE,
            "f": "json",
        }
        response = session.get(
            QUERY_URL, params=params,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise RuntimeError(f"ArcGIS error: {payload['error']}")
        features = payload.get("features")
        if not isinstance(features, list):
            raise RuntimeError("ArcGIS response missing features list")
        for feature in features:
            record = feature_to_record(feature, fetched_at=fetched_at)
            if record["active_case"]:
                records[record["doc_number"]] = record
        print(f"offset {offset:,}: {len(features):,} rows; {len(records):,} active kept", flush=True)
        if len(features) < PAGE_SIZE:
            break
        offset += len(features)
        time.sleep(max(0.0, pace))
    return sorted(records.values(), key=lambda row: (row["recorded_date"], row["doc_number"]), reverse=True)


def atomic_write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temp_name = handle.name
            for record in records:
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        temp_name = None
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUT_PATH)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    records = fetch_active_cases(timeout=args.timeout)
    atomic_write_jsonl(args.output, records)
    print(f"done: {len(records):,} active Tucson code cases -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
