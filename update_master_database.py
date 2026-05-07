#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import date
from io import StringIO
from pathlib import Path
from typing import Callable, Dict, Iterable, List
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

EXCHANGE_CODES = {
    "nasdaq": "NASDAQ",
    "nyse": "NYSE",
    "amex": "AMEX",
}

RAW_FIELDNAMES = ["Exchange", "Symbol", "Name", "InstrumentType", "FirstIpoDate", "Source"]
MASTER_FIELDNAMES = [
    "Symbol",
    "Exchange",
    "Name",
    "InstrumentType",
    "IsActive",
    "DateAdded",
    "DateRemoved",
    "RemovalReason",
    "UniqueID",
]
REQUEST_HEADERS = {
    "Accept": "application/json, text/csv, text/plain, */*",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
    "User-Agent": "ticker-chronicle/1.0 (+https://github.com/mrft/ticker-chronicle)",
}


def _canonical_exchange(exchange: str) -> str:
    return EXCHANGE_CODES[exchange.lower()]


def _normalize_name(name: str) -> str:
    return " ".join((name or "").strip().split())


def _truthy_etf(value: str) -> bool:
    return str(value or "").strip().upper() in {"Y", "YES", "TRUE", "ETF"}


def _normalize_listing(
    exchange: str,
    symbol: str,
    name: str,
    instrument_type: str,
    first_ipo_date: str = "",
    source: str = "",
) -> Dict[str, str]:
    return {
        "Exchange": _canonical_exchange(exchange),
        "Symbol": (symbol or "").strip().upper(),
        "Name": _normalize_name(name),
        "InstrumentType": "ETF" if instrument_type == "ETF" else "Stock",
        "FirstIpoDate": (first_ipo_date or "").strip(),
        "Source": source,
    }


def _generate_unique_id(listing: Dict[str, str], observed_on: str) -> str:
    seed = "|".join(
        [
            listing["Exchange"],
            listing["Name"].casefold(),
            listing["InstrumentType"],
            listing["FirstIpoDate"] or f"first-seen:{observed_on}",
        ]
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]


def _write_csv(path: Path, rows: Iterable[Dict[str, str]], fieldnames: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _fetch_text(url: str) -> str:
    request = Request(url, headers=REQUEST_HEADERS)
    with urlopen(request, timeout=60) as response:  # nosec B310 - fixed HTTPS endpoints
        return response.read().decode("utf-8-sig")


def _parse_api_payload(exchange: str, payload: str) -> List[Dict[str, str]]:
    stripped = payload.lstrip()
    if stripped.startswith("{"):
        data = json.loads(payload)
        rows = data.get("data", {}).get("rows", [])
    else:
        rows = list(csv.DictReader(StringIO(payload)))

    listings = []
    for row in rows:
        symbol = row.get("symbol") or row.get("Symbol")
        name = row.get("name") or row.get("Name")
        if not symbol or not name:
            continue
        listings.append(
            _normalize_listing(
                exchange=exchange,
                symbol=symbol,
                name=name,
                instrument_type="ETF" if _truthy_etf(row.get("etf") or row.get("ETF")) else "Stock",
                first_ipo_date=row.get("ipoyear") or row.get("ipoYear") or row.get("IPOyear") or "",
                source="nasdaq_screener_api",
            )
        )
    return listings


def _parse_symbol_directory_payload(exchange: str, payload: str) -> List[Dict[str, str]]:
    rows = csv.DictReader(StringIO(payload), delimiter="|")
    listings: List[Dict[str, str]] = []
    for row in rows:
        if exchange == "nasdaq":
            symbol = row.get("Symbol")
            name = row.get("Security Name")
        else:
            symbol = row.get("ACT Symbol")
            name = row.get("Security Name")
            code = (row.get("Exchange") or "").strip().upper()
            expected = "N" if exchange == "nyse" else "A"
            if code != expected:
                continue

        if not symbol or "File Creation Time" in symbol:
            continue
        listings.append(
            _normalize_listing(
                exchange=exchange,
                symbol=symbol,
                name=name or "",
                instrument_type="ETF" if _truthy_etf(row.get("ETF")) else "Stock",
                source="nasdaq_trader_symbol_directory",
            )
        )
    return listings


def fetch_exchange_snapshot(exchange: str) -> List[Dict[str, str]]:
    api_url = f"https://api.nasdaq.com/api/screener/stocks?exchange={exchange}&download=true"
    try:
        rows = _parse_api_payload(exchange, _fetch_text(api_url))
        if rows:
            return rows
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError):
        pass

    fallback_url = (
        "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
        if exchange == "nasdaq"
        else "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt"
    )
    rows = _parse_symbol_directory_payload(exchange, _fetch_text(fallback_url))
    if not rows:
        raise RuntimeError(f"No listings returned for {exchange}")
    return rows


def _load_master(master_path: Path) -> List[Dict[str, str]]:
    rows = _read_csv(master_path)
    master_rows: List[Dict[str, str]] = []
    for row in rows:
        master_rows.append({field: row.get(field, "") for field in MASTER_FIELDNAMES})
    return master_rows


