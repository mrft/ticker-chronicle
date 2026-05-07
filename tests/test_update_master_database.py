import csv
import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from update_master_database import fetch_fmp_delisting_reason, normalize_delisting_reason, run_update


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
            run_update(
                data_dir,
                run_date="2026-05-08",
                fetcher=lambda exchange: second_snapshot[exchange],
                reason_resolver=lambda row, run_date: "Bankruptcy: Chapter 11 filing (source: test)",
            )
            run_update(data_dir, run_date="2026-05-11", fetcher=lambda exchange: third_snapshot[exchange])

            master_rows = read_csv(data_dir / "master_database.csv")
            self.assertEqual(len(master_rows), 2)

            original_row = next(row for row in master_rows if row["Name"] == "Alpha Beta Co")
            reused_row = next(row for row in master_rows if row["Name"] == "Atlas Bio Holdings")

            self.assertEqual(original_row["IsActive"], "False")
            self.assertEqual(original_row["DateRemoved"], "2026-05-08")
            self.assertEqual(original_row["RemovalReason"], "Bankruptcy: Chapter 11 filing (source: test)")

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

    def test_unique_id_is_stable_for_same_metadata_without_ipo_date(self):
        snapshot = {
            "nasdaq": [
                {
                    "Exchange": "NASDAQ",
                    "Symbol": "STBL",
                    "Name": "Stable Corp",
                    "InstrumentType": "Stock",
                    "FirstIpoDate": "",
                    "Source": "fixture",
                }
            ],
            "nyse": [],
            "amex": [],
        }
        removed = {"nasdaq": [], "nyse": [], "amex": []}

        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            run_update(data_dir, run_date="2026-05-07", fetcher=lambda exchange: snapshot[exchange])
            run_update(data_dir, run_date="2026-05-08", fetcher=lambda exchange: removed[exchange])
            run_update(data_dir, run_date="2026-05-09", fetcher=lambda exchange: snapshot[exchange])

            rows = [row for row in read_csv(data_dir / "master_database.csv") if row["Symbol"] == "STBL"]
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["UniqueID"], rows[1]["UniqueID"])

    def test_normalize_delisting_reason_categories(self):
        self.assertEqual(
            normalize_delisting_reason("Company entered bankruptcy proceedings", "test"),
            "Bankruptcy: Company entered bankruptcy proceedings (source: test)",
        )
        self.assertEqual(
            normalize_delisting_reason("Completed merger with another issuer", "test"),
            "Acquisition/Merger/Privatization: Completed merger with another issuer (source: test)",
        )
        self.assertEqual(
            normalize_delisting_reason("Failed to satisfy listing requirements", "test"),
            "Regulatory issue: Failed to satisfy listing requirements (source: test)",
        )

    @mock.patch("update_master_database._fetch_json")
    def test_fetch_fmp_delisting_reason_ignores_mismatched_exchange(self, mock_fetch_json):
        mock_fetch_json.return_value = [
            {
                "symbol": "ABCD",
                "exchange": "NYSE",
                "delistedDate": "2026-05-08",
                "reason": "Completed merger",
            },
            {
                "symbol": "ABCD",
                "exchange": "NASDAQ",
                "delistedDate": "2026-05-08",
                "reason": "Completed merger transaction",
            },
        ]

        with mock.patch.dict(os.environ, {"FMP_API_KEY": "test-key"}, clear=False):
            reason = fetch_fmp_delisting_reason("ABCD", "NASDAQ", "2026-05-08")

        self.assertEqual(
            reason,
            "Acquisition/Merger/Privatization: Completed merger transaction (source: FMP Delisted API)",
        )

    @mock.patch("update_master_database._fetch_json")
    def test_fetch_fmp_delisting_reason_ignores_future_dates(self, mock_fetch_json):
        mock_fetch_json.return_value = [
            {
                "symbol": "ABCD",
                "exchange": "NASDAQ",
                "delistedDate": "2026-05-09",
                "reason": "Bankruptcy filing",
            },
            {
                "symbol": "ABCD",
                "exchange": "NASDAQ",
                "delistedDate": "2026-05-08",
                "reason": "Completed merger transaction",
            },
        ]

        with mock.patch.dict(os.environ, {"FMP_API_KEY": "test-key"}, clear=False):
            reason = fetch_fmp_delisting_reason("ABCD", "NASDAQ", "2026-05-08")

        self.assertEqual(
            reason,
            "Acquisition/Merger/Privatization: Completed merger transaction (source: FMP Delisted API)",
        )

    @mock.patch("update_master_database._fetch_json")
    def test_fetch_fmp_delisting_reason_handles_missing_reason_fields(self, mock_fetch_json):
        mock_fetch_json.return_value = [
            {
                "symbol": "WXYZ",
                "exchange": "NASDAQ",
                "delistedDate": "2026-05-07",
            }
        ]

        with mock.patch.dict(os.environ, {"FMP_API_KEY": "test-key"}, clear=False):
            reason = fetch_fmp_delisting_reason("WXYZ", "NASDAQ", "2026-05-08")

        self.assertEqual(reason, "Other delisting reason: Details unavailable (source: FMP Delisted API)")


if __name__ == "__main__":
    unittest.main()
