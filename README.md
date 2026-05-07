# ticker-chronicle

Download tickers from major US exchanges every weekday and keep a git-backed audit trail of listing history.

## What gets updated

- `data/raw/nasdaq.csv`
- `data/raw/nyse.csv`
- `data/raw/amex.csv`
- `data/master_database.csv`
- `data/status.json`

The raw files contain the latest exchange snapshots. Git tracks their day-to-day diffs.

## Master database format

`data/master_database.csv` is maintained like a small SCD Type 2 table.

Columns:

- `Symbol`
- `Exchange`
- `Name`
- `InstrumentType` (`Stock` or `ETF`)
- `IsActive`
- `DateAdded`
- `DateRemoved`
- `RemovalReason`
- `UniqueID`

Each active listing keeps one current row. When a listing disappears or its metadata changes, the old row is closed out and a new row is inserted when needed. `UniqueID` is a stable hash derived from listing metadata instead of the ticker symbol, which helps when a ticker is reused later.

`RemovalReason` values are enriched when available and normalized to one of:

- `Acquisition/Merger/Privatization`
- `Bankruptcy`
- `Regulatory issue`
- `Other delisting reason`

If a specific reason cannot be sourced, the row is still closed with `reason unavailable`.

## Date integrity

`data/status.json` stores the explicit `last_successful_run_date` value. The workflow and audit trail do not rely on filesystem timestamps.

## Running locally

```bash
python update_master_database.py
```

The script uses the NASDAQ stock screener endpoint first and falls back to the Nasdaq Trader symbol-directory files if needed.

Optional: set `FMP_API_KEY` to enrich delisting reasons from the FMP Delisted API.

```bash
export FMP_API_KEY=your_api_key_here
python update_master_database.py
```

## Querying listings for a specific date

A row was active on a date when:

- `DateAdded <= target_date`
- and (`DateRemoved` is blank or `DateRemoved > target_date`)

Example:

```python
import csv
from datetime import date

target_date = date.fromisoformat("2026-05-07")

with open("data/master_database.csv", newline="", encoding="utf-8") as handle:
    reader = csv.DictReader(handle)
    active_rows = [
        row
        for row in reader
        if row["DateAdded"] <= target_date.isoformat()
        and (not row["DateRemoved"] or row["DateRemoved"] > target_date.isoformat())
    ]

print(f"{len(active_rows)} listings were active on {target_date.isoformat()}")
```

## Automation

`.github/workflows/daily_update.yml` runs the updater every weekday at `21:00 UTC`, then commits and pushes any changed CSV/JSON files back to the repository.
