import unittest
from datetime import date

from scrapers import pima_recorder


class PimaRecorderCompletenessTests(unittest.TestCase):
    def test_planned_chunks_split_every_calendar_day(self):
        self.assertEqual(
            list(pima_recorder.planned_chunks(date(2026, 8, 31), date(2026, 9, 2))),
            [
                ("08/31/2026", "08/31/2026", "2026-08-31"),
                ("09/01/2026", "09/01/2026", "2026-09-01"),
                ("09/02/2026", "09/02/2026", "2026-09-02"),
            ],
        )
    def test_raw_portal_cap_fails_even_when_deduplication_would_hide_it(self):
        capped_response = {
            "raw_row_count": 2000,
            "records": [
                {"seq": "20262430069"},
                {"seq": "20262430069"},
                {"seq": None},
            ],
        }
        with self.assertRaisesRegex(RuntimeError, "possible portal truncation"):
            pima_recorder.normalize_search_result(capped_response, "09/01/2026")

    def test_missing_raw_count_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "missing valid raw row count"):
            pima_recorder.normalize_search_result({"records": []}, "09/01/2026")


if __name__ == "__main__":
    unittest.main()
