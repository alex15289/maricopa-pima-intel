import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

import requests

from scrapers.pima_tax_delinquency import (
    BALANCE_HEADERS,
    atomic_write_jsonl,
    extract_candidates,
    normalize_apn,
    parse_property_page,
    run_verifier,
)


def balance_row(year="2022", cert="C-22", amount="100.00", interest="10.00",
                fees="2.00", penalties="3.00", total="115.00"):
    return ["", year, cert, "08/24/2026", "5%", amount, interest, fees, penalties, total]


def property_html(apn="12345678X", rows=(), address="123 MAIN ST", property_type="Real Estate",
                  headers=BALANCE_HEADERS):
    header_html = "".join(f"<th>{value}</th>" for value in headers)
    row_html = "".join(
        "<tr>" + "".join(f"<td>{value}</td>" for value in row) + "</tr>"
        for row in rows
    )
    return f"""<!doctype html><html><body>
      <input id="form_statecode" name="form_statecode" value="{apn}">
      <h6>PROPERTY ADDRESS</h6><p>{address}</p>
      <h6>PROPERTY TYPE</h6><p>{property_type}</p>
      <table id="tblAcctBal"><thead><tr>{header_html}</tr></thead>
      <tbody>{row_html}</tbody></table>
    </body></html>"""


def lead(apn="123-45-678X", doc_type="Federal Tax Lien", doc_number="2026001", **changes):
    record = {
        "county": "Pima",
        "source": "pima_recorder_portal",
        "doc_type": doc_type,
        "doc_number": doc_number,
        "recorded_date": "2026-07-01",
        "names": ["OWNER ONE", "IRS"],
        "apn": apn,
        "apn_norm": apn,
        "resolved": True,
        "resolved_by": "name",
        "match_tier": 1,
    }
    record.update(changes)
    return record


class FakeResponse:
    def __init__(self, text="", status_code=200, url="https://example.test/result"):
        self.text = text
        self.status_code = status_code
        self.url = url


class FakeSession:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class CandidateExtractionTests(unittest.TestCase):
    def test_groups_all_supported_resolved_recorder_liens_by_normalized_apn(self):
        payload = {"leads": [
            lead(doc_number="FED-1"),
            lead(apn="12345678x", doc_type="State Tax Lien", doc_number="STATE-1"),
            lead(apn="12345678X", doc_type="City Lien", doc_number="CITY-1"),
            lead(apn="999-99-9999", doc_type="Judgment", doc_number="OTHER"),
            lead(apn="999-99-9999", county="Maricopa", doc_number="WRONG-COUNTY"),
        ]}

        result = extract_candidates(payload)

        self.assertEqual(["12345678X"], [c["apn_norm"] for c in result["candidates"]])
        evidence = result["candidates"][0]["recorder_lien_evidence"]
        self.assertEqual(["CITY-1", "FED-1", "STATE-1"], sorted(d["doc_number"] for d in evidence))
        self.assertEqual(
            {"doc_type", "doc_number", "recorded_date", "source", "names", "resolved_by", "match_tier"},
            set(evidence[0]),
        )

    def test_excludes_unresolved_and_malformed_apns_with_audit_reasons(self):
        payload = {"leads": [
            lead(apn="123-45", doc_number="SHORT"),
            lead(apn="123-45-678!", doc_number="PUNCT"),
            lead(apn="", apn_norm=None, doc_number="EMPTY"),
            lead(apn="123-45-678X", resolved=False, doc_number="UNRESOLVED"),
        ]}

        result = extract_candidates(payload)

        self.assertEqual([], result["candidates"])
        self.assertEqual(
            {"invalid_apn": 3, "unresolved": 1},
            result["audit_reason_counts"],
        )
        self.assertEqual(4, len(result["excluded"]))

    def test_normalization_accepts_only_nine_alphanumeric_characters(self):
        self.assertEqual("13820222X", normalize_apn("138-20-222x"))
        for malformed in (None, "", "12345678", "1234567890", "12345678!", "１２３４５６７８９"):
            with self.subTest(malformed=malformed):
                self.assertIsNone(normalize_apn(malformed))

    def test_external_tax_signals_merge_by_apn_without_becoming_recorder_evidence(self):
        payload = {"leads": [lead(doc_number="FED-1")]}
        external = [{
            "apn": "123-45-678x",
            "source": "dealmachine_existing_export",
            "signal_type": "tax_delinquent_flag",
            "source_record_id": "property-1",
            "property_address": "123 MAIN ST",
            "tax_delinquent_year": "2024",
        }]

        result = extract_candidates(payload, external_candidates=external)

        self.assertEqual(1, len(result["candidates"]))
        candidate = result["candidates"][0]
        self.assertEqual("FED-1", candidate["recorder_lien_evidence"][0]["doc_number"])
        self.assertEqual("dealmachine_existing_export", candidate["external_signal_evidence"][0]["source"])
        self.assertNotIn("doc_type", candidate["external_signal_evidence"][0])

    def test_existing_verified_tax_lead_is_revalidated_without_becoming_recorder_evidence(self):
        prior = lead(
            apn="140-15-0530",
            doc_type="Tax Delinquent 3+ Years",
            doc_number="PIMA-TAX3-140150530",
            source="pima_treasurer_property_inquiry",
            doc_code="TAX3",
            recorder_lien_evidence=[],
            external_signal_evidence=[{
                "source": "dealmachine_existing_export_2026-08-24",
                "signal_type": "tax_delinquent_flag",
                "source_record_id": "1951-e-virginia-st",
                "property_address": "1951 E VIRGINIA ST",
                "tax_delinquent_year": "2024",
            }],
        )

        result = extract_candidates({"leads": [prior]})

        self.assertEqual(["140150530"], [c["apn_norm"] for c in result["candidates"]])
        candidate = result["candidates"][0]
        self.assertEqual([], candidate["recorder_lien_evidence"])
        self.assertEqual(
            "dealmachine_existing_export_2026-08-24",
            candidate["external_signal_evidence"][0]["source"],
        )


