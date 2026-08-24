import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline import merge_leads_history


def payload(rows):
    return {"leads": rows, "total_leads": len(rows)}


def lead(doc_number, *, code="NS", as_of="2026-08-23"):
    return {
        "county": "Pima",
        "source": "pima_treasurer_property_inquiry" if code == "TAX3" else "test",
        "doc_number": doc_number,
        "doc_code": code,
        "doc_type": "Tax Delinquent 3+ Years" if code == "TAX3" else "Notice",
        "category": "Tax & Liens" if code == "TAX3" else "Foreclosure",
        "recorded_date": as_of,
        "as_of": as_of,
        "resolved": True,
    }


class AuthoritativeHistoryMergeTests(unittest.TestCase):
    def run_merge(self, baseline_rows, fresh_rows, *extra_args):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            baseline = root / "baseline.json"
            fresh = root / "fresh.json"
            output = root / "output.json"
            baseline.write_text(json.dumps(payload(baseline_rows)))
            fresh.write_text(json.dumps(payload(fresh_rows)))
            argv = [
                "merge_leads_history.py",
                "--baseline", str(baseline),
                "--fresh", str(fresh),
                "--output", str(output),
                *extra_args,
            ]
            with mock.patch("sys.argv", argv):
                merge_leads_history.main()
            return json.loads(output.read_text())

    def test_complete_authoritative_tax_snapshot_removes_stale_tax_rows_only(self):
        old_tax = lead("PIMA-TAX3-111111111", code="TAX3")
        ordinary = lead("NS-1")

        out = self.run_merge(
            [old_tax, ordinary], [], "--authoritative-doc-code", "TAX3"
        )

        self.assertEqual(["NS-1"], [row["doc_number"] for row in out["leads"]])

    def test_authoritative_tax_snapshot_replaces_same_apn_instead_of_duplicating(self):
        old_tax = lead("PIMA-TAX3-111111111", code="TAX3", as_of="2026-08-23")
        current_tax = lead("PIMA-TAX3-111111111", code="TAX3", as_of="2026-08-24")

        out = self.run_merge(
            [old_tax], [current_tax], "--authoritative-doc-code", "TAX3"
        )

        self.assertEqual(1, len(out["leads"]))
        self.assertEqual("2026-08-24", out["leads"][0]["as_of"])

    def test_without_authoritative_flag_baseline_history_is_preserved(self):
        old_tax = lead("PIMA-TAX3-111111111", code="TAX3")

        out = self.run_merge([old_tax], [])

        self.assertEqual(["PIMA-TAX3-111111111"], [row["doc_number"] for row in out["leads"]])


if __name__ == "__main__":
    unittest.main()