def _current_snapshot_map(listings_by_exchange: Dict[str, List[Dict[str, str]]]) -> Dict[tuple[str, str], Dict[str, str]]:
    snapshot: Dict[tuple[str, str], Dict[str, str]] = {}
    for exchange in EXCHANGE_CODES.values():
        for row in listings_by_exchange.get(exchange, []):
            key = (row["Exchange"], row["Symbol"])
            snapshot[key] = row
    return snapshot


def _active_master_map(master_rows: List[Dict[str, str]]) -> Dict[tuple[str, str], Dict[str, str]]:
    active_rows: Dict[tuple[str, str], Dict[str, str]] = {}
    for row in master_rows:
        if row.get("IsActive", "").lower() == "true":
            active_rows[(row["Exchange"], row["Symbol"])] = row
    return active_rows


def _same_listing(left: Dict[str, str], right: Dict[str, str]) -> bool:
    return (
        left["Name"] == right["Name"]
        and left["InstrumentType"] == right["InstrumentType"]
        and left["Exchange"] == right["Exchange"]
    )


def update_master_rows(
    master_rows: List[Dict[str, str]],
    listings_by_exchange: Dict[str, List[Dict[str, str]]],
    run_date: str,
) -> List[Dict[str, str]]:
    current_snapshot = _current_snapshot_map(listings_by_exchange)
    active_rows = _active_master_map(master_rows)

    for key, master_row in active_rows.items():
        if key in current_snapshot:
            continue
        master_row["IsActive"] = "False"
        master_row["DateRemoved"] = run_date
        master_row["RemovalReason"] = f"Missing from current {master_row['Exchange']} source snapshot"

    for key, current_row in current_snapshot.items():
        active_row = active_rows.get(key)
        if active_row is None:
            master_rows.append(
                {
                    "Symbol": current_row["Symbol"],
                    "Exchange": current_row["Exchange"],
                    "Name": current_row["Name"],
                    "InstrumentType": current_row["InstrumentType"],
                    "IsActive": "True",
                    "DateAdded": run_date,
                    "DateRemoved": "",
                    "RemovalReason": "",
                    "UniqueID": _generate_unique_id(current_row, run_date),
                }
            )
            continue

        comparable_current = {
            "Name": current_row["Name"],
            "InstrumentType": current_row["InstrumentType"],
            "Exchange": current_row["Exchange"],
        }
        comparable_active = {
            "Name": active_row["Name"],
            "InstrumentType": active_row["InstrumentType"],
            "Exchange": active_row["Exchange"],
        }
        if _same_listing(comparable_current, comparable_active):
            continue

        active_row["IsActive"] = "False"
        active_row["DateRemoved"] = run_date
        active_row["RemovalReason"] = "Listing metadata changed"
        master_rows.append(
            {
                "Symbol": current_row["Symbol"],
                "Exchange": current_row["Exchange"],
                "Name": current_row["Name"],
                "InstrumentType": current_row["InstrumentType"],
                "IsActive": "True",
                "DateAdded": run_date,
                "DateRemoved": "",
                "RemovalReason": "",
                "UniqueID": _generate_unique_id(current_row, run_date),
            }
        )

    master_rows.sort(key=lambda row: (row["Exchange"], row["Symbol"], row["DateAdded"], row["UniqueID"]))
    return master_rows


def run_update(
    data_dir: Path,
    run_date: str | None = None,
    fetcher: Callable[[str], List[Dict[str, str]]] = fetch_exchange_snapshot,
) -> Dict[str, int]:
    resolved_run_date = run_date or date.today().isoformat()
    raw_dir = data_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    listings_by_exchange: Dict[str, List[Dict[str, str]]] = {}
    summary: Dict[str, int] = {}
    for exchange in EXCHANGE_CODES:
        rows = sorted(fetcher(exchange), key=lambda row: row["Symbol"])
        listings_by_exchange[_canonical_exchange(exchange)] = rows
        _write_csv(raw_dir / f"{exchange}.csv", rows, RAW_FIELDNAMES)
        summary[exchange] = len(rows)

    master_path = data_dir / "master_database.csv"
    master_rows = update_master_rows(_load_master(master_path), listings_by_exchange, resolved_run_date)
    _write_csv(master_path, master_rows, MASTER_FIELDNAMES)

    status_path = data_dir / "status.json"
    status_path.write_text(
        json.dumps({"last_successful_run_date": resolved_run_date}, indent=2) + "\n",
        encoding="utf-8",
    )

    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update exchange raw files and master listing history.")
    parser.add_argument(
        "--data-dir",
        default=str(Path(__file__).resolve().parent / "data"),
        help="Directory that contains raw snapshots, the master database, and status.json.",
    )
    parser.add_argument(
        "--today",
        help="Override the effective run date (YYYY-MM-DD) for reproducible runs.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary = run_update(Path(args.data_dir), run_date=args.today)
    counts = ", ".join(f"{exchange.upper()}={count}" for exchange, count in summary.items())
    print(f"Updated exchange listings: {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
