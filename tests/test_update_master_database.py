import csv
import json
import tempfile
import unittest
from pathlib import Path

from update_master_database import run_update


def read_csv(path: Path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


class UpdateMasterDatabaseTests(unittest.TestCase):
    def test_first_run_writes_raw_files_master_rows_and_status(self):
        fixtures = {
            "nasdaq": [
                {
                    "Exchange": "NASDAQ",
                    "Symbol": "QQQ",
                    "Name": "Invesco QQQ Trust",
                    "InstrumentType": "ETF",
                    "FirstIpoDate": "1999",
                    "Source": "fixture",
                }
            ],
            "nyse": [
                {
                    "Exchange": "NYSE",
                    "Symbol": "IBM",
                    "Name": "International Business Machines",
                    "InstrumentType": "Stock",
                    "FirstIpoDate": "1915",
                    "Source": "fixture",
                }
            ],
            "amex": [],
        }

        def fetcher(exchange: str):
            return fixtures[exchange]

        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            summary = run_update(data_dir, run_date="2026-05-07", fetcher=fetcher)

            self.assertEqual(summary, {"nasdaq": 1, "nyse": 1, "amex": 0})
            self.assertEqual(read_csv(data_dir / "raw" / "nasdaq.csv")[0]["Symbol"], "QQQ")

            master_rows = read_csv(data_dir / "master_database.csv")
            self.assertEqual(len(master_rows), 2)
            self.assertTrue(master_rows[0]["UniqueID"])
            self.assertEqual(master_rows[0]["DateAdded"], "2026-05-07")
            self.assertEqual(master_rows[0]["IsActive"], "True")

            status = json.loads((data_dir / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["last_successful_run_date"], "2026-05-07")

    def test_removed_listing_is_closed_and_reused_ticker_gets_new_row(self):
        first_snapshot = {
            "nasdaq": [
                {
                    "Exchange": "NASDAQ",
                    "Symbol": "ABCD",
                    "Name": "Alpha Beta Co",
                    "InstrumentType": "Stock",
                    "FirstIpoDate": "",
                    "Source": "fixture",
                }
            ],
            "nyse": [],
            "amex": [],
        }
        second_snapshot = {"nasdaq": [], "nyse": [], "amex": []}
        third_snapshot = {
            "nasdaq": [
                {
                    "Exchange": "NASDAQ",
                    "Symbol": "ABCD",
                    "Name": "Atlas Bio Holdings",
                    "InstrumentType": "Stock",
                    "FirstIpoDate": "",
                    "Source": "fixture",
                }
            ],
            "nyse": [],
            "amex": [],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)

            run_update(data_dir, run_date="2026-05-07", fetcher=lambda exchange: first_snapshot[exchange])
            run_update(data_dir, run_date="2026-05-08", fetcher=lambda exchange: second_snapshot[exchange])
            run_update(data_dir, run_date="2026-05-11", fetcher=lambda exchange: third_snapshot[exchange])

            master_rows = read_csv(data_dir / "master_database.csv")
            self.assertEqual(len(master_rows), 2)

            original_row = next(row for row in master_rows if row["Name"] == "Alpha Beta Co")
            reused_row = next(row for row in master_rows if row["Name"] == "Atlas Bio Holdings")

            self.assertEqual(original_row["IsActive"], "False")
            self.assertEqual(original_row["DateRemoved"], "2026-05-08")
            self.assertIn("Missing from current NASDAQ source snapshot", original_row["RemovalReason"])

            self.assertEqual(reused_row["IsActive"], "True")
            self.assertEqual(reused_row["DateAdded"], "2026-05-11")
            self.assertNotEqual(original_row["UniqueID"], reused_row["UniqueID"])

    def test_metadata_change_creates_new_active_row(self):
        original = {
            "nasdaq": [
                {
                    "Exchange": "NASDAQ",
                    "Symbol": "XYZ",
                    "Name": "Example Corp",
                    "InstrumentType": "Stock",
                    "FirstIpoDate": "2001",
                    "Source": "fixture",
                }
            ],
            "nyse": [],
            "amex": [],
        }
        updated = {
            "nasdaq": [
                {
                    "Exchange": "NASDAQ",
                    "Symbol": "XYZ",
                    "Name": "Example Corp ETF",
                    "InstrumentType": "ETF",
                    "FirstIpoDate": "2001",
                    "Source": "fixture",
                }
            ],
            "nyse": [],
            "amex": [],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            run_update(data_dir, run_date="2026-05-07", fetcher=lambda exchange: original[exchange])
            run_update(data_dir, run_date="2026-05-08", fetcher=lambda exchange: updated[exchange])

            master_rows = read_csv(data_dir / "master_database.csv")
            self.assertEqual(len(master_rows), 2)
            inactive_row = next(row for row in master_rows if row["Name"] == "Example Corp")
            active_row = next(row for row in master_rows if row["Name"] == "Example Corp ETF")

            self.assertEqual(inactive_row["IsActive"], "False")
            self.assertEqual(inactive_row["DateRemoved"], "2026-05-08")
            self.assertEqual(inactive_row["RemovalReason"], "Listing metadata changed")
            self.assertEqual(active_row["InstrumentType"], "ETF")


if __name__ == "__main__":
    unittest.main()
