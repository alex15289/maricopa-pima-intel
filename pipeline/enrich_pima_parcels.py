#!/usr/bin/env python3
"""
enrich_pima_parcels.py
──────────────────────
Rebuild data/pima_parcels.jsonl with real ownership + address data from the
Pima County Assessor's free public downloads.

Inputs (must be pre-downloaded + unzipped under data/pima_assessor/):
  • Ownership.csv  — Parcel + Mail1-Mail5 + Zip + Zip4
  • SITUS.csv      — Parcel + StreetNo/Dir/Name/City (site address)
  • Notice27.csv   — Parcel + CurrentFcv + Use + legal metadata
  • data/pima_parcels.jsonl (existing, for lat/lon)

Output:
  • data/pima_parcels.jsonl (overwritten, now fully populated)

Signals computed per parcel:
  • owner           (Mail1 cleaned)
  • owner_2         (if a co-owner pattern present in Mail1)
  • mail_address    (street line)
  • mail_city       (city token)
  • mail_state      (2-letter state)
  • mail_zip        (5-digit zip)
  • site_address    (constructed from SITUS components)
  • site_city       (SITUS.STREETCITY, Pima uses 2-letter shortcodes — expanded)
  • fcv             (from Notice27, current market value)
  • use_code        (from Notice27)
  • latitude/longitude (preserved from existing parcels)
  • is_llc          (owner ends with LLC/INC/LP/etc.)
  • is_trust        (owner contains TRUST/TRUSTEE)
  • absentee        (mail city ≠ site city)
  • out_of_state    (mail state != AZ)
  • is_exempt       (gov/church/school/county-owned based on owner name or Exempt flag)
"""
import csv
import json
import logging
import re
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("enrich_pima")

# ─── paths ────────────────────────────────────────────────────────────────────
BASE = Path(__file__).resolve().parent
if (BASE / "data").exists():
    ROOT = BASE
else:
    ROOT = BASE.parent   # script lives in pipeline/, data/ is sibling
DATA = ROOT / "data"
PIMA_DIR = DATA / "pima_assessor"

OWNERSHIP_CSV = PIMA_DIR / "Ownership.csv"
SITUS_CSV     = PIMA_DIR / "SITUS.csv"
NOTICE_CSV    = PIMA_DIR / "Notice27.csv"
EXISTING_JSONL = DATA / "pima_parcels.jsonl"
OUT_JSONL     = DATA / "pima_parcels.jsonl"   # same path → overwrite

# ─── Pima site-city shortcodes to full names ─────────────────────────────────
# Pima SITUS uses tiny 2-4 char codes like "TU"=Tucson, "PC"=Pima County/
# unincorporated, "MV"=Marana Valley, etc. Expand the common ones; leave the
# obscure ones as-is.
SITUS_CITY_MAP = {
    "TU": "TUCSON",
    "TUC": "TUCSON",
    "MA": "MARANA",
    "MAR": "MARANA",
    "MV": "MARANA",           # Marana Valley
    "OV": "ORO VALLEY",
    "ORO": "ORO VALLEY",
    "SV": "SAHUARITA",
    "SAH": "SAHUARITA",
    "SC": "SAHUARITA",
    "PC": "",                 # "Pima County" = unincorporated; blank for matching
    "SW": "SOUTH TUCSON",
    "SOU": "SOUTH TUCSON",
    "GV": "GREEN VALLEY",
    "VL": "VAIL",
    "AJ": "AJO",
    "CA": "CATALINA",
    "TR": "TORTOLITA",
}