class OfficialHtmlParsingTests(unittest.TestCase):
    def test_valid_empty_balance_table_is_verified_clear(self):
        parsed = parse_property_page(property_html(rows=[]), "12345678X", as_of=date(2026, 8, 24))

        self.assertEqual("verified_clear", parsed["status"])
        self.assertEqual([], parsed["unpaid_balances"])
        self.assertEqual("123 MAIN ST", parsed["property_address"])
        self.assertEqual("Real Estate", parsed["property_type"])
        self.assertEqual(0, parsed["years_delinquent"])

    def test_zero_due_rows_are_verified_clear(self):
        parsed = parse_property_page(
            property_html(rows=[balance_row(total="0.00")]),
            "12345678X", as_of=date(2026, 8, 24),
        )

        self.assertEqual("verified_clear", parsed["status"])
        self.assertEqual([], parsed["tax_years"])
        self.assertEqual(0.0, parsed["amount_owed"])

    def test_one_two_and_three_distinct_positive_years_enforce_boundary(self):
        for count, expected_status in ((1, "verified_not_eligible"), (2, "verified_not_eligible"),
                                       (3, "verified_eligible")):
            rows = [balance_row(str(2023 + index), f"C-{index}") for index in range(count)]
            with self.subTest(count=count):
                parsed = parse_property_page(
                    property_html(rows=rows), "12345678X", as_of=date(2026, 8, 24)
                )
                self.assertEqual(expected_status, parsed["status"])
                self.assertEqual(count, parsed["years_delinquent"])

    def test_duplicate_year_does_not_double_count_and_zero_due_is_not_unpaid(self):
        rows = [
            balance_row("2022", "A", total="115.00"),
            balance_row("2022", "B", total="25.00"),
            balance_row("2021", "C", total="0.00"),
            balance_row("2023", "D", total="75.00"),
        ]
        parsed = parse_property_page(property_html(rows=rows), "12345678X", as_of=date(2026, 8, 24))

        self.assertEqual("verified_not_eligible", parsed["status"])
        self.assertEqual(2, parsed["years_delinquent"])
        self.assertEqual([2022, 2023], parsed["tax_years"])
        self.assertEqual(["A", "B", "D"], parsed["certificate_numbers"])
        self.assertEqual(215.0, parsed["amount_owed"])

    def test_live_installment_year_labels_are_normalized_without_double_counting(self):
        rows = [
            balance_row("2022 - 1", "CERT-1", total="100.00"),
            balance_row("2023 - 1", "CERT-1", total="200.00"),
            balance_row("2024 - 1", "CERT-1", total="300.00"),
            balance_row("2025 - 1", "", total="400.00"),
            balance_row("2025 - 2", "", total="500.00"),
            ["", "", "", "", "", "", "", "", "", ""],
        ]

        parsed = parse_property_page(
            property_html(rows=rows), "12345678X", as_of=date(2026, 8, 24)
        )

        self.assertEqual("verified_eligible", parsed["status"])
        self.assertEqual([2022, 2023, 2024, 2025], parsed["tax_years"])
        self.assertEqual(4, parsed["years_delinquent"])
        self.assertEqual(["CERT-1"], parsed["certificate_numbers"])
        self.assertEqual(1500.0, parsed["amount_owed"])
        self.assertEqual([1, 1, 1, 1, 2], [row["installment"] for row in parsed["balance_rows"]])

    def test_semantic_header_text_survives_nested_markup_without_whitespace(self):
        html = property_html().replace("<th>INTEREST PERCENT</th>", "<th>INTEREST<i></i>PERCENT</th>")

        parsed = parse_property_page(html, "12345678X", as_of=date(2026, 8, 24))

        self.assertEqual("verified_clear", parsed["status"])

    def test_identity_property_fields_table_and_schema_are_required(self):
        malformed_cases = {
            "wrong_identity": property_html(apn="999999999"),
            "normalized_but_not_exact_identity": property_html(apn="123-45-678X"),
            "missing_identity": property_html().replace('id="form_statecode"', 'id="other"'),
            "missing_address": property_html(address="  "),
            "missing_type": property_html(property_type=""),
            "wrong_property_type": property_html(property_type="Personal Property"),
            "missing_table": property_html().replace('id="tblAcctBal"', 'id="otherTable"'),
            "missing_tbody": property_html().replace("<tbody></tbody>", ""),
            "changed_headers": property_html(headers=BALANCE_HEADERS[:-1] + ["BALANCE"]),
            "nine_cell_row": property_html(rows=[balance_row()[:-1]]),
        }
        for case, html in malformed_cases.items():
            with self.subTest(case=case):
                parsed = parse_property_page(html, "12345678X", as_of=date(2026, 8, 24))
                self.assertEqual("verification_error", parsed["status"])
                self.assertTrue(parsed["errors"])

    def test_malformed_year_or_numeric_fields_fail_closed(self):
        malformed_rows = {
            "year_format": balance_row("22"),
            "future_year": balance_row("2027"),
            "ancient_year": balance_row("1800"),
            "negative": balance_row(amount="-1.00"),
            "nonnumeric": balance_row(total="unknown"),
            "bad_interest_rate": balance_row(),
        }
        malformed_rows["bad_interest_rate"][4] = "variable"
        for case, row in malformed_rows.items():
            with self.subTest(case=case):
                parsed = parse_property_page(
                    property_html(rows=[row]), "12345678X", as_of=date(2026, 8, 24)
                )
                self.assertEqual("verification_error", parsed["status"])


