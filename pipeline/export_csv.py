"""
Export doc-type leads.json into Skip Trace + GHL CSVs.

Every lead is a recorded document. Unresolved leads (Maricopa docs we couldn't
pin to a parcel) are still exported — with their party name + document number —
so they remain skip-traceable.

Usage:
    python pipeline/export_csv.py
    python pipeline/export_csv.py --county Maricopa --doc-type "Notice of Trustee Sale"
    python pipeline/export_csv.py --resolved-only
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("export")


def split_owner(full: str | None) -> tuple[str, str]:
    if not full:
        return "", ""
    s = full.strip()
    if any(k in s.upper() for k in ("TRUST", "LLC", "INC", "LTD", "CORP")):
        return "", s
    if "," in s:
        last, rest = s.split(",", 1)
        parts = rest.strip().split()
        return (parts[0] if parts else ""), last.strip()
    parts = s.split()
    if len(parts) == 1:
        return "", parts[0]
    return " ".join(parts[:-1]), parts[-1]


def name_for(lead: dict) -> str:
    return lead.get("owner") or (lead.get("names") or [""])[0]


def export_skiptrace(leads: list[dict], out: Path) -> None:
    cols = ["Owner First", "Owner Last", "Mailing Address", "Mailing City",
            "Mailing State", "Mailing Zip", "Property Address", "Property City",
            "Property Zip", "APN", "County", "Doc Type", "Doc Number",
            "Recorded", "Resolved"]
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for l in leads:
            first, last = split_owner(name_for(l))
            w.writerow({
                "Owner First": first, "Owner Last": last,
                "Mailing Address": l.get("mail_address") or "",
                "Mailing City": l.get("mail_city") or "",
                "Mailing State": l.get("mail_state") or "",
                "Mailing Zip": l.get("mail_zip") or "",
                "Property Address": l.get("site_address") or "",
                "Property City": l.get("site_city") or "",
                "Property Zip": l.get("site_zip") or "",
                "APN": l.get("apn") or "",
                "County": l.get("county") or "",
                "Doc Type": l.get("doc_type") or "",
                "Doc Number": l.get("doc_number") or "",
                "Recorded": l.get("recorded_date") or "",
                "Resolved": "Y" if l.get("resolved") else "N",
            })
    log.info("skiptrace CSV -> %s (%d rows)", out, len(leads))


def export_ghl(leads: list[dict], out: Path) -> None:
    cols = ["First Name", "Last Name", "Address Line 1", "City", "State",
            "Postal Code", "Source", "Tags", "APN", "County", "Doc Type",
            "Doc Number", "Recorded"]
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for l in leads:
            first, last = split_owner(name_for(l))
            tags = [l.get("county"), l.get("doc_type"), *(l.get("annotations") or {}).keys()]
            w.writerow({
                "First Name": first, "Last Name": last,
                "Address Line 1": l.get("mail_address") or l.get("site_address") or "",
                "City": l.get("mail_city") or l.get("site_city") or "",
                "State": l.get("mail_state") or "AZ",
                "Postal Code": l.get("mail_zip") or l.get("site_zip") or "",
                "Source": "Maricopa/Pima Doc Intel",
                "Tags": ", ".join(t for t in tags if t),
                "APN": l.get("apn") or "",
                "County": l.get("county") or "",
                "Doc Type": l.get("doc_type") or "",
                "Doc Number": l.get("doc_number") or "",
                "Recorded": l.get("recorded_date") or "",
            })
    log.info("GHL CSV -> %s (%d rows)", out, len(leads))


def main() -> None:
    ap = argparse.ArgumentParser(description="Export doc-type leads to Skip Trace / GHL CSVs")
    ap.add_argument("--leads", default=str(DATA_DIR / "leads.json"))
    ap.add_argument("--format", choices=["skiptrace", "ghl", "both"], default="both")
    ap.add_argument("--county", choices=["Maricopa", "Pima"], default=None)
    ap.add_argument("--doc-type", default=None)
    ap.add_argument("--resolved-only", action="store_true")
    args = ap.parse_args()

    path = Path(args.leads)
    if not path.exists():
        raise SystemExit(f"leads file not found: {path} — run pipeline/build_docleads.py first")
    leads = json.loads(path.read_text()).get("leads", [])
    if args.county:
        leads = [l for l in leads if l.get("county") == args.county]
    if args.doc_type:
        leads = [l for l in leads if l.get("doc_type") == args.doc_type]
    if args.resolved_only:
        leads = [l for l in leads if l.get("resolved")]
    log.info("exporting %d leads (county=%s, doc_type=%s, resolved_only=%s)",
             len(leads), args.county, args.doc_type, args.resolved_only)

    if args.format in ("skiptrace", "both"):
        export_skiptrace(leads, DATA_DIR / "export_skiptrace.csv")
    if args.format in ("ghl", "both"):
        export_ghl(leads, DATA_DIR / "export_ghl.csv")


if __name__ == "__main__":
    main()