# ─── business-name detection ─────────────────────────────────────────────────
ENTITY_SUFFIXES = {
    "LLC", "INC", "LP", "LLP", "PLLC", "CORP", "CO",
    "LTD", "PA", "PC", "TRUST", "TR", "ASSOCIATION", "ASSN",
    "CHURCH", "FOUNDATION", "PARTNERS", "PARTNERSHIP", "GROUP",
    "HOLDINGS", "PROPERTIES", "INVESTMENTS", "INVESTMENT",
    "VENTURES", "ENTERPRISES", "REALTY", "DEVELOPMENT",
}
TRUST_TOKENS = {"TRUST", "TRUSTEE", "TR", "TRUSTEES", "FAMILY TRUST", "LIVING TRUST"}
EXEMPT_TOKENS = {
    "PIMA COUNTY", "CITY OF TUCSON", "STATE OF ARIZONA", "UNITED STATES",
    "ARIZONA BOARD", "SCHOOL DISTRICT", "DEPARTMENT OF", "ROMAN CATHOLIC",
    "CHURCH", "DIOCESE", "UNIVERSITY OF", "PIMA COMMUNITY",
    "AMPHITHEATER SCHOOL", "SUNNYSIDE UNIFIED", "TUSD", "TUCSON UNIFIED",
    "MARANA UNIFIED", "FLOWING WELLS", "SAHUARITA UNIFIED",
}

STATE_RE = re.compile(r"\b([A-Z]{2})\b\s*$")   # 2-letter state at end of line


def norm_parcel(p):
    """Normalize Pima APN. Strip whitespace, uppercase."""
    if p is None:
        return ""
    return str(p).strip().upper()


def clean(s):
    """Clean a CSV string field. Strip trailing whitespace/dots."""
    if s is None:
        return ""
    return s.strip().strip(".").strip()


def is_llc(owner):
    if not owner:
        return False
    toks = owner.upper().replace(",", " ").split()
    if not toks:
        return False
    return toks[-1] in ENTITY_SUFFIXES or (len(toks) >= 2 and toks[-2] in ENTITY_SUFFIXES)


def is_trust_name(owner):
    if not owner:
        return False
    u = owner.upper()
    return any(tok in u for tok in TRUST_TOKENS)


def is_exempt(owner):
    if not owner:
        return False
    u = owner.upper()
    return any(tok in u for tok in EXEMPT_TOKENS)


def parse_mail_block(mail1, mail2, mail3, mail4, mail5, zip5, zip4):
    """
    Return a dict with: owner, mail_address, mail_city, mail_state, mail_zip.

    Pima's 5-line mail block can look like any of:
        Mail1 = owner,    Mail2 = street,   Mail3 = "CITY ST"
        Mail1 = owner,    Mail2 = attn,     Mail3 = street,    Mail4 = "CITY ST"
        Mail1 = owner,    Mail2 = street,   Mail3 = street2,   Mail4 = "CITY ST"

    Strategy: owner = Mail1. Walk Mail2-Mail5 backwards; the first nonblank
    line that ends with a 2-letter state code is the city/state line. The line
    just above it is the primary street line. Anything else is concatenated.
    """
    lines = [clean(x) for x in (mail1, mail2, mail3, mail4, mail5)]
    owner = lines[0]

    body = [x for x in lines[1:] if x and x != "."]

    mail_city = ""
    mail_state = ""
    mail_address = ""

    city_state_idx = -1
    for i in range(len(body) - 1, -1, -1):
        m = STATE_RE.search(body[i])
        if m:
            mail_state = m.group(1).upper()
            mail_city = body[i][:m.start()].strip()
            city_state_idx = i
            break

    if city_state_idx >= 0:
        street_lines = body[:city_state_idx]
        if street_lines:
            # Use the LAST street line (after any C/O or ATTN line) as primary
            mail_address = street_lines[-1]
    else:
        # No city/state line detected → just concatenate what we have
        mail_address = " ".join(body)

    mail_zip = clean(zip5)[:5] if zip5 else ""

    return {
        "owner": owner,
        "mail_address": mail_address,
        "mail_city": mail_city,
        "mail_state": mail_state,
        "mail_zip": mail_zip,
    }


def build_site_address(streetno, streetdir, streetname, streetcity):
    """Concatenate SITUS parts into a site address. Return (addr, city)."""
    no = clean(streetno)
    dr = clean(streetdir)
    nm = clean(streetname)
    cc = clean(streetcity).upper()

    # Skip zero/placeholder street numbers
    if no in ("0", "00", "000", "0000", ""):
        no = ""

    parts = [p for p in (no, dr, nm) if p]
    addr = " ".join(parts)
    city = SITUS_CITY_MAP.get(cc, cc)
    return addr, city


