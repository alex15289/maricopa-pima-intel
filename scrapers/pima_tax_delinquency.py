#!/usr/bin/env python3
"""Verify resolved Pima Recorder tax-lien APNs against the official Treasurer page.

This is a fail-closed verifier, not a delinquency inference. Recorder liens only
select candidate APNs. A lead is emitted only when Pima County's current account
balance table validates and contains at least three distinct unpaid tax years.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import time
from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable

import requests

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
PROPERTY_INQUIRY_URL = "https://www.to.pima.gov/propertyInquiry/"
SUPPORTED_LIEN_TYPES = {"Federal Tax Lien", "State Tax Lien", "City Lien"}
BALANCE_HEADERS = [
    "PAY", "TAX YEAR", "CERT NO", "INTEREST DATE", "INTEREST PERCENT",
    "AMOUNT", "INTEREST", "FEES", "PENALTIES", "TOTAL DUE",
]
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
    "PimaTaxVerifier/1.0"
)
MAX_ATTEMPTS = 3
RISK_BASIS = (
    "County-verified 3+ distinct currently unpaid tax years; "
    "certificate/redemption/legal-stage verification required."
)


def normalize_apn(value: object) -> str | None:
    """Return the Treasurer's nine-character state code, or reject the value."""
    if value is None:
        return None
    raw = str(value).strip().upper()
    # Only ASCII letters/digits are allowed. Separators are limited to common
    # presentation punctuation; arbitrary punctuation must not be laundered.
    if not raw or re.search(r"[^A-Z0-9\s-]", raw):
        return None
    normalized = re.sub(r"[\s-]", "", raw)
    return normalized if re.fullmatch(r"[A-Z0-9]{9}", normalized) else None


def _evidence_summary(record: dict) -> dict:
    return {
        "doc_type": record.get("doc_type"),
        "doc_number": record.get("doc_number"),
        "recorded_date": record.get("recorded_date"),
        "source": record.get("source"),
        "names": list(record.get("names") or []),
        "resolved_by": record.get("resolved_by"),
        "match_tier": record.get("match_tier"),
    }


def _external_evidence_summary(record: dict) -> dict:
    """Keep external screening signals separate from Recorder document evidence."""
    return {
        "source": record.get("source"),
        "signal_type": record.get("signal_type"),
        "source_record_id": record.get("source_record_id"),
        "property_address": record.get("property_address"),
        "tax_delinquent_year": record.get("tax_delinquent_year"),
    }


def extract_candidates(payload: dict, *, external_candidates: list[dict] | None = None) -> dict:
    """Group supported Recorder liens and optional external tax signals by APN."""
    if not isinstance(payload, dict) or not isinstance(payload.get("leads"), list):
        raise ValueError("leads input must be an object containing a leads list")

    grouped: dict[str, list[dict]] = {}
    excluded: list[dict] = []
    reasons: Counter[str] = Counter()
    for index, record in enumerate(payload["leads"]):
        if not isinstance(record, dict):
            continue
        if record.get("county") != "Pima" or record.get("doc_type") not in SUPPORTED_LIEN_TYPES:
            continue
        apn: str | None = None
        if record.get("resolved") is not True:
            reason = "unresolved"
        else:
            apn = normalize_apn(record.get("apn_norm") or record.get("apn"))
            reason = "" if apn else "invalid_apn"
        if reason:
            reasons[reason] += 1
            excluded.append({
                "input_index": index,
                "reason": reason,
                "doc_type": record.get("doc_type"),
                "doc_number": record.get("doc_number"),
                "apn": record.get("apn"),
                "apn_norm": record.get("apn_norm"),
            })
            continue
        assert apn is not None
        grouped.setdefault(apn, []).append(_evidence_summary(record))

    external_grouped: dict[str, list[dict]] = {}
    if external_candidates is not None:
        if not isinstance(external_candidates, list):
            raise ValueError("external candidates must be a list")
        for index, record in enumerate(external_candidates):
            if not isinstance(record, dict):
                reasons["external_malformed"] += 1
                excluded.append({"external_input_index": index, "reason": "external_malformed"})
                continue
            apn = normalize_apn(record.get("apn_norm") or record.get("apn"))
            source = record.get("source")
            signal_type = record.get("signal_type")
            if not apn:
                reason = "external_invalid_apn"
            elif not isinstance(source, str) or not source.strip():
                reason = "external_missing_source"
            elif not isinstance(signal_type, str) or not signal_type.strip():
                reason = "external_missing_signal_type"
            else:
                reason = ""
            if reason:
                reasons[reason] += 1
                excluded.append({
                    "external_input_index": index,
                    "reason": reason,
                    "apn": record.get("apn"),
                    "apn_norm": record.get("apn_norm"),
                })
                continue
            assert apn is not None
            external_grouped.setdefault(apn, []).append(_external_evidence_summary(record))

    candidates = []
    for apn in sorted(set(grouped) | set(external_grouped)):
        evidence = sorted(
            grouped.get(apn, []),
            key=lambda item: (
                str(item.get("recorded_date") or ""),
                str(item.get("doc_number") or ""),
                str(item.get("doc_type") or ""),
            ),
        )
        external_evidence = sorted(
            external_grouped.get(apn, []),
            key=lambda item: (
                str(item.get("source") or ""),
                str(item.get("source_record_id") or ""),
                str(item.get("property_address") or ""),
            ),
        )
        candidates.append({
            "apn_norm": apn,
            "recorder_lien_evidence": evidence,
            "external_signal_evidence": external_evidence,
        })
    return {
        "candidates": candidates,
        "excluded": excluded,
        "audit_reason_counts": dict(sorted(reasons.items())),
    }