class VerifierRunTests(unittest.TestCase):
    def _paths(self, tmp):
        base = Path(tmp)
        return (
            base / "leads.json",
            base / "verification.jsonl",
            base / "eligible.jsonl",
            base / "summary.json",
        )

    def _write_leads(self, path, records):
        path.write_text(json.dumps({"leads": records}))

    def test_complete_run_writes_atomic_outputs_and_exact_eligible_evidence_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            leads_path, verification_path, eligible_path, summary_path = self._paths(tmp)
            self._write_leads(leads_path, [lead(doc_number="FED-1")])
            rows = [balance_row("2021", "CERT-A", total="100.25"),
                    balance_row("2022", "CERT-B", total="200.50"),
                    balance_row("2023", "CERT-C", total="300.75")]
            session = FakeSession([FakeResponse(property_html(rows=rows), url="https://www.to.pima.gov/propertyInquiry/?stateCode=12345678X")])
            real_replace = __import__("os").replace
            replace_calls = []

            def recording_replace(source, destination):
                replace_calls.append((Path(source), Path(destination)))
                return real_replace(source, destination)

            with mock.patch("scrapers.pima_tax_delinquency.os.replace", side_effect=recording_replace):
                result = run_verifier(
                    leads_path, verification_path, eligible_path, summary_path,
                    as_of=date(2026, 8, 24), pace=1.0, timeout=7.0,
                    session=session, sleep=lambda _: None,
                    now=lambda: datetime(2026, 8, 24, 12, 30, tzinfo=timezone.utc),
                )

            self.assertEqual(0, result)
            self.assertEqual(
                {verification_path, eligible_path, summary_path},
                {destination for _, destination in replace_calls},
            )
            self.assertTrue(all(source.parent == destination.parent for source, destination in replace_calls))
            self.assertFalse(any(p.name.endswith(".tmp") for p in Path(tmp).iterdir()))
            doc = json.loads(eligible_path.read_text().strip())
            self.assertEqual("Pima", doc["county"])
            self.assertEqual("pima_treasurer_property_inquiry", doc["source"])
            self.assertEqual("Tax Delinquent 3+ Years", doc["doc_type"])
            self.assertEqual("TAX3", doc["doc_code"])
            self.assertEqual("Tax & Liens", doc["category"])
            self.assertEqual("PIMA-TAX3-12345678X", doc["doc_number"])
            self.assertEqual("2026-08-24", doc["recorded_date"])
            self.assertEqual("2026-08-24", doc["as_of"])
            self.assertEqual("12345678X", doc["apn"])
            self.assertEqual("12345678X", doc["apn_norm"])
            self.assertIs(doc["resolved"], True)
            self.assertEqual(601.5, doc["amount_owed"])
            self.assertEqual(3, doc["years_delinquent"])
            self.assertEqual([2021, 2022, 2023], doc["tax_years"])
            self.assertEqual(["CERT-A", "CERT-B", "CERT-C"], doc["certificate_numbers"])
            self.assertEqual(
                "County-verified 3+ distinct currently unpaid tax years; certificate/redemption/legal-stage verification required.",
                doc["foreclosure_risk_basis"],
            )
            self.assertEqual("https://www.to.pima.gov/propertyInquiry/?stateCode=12345678X", doc["source_url"])
            self.assertEqual("2026-08-24T12:30:00Z", doc["verified_at"])
            self.assertEqual("FED-1", doc["recorder_lien_evidence"][0]["doc_number"])
            self.assertNotIn("foreclosure_imminent", json.dumps(doc).lower())
            verification = json.loads(verification_path.read_text().strip())
            self.assertEqual("verified_eligible", verification["status"])
            self.assertEqual(doc["recorder_lien_evidence"], verification["recorder_lien_evidence"])
            summary = json.loads(summary_path.read_text())
            self.assertEqual("complete", summary["status"])
            self.assertEqual(1, summary["eligible"])
            self.assertEqual(0, summary["errors"])

            url, kwargs = session.calls[0]
            self.assertEqual("https://www.to.pima.gov/propertyInquiry/", url)
            self.assertEqual({"stateCode": "12345678X"}, kwargs["params"])
            self.assertEqual(7.0, kwargs["timeout"])
            self.assertIn("Mozilla/5.0", kwargs["headers"]["User-Agent"])

    def test_complete_clear_run_replaces_stale_eligible_output_with_empty_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            leads_path, verification_path, eligible_path, summary_path = self._paths(tmp)
            self._write_leads(leads_path, [lead()])
            eligible_path.write_text('{"stale":true}\n')

            code = run_verifier(
                leads_path, verification_path, eligible_path, summary_path,
                as_of=date(2026, 8, 24), pace=1.0, timeout=5,
                session=FakeSession([FakeResponse(property_html())]), sleep=lambda _: None,
            )

            self.assertEqual(0, code)
            self.assertEqual("", eligible_path.read_text())
            self.assertEqual("verified_clear", json.loads(verification_path.read_text())["status"])

    def test_incomplete_run_records_error_continues_and_preserves_stale_eligible_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            leads_path, verification_path, eligible_path, summary_path = self._paths(tmp)
            self._write_leads(leads_path, [lead(apn="111111111"), lead(apn="222222222")])
            stale = '{"known":"eligible"}\n'
            eligible_path.write_text(stale)
            session = FakeSession([
                requests.ConnectionError("offline"), requests.ConnectionError("offline"),
                requests.ConnectionError("offline"), FakeResponse(property_html(apn="222222222")),
            ])

            code = run_verifier(
                leads_path, verification_path, eligible_path, summary_path,
                as_of=date(2026, 8, 24), pace=1.0, timeout=5,
                session=session, sleep=lambda _: None,
            )

            self.assertEqual(1, code)
            self.assertEqual(stale, eligible_path.read_text())
            outcomes = [json.loads(line) for line in verification_path.read_text().splitlines()]
            self.assertEqual(["verification_error", "verified_clear"], sorted(o["status"] for o in outcomes))
            summary = json.loads(summary_path.read_text())
            self.assertEqual("incomplete", summary["status"])
            self.assertEqual(1, summary["errors"])
            self.assertTrue(summary["eligible_output_preserved"])
            self.assertEqual(4, len(session.calls))

    def test_zero_candidates_exits_nonzero_and_preserves_stale_eligible_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            leads_path, verification_path, eligible_path, summary_path = self._paths(tmp)
            self._write_leads(leads_path, [lead(resolved=False), lead(apn="bad")])
            eligible_path.write_text("old\n")

            code = run_verifier(
                leads_path, verification_path, eligible_path, summary_path,
                as_of=date(2026, 8, 24), pace=1.0, timeout=5,
                session=FakeSession([]), sleep=lambda _: None,
            )

            self.assertEqual(1, code)
            self.assertEqual("old\n", eligible_path.read_text())
            self.assertEqual("", verification_path.read_text())
            summary = json.loads(summary_path.read_text())
            self.assertEqual("no_candidates", summary["status"])
            self.assertEqual({"invalid_apn": 1, "unresolved": 1}, summary["audit_reason_counts"])

    def test_atomic_jsonl_replaces_existing_file_without_partial_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "records.jsonl"
            path.write_text("old\n")
            atomic_write_jsonl(path, [{"apn": "one"}, {"apn": "two"}])
            self.assertEqual(
                [{"apn": "one"}, {"apn": "two"}],
                [json.loads(line) for line in path.read_text().splitlines()],
            )
            self.assertEqual([path], list(Path(tmp).iterdir()))

    def test_pacing_retries_only_retryable_failures_and_rejects_subsecond_pace(self):
        with tempfile.TemporaryDirectory() as tmp:
            leads_path, verification_path, eligible_path, summary_path = self._paths(tmp)
            self._write_leads(leads_path, [lead()])
            sleep = mock.Mock()
            session = FakeSession([
                FakeResponse(status_code=429),
                FakeResponse(status_code=503),
                FakeResponse(property_html()),
            ])
            code = run_verifier(
                leads_path, verification_path, eligible_path, summary_path,
                as_of=date(2026, 8, 24), pace=1.25, timeout=5,
                session=session, sleep=sleep,
            )
            self.assertEqual(0, code)
            self.assertEqual(3, len(session.calls))
            self.assertEqual([mock.call(1.25), mock.call(1.25)], sleep.call_args_list)

            with self.assertRaises(ValueError):
                run_verifier(
                    leads_path, verification_path, eligible_path, summary_path,
                    as_of=date(2026, 8, 24), pace=0.99, timeout=5,
                    session=FakeSession([]), sleep=lambda _: None,
                )


if __name__ == "__main__":
    unittest.main()