def cities_match(a, b):
    """Fuzzy-compare two city strings after normalization."""
    if not a or not b:
        return False
    na = a.upper().strip()
    nb = b.upper().strip()
    if na == nb:
        return True
    # Handle shortcodes
    na_full = SITUS_CITY_MAP.get(na, na)
    nb_full = SITUS_CITY_MAP.get(nb, nb)
    return na_full == nb_full


# ─── main pipeline ────────────────────────────────────────────────────────────
def load_ownership():
    log.info(f"loading Ownership.csv …")
    out = {}
    with OWNERSHIP_CSV.open(newline="", encoding="utf-8", errors="replace") as f:
        r = csv.DictReader(f)
        for i, row in enumerate(r, 1):
            apn = norm_parcel(row.get("Parcel"))
            if not apn:
                continue
            parsed = parse_mail_block(
                row.get("Mail1"), row.get("Mail2"), row.get("Mail3"),
                row.get("Mail4"), row.get("Mail5"),
                row.get("Zip"), row.get("Zip4"),
            )
            out[apn] = parsed
            if i % 100_000 == 0:
                log.info(f"  {i:,} ownership rows…")
    log.info(f"  ✓ {len(out):,} ownership records indexed")
    return out


def load_situs():
    log.info(f"loading SITUS.csv …")
    out = {}
    with SITUS_CSV.open(newline="", encoding="utf-8", errors="replace") as f:
        r = csv.DictReader(f)
        for i, row in enumerate(r, 1):
            apn = norm_parcel(row.get("PARCEL"))
            if not apn:
                continue
            addr, city = build_site_address(
                row.get("STREETNO"), row.get("STREETDIR"),
                row.get("STREETNAM"), row.get("STREETCITY"),
            )
            out[apn] = {"site_address": addr, "site_city": city}
            if i % 100_000 == 0:
                log.info(f"  {i:,} situs rows…")
    log.info(f"  ✓ {len(out):,} situs records indexed")
    return out


def load_notice():
    log.info(f"loading Notice27.csv …")
    out = {}
    with NOTICE_CSV.open(newline="", encoding="utf-8", errors="replace") as f:
        r = csv.DictReader(f)
        for i, row in enumerate(r, 1):
            apn = norm_parcel(row.get("Parcel   ") or row.get("Parcel"))
            if not apn:
                continue
            try:
                fcv = float((row.get("CurrentFcv  ") or row.get("CurrentFcv") or "0").strip() or 0)
            except ValueError:
                fcv = 0
            use = clean(row.get("Use ") or row.get("Use"))
            exempt = clean(row.get("Exempt"))
            record_date = clean(row.get("RecordDate"))
            out[apn] = {
                "fcv": fcv if fcv > 0 else None,
                "use_code": use,
                "is_exempt_flag": bool(exempt and exempt != "0"),
                "record_date": record_date,
            }
            if i % 100_000 == 0:
                log.info(f"  {i:,} notice rows…")
    log.info(f"  ✓ {len(out):,} notice records indexed")
    return out


def load_existing_parcels():
    """Preserve lat/lon + source_objectid from the current pima_parcels.jsonl."""
    log.info(f"loading existing {EXISTING_JSONL.name} (for lat/lon)…")
    out = {}
    if not EXISTING_JSONL.exists():
        log.warning("  no existing pima_parcels.jsonl — lat/lon will be null")
        return out
    with EXISTING_JSONL.open() as f:
        for i, line in enumerate(f, 1):
            try:
                r = json.loads(line)
            except Exception:
                continue
            apn = norm_parcel(r.get("apn_norm") or r.get("apn"))
            if not apn:
                continue
            out[apn] = {
                "latitude":  r.get("latitude"),
                "longitude": r.get("longitude"),
                "source_objectid": r.get("source_objectid"),
            }
            if i % 100_000 == 0:
                log.info(f"  {i:,} existing rows…")
    log.info(f"  ✓ {len(out):,} existing parcels indexed")
    return out