def _clean_text(parts: list[str]) -> str:
    return " ".join("".join(parts).split())


def _normal_header(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().upper()


def _header_key(value: str) -> str:
    """Compare visible header semantics despite markup-adjacent whitespace."""
    return re.sub(r"[^A-Z0-9]", "", value.upper())


class _PropertyPageParser(HTMLParser):
    """Purpose-built parser for identity fields and tblAcctBal."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.identity_state_code: str | None = None
        self.property_fields: dict[str, str] = {}
        self.table_seen = False
        self.tbody_count = 0
        self.headers: list[str] = []
        self.rows: list[list[str]] = []
        self.errors: list[str] = []
        self._label_parts: list[str] | None = None
        self._pending_label: str | None = None
        self._property_parts: list[str] | None = None
        self._in_table = False
        self._section: str | None = None
        self._row: list[str] | None = None
        self._cell_parts: list[str] | None = None
        self._cell_tag: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "input" and attributes.get("id") == "form_statecode":
            self.identity_state_code = attributes.get("value")
        if tag == "h6":
            self._label_parts = []
        elif tag == "p" and self._pending_label in {"PROPERTY ADDRESS", "PROPERTY TYPE"}:
            self._property_parts = []
        if tag == "table" and attributes.get("id") == "tblAcctBal":
            if self.table_seen:
                self.errors.append("duplicate_balance_table")
            self.table_seen = True
            self._in_table = True
        elif self._in_table and tag in {"thead", "tbody"}:
            if tag == "tbody":
                self.tbody_count += 1
            self._section = tag
        elif self._in_table and tag == "tr":
            self._row = []
        elif self._in_table and tag in {"th", "td"} and self._row is not None:
            self._cell_tag = tag
            self._cell_parts = []

    def handle_data(self, data: str) -> None:
        if self._label_parts is not None:
            self._label_parts.append(data)
        if self._property_parts is not None:
            self._property_parts.append(data)
        if self._cell_parts is not None:
            self._cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "h6" and self._label_parts is not None:
            self._pending_label = _normal_header(_clean_text(self._label_parts))
            self._label_parts = None
        elif tag == "p" and self._property_parts is not None:
            assert self._pending_label is not None
            self.property_fields[self._pending_label] = _clean_text(self._property_parts)
            self._property_parts = None
            self._pending_label = None
        if self._in_table and tag in {"th", "td"} and self._cell_parts is not None:
            if tag != self._cell_tag:
                self.errors.append("malformed_balance_cell")
            assert self._row is not None
            self._row.append(_clean_text(self._cell_parts))
            self._cell_parts = None
            self._cell_tag = None
        elif self._in_table and tag == "tr" and self._row is not None:
            if self._section == "thead" and self._row:
                if self.headers:
                    self.errors.append("multiple_balance_header_rows")
                else:
                    self.headers = self._row
            elif self._section == "tbody" and self._row:
                self.rows.append(self._row)
            self._row = None
        elif self._in_table and tag in {"thead", "tbody"}:
            self._section = None
        elif self._in_table and tag == "table":
            self._in_table = False
            self._section = None


def _parse_nonnegative_decimal(value: str, field: str) -> Decimal:
    normalized = value.strip().replace(",", "")
    if normalized.startswith("$"):
        normalized = normalized[1:].strip()
    if not re.fullmatch(r"\d+(?:\.\d+)?", normalized):
        raise ValueError(f"invalid_{field}")
    try:
        amount = Decimal(normalized)
    except InvalidOperation as exc:
        raise ValueError(f"invalid_{field}") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"invalid_{field}")
    return amount


def _error_result(errors: Iterable[str], **identity: object) -> dict:
    return {
        "status": "verification_error",
        "errors": list(errors),
        "unpaid_balances": [],
        "tax_years": [],
        "certificate_numbers": [],
        "years_delinquent": 0,
        "amount_owed": 0.0,
        **identity,
    }


def parse_property_page(html: str, requested_apn: str, *, as_of: date) -> dict:
    """Validate and parse the official account balance table, failing closed."""
    parser = _PropertyPageParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:
        return _error_result([f"html_parse_error:{type(exc).__name__}"])

    identity_raw = (parser.identity_state_code or "").strip()
    identity_apn = normalize_apn(identity_raw)
    address = parser.property_fields.get("PROPERTY ADDRESS", "").strip()
    property_type = parser.property_fields.get("PROPERTY TYPE", "").strip()
    errors = list(parser.errors)
    if identity_raw != requested_apn:
        errors.append("state_code_identity_mismatch")
    if not address:
        errors.append("missing_property_address")
    if not property_type:
        errors.append("missing_property_type")
    elif property_type.casefold() != "real estate":
        errors.append("unsupported_property_type")
    if not parser.table_seen:
        errors.append("missing_balance_table")
    if parser.tbody_count != 1:
        errors.append("balance_table_tbody_count")
    if [_header_key(h) for h in parser.headers] != [_header_key(h) for h in BALANCE_HEADERS]:
        errors.append("balance_table_schema_mismatch")
    if errors:
        return _error_result(
            errors,
            page_state_code=identity_apn,
            property_address=address or None,
            property_type=property_type or None,
        )

    parsed_rows: list[dict] = []
    for row_index, cells in enumerate(parser.rows, start=1):
        # The live Treasurer page includes a ten-cell, all-empty presentation row
        # after the balances. It is not a tax record and must not fail or count.
        if cells and not any(cell.strip() for cell in cells):
            continue
        if len(cells) != 10:
            errors.append(f"balance_row_{row_index}_cell_count")
            continue
        year_text = cells[1].strip()
        # Live rows identify semiannual installments as "YYYY - 1" and
        # "YYYY - 2". Count the base year once while retaining the installment.
        year_match = re.fullmatch(r"(\d{4})(?:\s*-\s*([12]))?", year_text)
        if not year_match:
            errors.append(f"balance_row_{row_index}_invalid_tax_year")
            continue
        year = int(year_match.group(1))
        installment = int(year_match.group(2)) if year_match.group(2) else None
        if year < 1900 or year > as_of.year:
            errors.append(f"balance_row_{row_index}_unreasonable_tax_year")
            continue
        if not cells[3].strip():
            errors.append(f"balance_row_{row_index}_missing_interest_date")
            continue
        try:
            interest_percent = _parse_nonnegative_decimal(
                cells[4].strip().removesuffix("%").strip(), "interest_percent"
            )
            amounts = [
                _parse_nonnegative_decimal(cells[index], BALANCE_HEADERS[index].lower().replace(" ", "_"))
                for index in range(5, 10)
            ]
        except ValueError as exc:
            errors.append(f"balance_row_{row_index}_{exc}")
            continue
        parsed_rows.append({
            "tax_year": year,
            "installment": installment,
            "certificate_number": cells[2].strip(),
            "interest_date": cells[3].strip(),
            "interest_percent": float(interest_percent),
            "amount": float(amounts[0]),
            "interest": float(amounts[1]),
            "fees": float(amounts[2]),
            "penalties": float(amounts[3]),
            "total_due": float(amounts[4]),
            "_total_due_decimal": amounts[4],
        })
    if errors:
        return _error_result(
            errors,
            page_state_code=identity_apn,
            property_address=address,
            property_type=property_type,
        )

    unpaid_rows = [row for row in parsed_rows if row["_total_due_decimal"] > 0]
    tax_years = sorted({row["tax_year"] for row in unpaid_rows})
    certificates = []
    for row in unpaid_rows:
        certificate = row["certificate_number"]
        if certificate and certificate not in certificates:
            certificates.append(certificate)
    total_due = sum((row["_total_due_decimal"] for row in unpaid_rows), Decimal("0"))
    for row in parsed_rows:
        row.pop("_total_due_decimal", None)

    if not unpaid_rows:
        status = "verified_clear"
    elif len(tax_years) >= 3:
        status = "verified_eligible"
    else:
        status = "verified_not_eligible"
    return {
        "status": status,
        "errors": [],
        "page_state_code": identity_apn,
        "property_address": address,
        "property_type": property_type,
        "balance_rows": parsed_rows,
        "unpaid_balances": [row for row in parsed_rows if row["total_due"] > 0],
        "tax_years": tax_years,
        "certificate_numbers": certificates,
        "years_delinquent": len(tax_years),
        "amount_owed": float(total_due),
    }


def _prepared_source_url(apn: str) -> str:
    prepared = requests.Request("GET", PROPERTY_INQUIRY_URL, params={"stateCode": apn}).prepare()
    assert prepared.url is not None
    return prepared.url


def _timestamp(now: Callable[[], datetime]) -> str:
    value = now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _atomic_write(path: Path, writer: Callable[[Any], None]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temp_name = handle.name
            writer(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        temp_name = None
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def atomic_write_jsonl(path: Path, records: Iterable[dict]) -> None:
    materialized = list(records)

    def write(handle: Any) -> None:
        for record in materialized:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")

    _atomic_write(path, write)


def atomic_write_json(path: Path, value: dict) -> None:
    def write(handle: Any) -> None:
        json.dump(value, handle, sort_keys=True, indent=2)
        handle.write("\n")

    _atomic_write(path, write)


def _eligible_doc(outcome: dict, *, as_of: date) -> dict:
    apn = outcome["apn_norm"]
    return {
        "county": "Pima",
        "source": "pima_treasurer_property_inquiry",
        "doc_type": "Tax Delinquent 3+ Years",
        "doc_code": "TAX3",
        "category": "Tax & Liens",
        "doc_number": f"PIMA-TAX3-{apn}",
        "recorded_date": as_of.isoformat(),
        "as_of": as_of.isoformat(),
        "apn": apn,
        "apn_norm": apn,
        "resolved": True,
        "amount_owed": outcome["amount_owed"],
        "years_delinquent": outcome["years_delinquent"],
        "tax_years": outcome["tax_years"],
        "certificate_numbers": outcome["certificate_numbers"],
        "foreclosure_risk_basis": RISK_BASIS,
        "source_url": outcome["source_url"],
        "verified_at": outcome["verified_at"],
        "property_address": outcome.get("property_address"),
        "property_type": outcome.get("property_type"),
        "unpaid_balances": outcome.get("unpaid_balances", []),
        "recorder_lien_evidence": outcome["recorder_lien_evidence"],
        "external_signal_evidence": outcome.get("external_signal_evidence", []),
    }


def run_verifier(
    leads_path: Path,
    verification_output: Path,
    eligible_output: Path,
    summary_output: Path,
    *,
    as_of: date,
    pace: float = 1.0,
    timeout: float = 20.0,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    external_candidates: list[dict] | None = None,
) -> int:
    """Run all candidates. Incomplete runs preserve the last eligible store."""
    if pace < 1.0:
        raise ValueError("pace must be at least 1 second")
    if timeout <= 0:
        raise ValueError("timeout must be positive")

    payload = json.loads(leads_path.read_text(encoding="utf-8"))
    extraction = extract_candidates(payload, external_candidates=external_candidates)
    candidates = extraction["candidates"]
    outcomes: list[dict] = []
    eligible: list[dict] = []
    client = session or requests.Session()
    request_count = 0

    for candidate in candidates:
        apn = candidate["apn_norm"]
        source_url = _prepared_source_url(apn)
        verified_at = _timestamp(now)
        response = None
        fetch_error = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if request_count:
                sleep(pace)
            request_count += 1
            try:
                response = client.get(
                    PROPERTY_INQUIRY_URL,
                    params={"stateCode": apn},
                    headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
                    timeout=timeout,
                )
            except requests.RequestException as exc:
                fetch_error = f"network_error:{type(exc).__name__}:{exc}"
                # Retry only failures with no HTTP response.
                if getattr(exc, "response", None) is None and attempt < MAX_ATTEMPTS:
                    continue
                break
            if response.status_code == 429 or 500 <= response.status_code <= 599:
                fetch_error = f"http_status:{response.status_code}"
                if attempt < MAX_ATTEMPTS:
                    response = None
                    continue
            elif response.status_code != 200:
                fetch_error = f"http_status:{response.status_code}"
            break

        base = {
            "apn_norm": apn,
            "source": "pima_treasurer_property_inquiry",
            "source_url": source_url,
            "as_of": as_of.isoformat(),
            "verified_at": verified_at,
            "recorder_lien_evidence": candidate["recorder_lien_evidence"],
            "external_signal_evidence": candidate.get("external_signal_evidence", []),
        }
        if response is None or response.status_code != 200:
            outcome = {**base, **_error_result([fetch_error or "fetch_failed"])}
        else:
            parsed = parse_property_page(response.text, apn, as_of=as_of)
            outcome = {**base, **parsed}
        outcomes.append(outcome)
        if outcome["status"] == "verified_eligible":
            eligible.append(_eligible_doc(outcome, as_of=as_of))

    error_count = sum(outcome["status"] == "verification_error" for outcome in outcomes)
    status_counts = Counter(outcome["status"] for outcome in outcomes)
    if not candidates:
        run_status = "no_candidates"
    elif error_count:
        run_status = "incomplete"
    else:
        run_status = "complete"
    complete = run_status == "complete"
    summary = {
        "status": run_status,
        "as_of": as_of.isoformat(),
        "generated_at": _timestamp(now),
        "candidates": len(candidates),
        "excluded": len(extraction["excluded"]),
        "audit_reason_counts": extraction["audit_reason_counts"],
        "verified_clear": status_counts["verified_clear"],
        "verified_not_eligible": status_counts["verified_not_eligible"],
        "eligible": status_counts["verified_eligible"],
        "errors": error_count,
        "all_requests_error": bool(candidates) and error_count == len(candidates),
        "eligible_output_preserved": not complete,
        "eligible_output": str(eligible_output),
        "verification_output": str(verification_output),
    }

    # Verification and summary always represent this attempt. The eligible store
    # is authoritative only after every candidate completed successfully.
    atomic_write_jsonl(verification_output, outcomes)
    if complete:
        atomic_write_jsonl(eligible_output, eligible)
        summary["eligible_output_preserved"] = False
    atomic_write_json(summary_output, summary)
    return 0 if complete else 1


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--leads", type=Path, default=DATA_DIR / "leads.json")
    parser.add_argument(
        "--verification-output", type=Path, default=DATA_DIR / "pima_tax_verification.jsonl"
    )
    parser.add_argument("--eligible-output", type=Path, default=DATA_DIR / "pima_tax_docs.jsonl")
    parser.add_argument(
        "--summary-output", type=Path, default=DATA_DIR / "pima_tax_verification_summary.json"
    )
    parser.add_argument("--as-of", type=_parse_date, default=date.today())
    parser.add_argument("--pace", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument(
        "--external-candidates", type=Path,
        help="Optional JSON list (or object with candidates list) of non-Recorder APN signals",
    )
    args = parser.parse_args(argv)
    try:
        external_candidates = None
        if args.external_candidates:
            external_payload = json.loads(args.external_candidates.read_text(encoding="utf-8"))
            external_candidates = (
                external_payload.get("candidates")
                if isinstance(external_payload, dict)
                else external_payload
            )
            if not isinstance(external_candidates, list):
                raise ValueError("external candidates file must contain a list")
        return run_verifier(
            args.leads,
            args.verification_output,
            args.eligible_output,
            args.summary_output,
            as_of=args.as_of,
            pace=args.pace,
            timeout=args.timeout,
            external_candidates=external_candidates,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
