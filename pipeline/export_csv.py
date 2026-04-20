"""
Export leads.json into the two formats used downstream:

    1. Skip Trace CSV — for BatchSkipTracing, REI Skip Trace Pro, etc.
       Columns: Owner First, Owner Last, Mailing Address, Mailing City,
                Mailing State, Mailing Zip, Property Address, Property City,
                Property State, Property Zip, APN

    2. GHL-ready CSV — for GoHighLevel bulk contact import.
       Columns: First Name, Last Name, Phone, Email, Address Line 1, City,
                State, Postal Code, Source, Tags, plus custom fields for
                APN, Signal Stack, Score, County.

Usage:
    python export_csv.py                    # both formats to data/
    python export_csv.py --format skiptrace
    python export_csv.py --min-score 80     # only export high-scoring leads
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("export")


def split_owner(full: Optional[str]) -> tuple[str, str]:
    """Very simple first/last split for CSV columns. Good enough for skip trace."""
    if not full:
        return "", ""
    s = full.strip()
    # Typical assessor format: "LAST FIRST" or "LAST, FIRST MI" or "LAST FIRST MI"
    if "," in s:
        last, rest = s.split(",", 1)
        parts = rest.strip().split()
        first = parts[0] if parts else ""
        return first, last.strip()
    # No comma — assume "LAST FIRST ..." or a trust/LLC name
    parts = s.split()
    if len(parts) == 1:
        return "", parts[0]
    # Heuristic: if the name contains TRUST / LLC / INC, treat whole thing as last
    if any(k in s.upper() for k in ("TRUST", " LLC", " INC", " LTD", " LP ", " LP.")):
        return "", s
    return parts[-1], " ".join(parts[:-1])


def signals_summary(lead: dict) -> str:
    return " + ".join(sorted({s.get("type", "") for s in lead.get("signals", []) if s.get("type")}))


def export_skiptrace(leads: list, out: Path):
    cols = [
        "Owner First", "Owner Last",
        "Mailing Address", "Mailing City", "Mailing State", "Mailing Zip",
        "Property Address", "Property City", "Property State", "Property Zip",
        "APN", "County",
    ]
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for lead in leads:
            first, last = split_owner(lead.get("owner"))
            w.writerow({
                "Owner First":     first,
                "Owner Last":      last,
                "Mailing Address": lead.get("mail_address") or "",
                "Mailing City":    lead.get("mail_city")    or "",
                "Mailing State":   lead.get("mail_state")   or "",
                "Mailing Zip":     lead.get("mail_zip")     or "",
                "Property Address":lead.get("site_address") or "",
                "Property City":   lead.get("site_city")    or "",
                "Property State":  "AZ",
                "Property Zip":    lead.get("site_zip")     or "",
                "APN":             lead.get("apn")          or "",
                "County":          lead.get("county")       or "",
            })
    log.info("skiptrace CSV -> %s  (%d rows)", out, len(leads))


def export_ghl(leads: list, out: Path):
    cols = [
        "First Name", "Last Name", "Phone", "Email",
        "Address Line 1", "City", "State", "Postal Code",
        "Source", "Tags",
        "APN", "County", "Signals", "Score", "Equity Est",
        "Property Address", "Property City", "Property Zip",
    ]
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for lead in leads:
            first, last = split_owner(lead.get("owner"))
            tags = [lead.get("county"), *sorted({s.get("type", "") for s in lead.get("signals", [])})]
            if lead.get("absentee"):     tags.append("Absentee")
            if lead.get("out_of_state"): tags.append("Out of State")
            w.writerow({
                "First Name":      first,
                "Last Name":       last,
                "Phone":           "",
                "Email":           "",
                "Address Line 1":  lead.get("mail_address") or lead.get("site_address") or "",
                "City":            lead.get("mail_city") or lead.get("site_city") or "",
                "State":           lead.get("mail_state") or "AZ",
                "Postal Code":     lead.get("mail_zip") or lead.get("site_zip") or "",
                "Source":          "Maricopa/Pima Intel",
                "Tags":            ", ".join(t for t in tags if t),
                "APN":             lead.get("apn") or "",
                "County":          lead.get("county") or "",
                "Signals":         signals_summary(lead),
                "Score":           lead.get("score", 0),
                "Equity Est":      lead.get("equity_est", 0),
                "Property Address":lead.get("site_address") or "",
                "Property City":   lead.get("site_city") or "",
                "Property Zip":    lead.get("site_zip") or "",
            })
    log.info("GHL CSV -> %s  (%d rows)", out, len(leads))


def main():
    ap = argparse.ArgumentParser(description="Export leads.json to Skip Trace / GHL CSVs")
    ap.add_argument("--leads", default=str(DATA_DIR / "leads.json"))
    ap.add_argument("--format", choices=["skiptrace", "ghl", "both"], default="both")
    ap.add_argument("--min-score", type=int, default=0)
    ap.add_argument("--county", choices=["Maricopa", "Pima", "both"], default="both")
    args = ap.parse_args()

    path = Path(args.leads)
    if not path.exists():
        raise SystemExit(f"leads file not found: {path} — run pipeline/build_leads.py first")

    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    leads = payload.get("leads", [])
    if args.county != "both":
        leads = [l for l in leads if l.get("county") == args.county]
    if args.min_score:
        leads = [l for l in leads if l.get("score", 0) >= args.min_score]

    log.info("exporting %d leads (county=%s, min_score=%d)", len(leads), args.county, args.min_score)

    if args.format in ("skiptrace", "both"):
        export_skiptrace(leads, DATA_DIR / "export_skiptrace.csv")
    if args.format in ("ghl", "both"):
        export_ghl(leads, DATA_DIR / "export_ghl.csv")


if __name__ == "__main__":
    main()