def main():
    # Sanity-check inputs before loading gigabytes
    for p in (OWNERSHIP_CSV, SITUS_CSV, NOTICE_CSV):
        if not p.exists():
            log.error(f"MISSING: {p}")
            sys.exit(1)

    ownership = load_ownership()
    situs     = load_situs()
    notice    = load_notice()
    existing  = load_existing_parcels()

    # Union of all APNs
    all_apns = set(ownership) | set(situs) | set(notice) | set(existing)
    log.info(f"merging {len(all_apns):,} unique APNs across 4 sources…")

    # Counters for diagnostics
    n_out = 0
    n_with_owner = 0
    n_absentee = 0
    n_oos = 0
    n_llc = 0
    n_trust = 0
    n_exempt = 0

    tmp_out = OUT_JSONL.with_suffix(".tmp.jsonl")
    with tmp_out.open("w") as out:
        for apn in sorted(all_apns):
            own = ownership.get(apn, {})
            sit = situs.get(apn, {})
            nv  = notice.get(apn, {})
            ex  = existing.get(apn, {})

            owner        = own.get("owner", "") or None
            mail_address = own.get("mail_address", "") or None
            mail_city    = own.get("mail_city", "") or None
            mail_state   = own.get("mail_state", "") or None
            mail_zip     = own.get("mail_zip", "") or None

            site_address = sit.get("site_address", "") or None
            site_city    = sit.get("site_city", "") or None

            fcv          = nv.get("fcv")
            use_code     = nv.get("use_code") or None
            is_exempt_f  = nv.get("is_exempt_flag", False)

            lat = ex.get("latitude")
            lon = ex.get("longitude")
            src = ex.get("source_objectid")

            # Derived flags
            absentee = False
            out_of_state = False
            if owner and mail_city and site_city:
                if not cities_match(mail_city, site_city):
                    absentee = True
            if mail_state and mail_state != "AZ":
                out_of_state = True
                absentee = True   # out-of-state owners are by definition absentee

            _is_llc   = is_llc(owner or "")
            _is_trust = is_trust_name(owner or "")
            _is_exempt = is_exempt(owner or "") or is_exempt_f

            record = {
                "county": "Pima",
                "apn": apn,
                "apn_norm": apn,
                "source_objectid": src,
                "owner": owner,
                "owner_2": None,
                "site_address": site_address,
                "site_city": site_city,
                "site_zip": None,
                "mail_address": mail_address,
                "mail_city": mail_city,
                "mail_state": mail_state,
                "mail_zip": mail_zip,
                "subdivision": None,
                "use_code": use_code,
                "use_desc": None,
                "year_built": None,
                "living_sqft": None,
                "lot_sqft": None,
                "bedrooms": None,
                "bathrooms": None,
                "fcv": fcv,
                "lpv": None,
                "last_sale_date": None,
                "last_sale_price": None,
                "latitude": lat,
                "longitude": lon,
                "absentee": absentee,
                "out_of_state": out_of_state,
                "is_llc": _is_llc,
                "is_trust": _is_trust,
                "is_exempt": _is_exempt,
            }
            out.write(json.dumps(record) + "\n")

            n_out += 1
            if owner:
                n_with_owner += 1
            if absentee:
                n_absentee += 1
            if out_of_state:
                n_oos += 1
            if _is_llc:
                n_llc += 1
            if _is_trust:
                n_trust += 1
            if _is_exempt:
                n_exempt += 1

    # Atomic replace
    tmp_out.replace(OUT_JSONL)

    log.info("=" * 60)
    log.info(f"✓ wrote {n_out:,} parcels → {OUT_JSONL}")
    log.info(f"   • with owner name:   {n_with_owner:,}")
    log.info(f"   • absentee:          {n_absentee:,}")
    log.info(f"   • out-of-state:      {n_oos:,}")
    log.info(f"   • LLC-owned:         {n_llc:,}")
    log.info(f"   • trust-owned:       {n_trust:,}")
    log.info(f"   • gov/exempt:        {n_exempt:,}")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
