#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
from datetime import date
from io import StringIO
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus
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
FMP_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": REQUEST_HEADERS["User-Agent"],
}
KNOWN_DELISTING_CATEGORIES = (
    "Acquisition/Merger/Privatization",
    "Bankruptcy",
    "Regulatory issue",
    "Other delisting reason",
)
ReasonResolver = Callable[[Dict[str, str], str], str]


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


def _generate_unique_id(listing: Dict[str, str]) -> str:
    ipo_date = (listing.get("FirstIpoDate") or "").strip()
    seed = "|".join(
        [
            listing["Exchange"],
            listing["Name"].casefold(),
            listing["InstrumentType"],
            ipo_date,
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


def _fetch_json(url: str, headers: Optional[Dict[str, str]] = None) -> object:
    request = Request(url, headers=headers or REQUEST_HEADERS)
    with urlopen(request, timeout=60) as response:  # nosec B310 - fixed HTTPS endpoints
        return json.loads(response.read().decode("utf-8-sig"))


def _parse_iso_date(value: str) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _categorize_delisting_reason(reason_text: str) -> str:
    normalized = (reason_text or "").casefold()
    acquisition_patterns = (
        "acqui",
        "merger",
        "merged",
        "buyout",
        "going private",
        "privat",
    )
    bankruptcy_patterns = (
        "bankrupt",
        "chapter 11",
        "chapter 7",
        "insolv",
        "liquidat",
    )
    regulatory_patterns = (
        "regulator",
        "regulatory",
        "compliance",
        "non-compliance",
        "listing standard",
        "listing requirement",
        "listing qualifications",
        "sec ",
        "sec.",
        "exchange rules",
    )

    if any(token in normalized for token in acquisition_patterns):
        return "Acquisition/Merger/Privatization"
    if any(token in normalized for token in bankruptcy_patterns):
        return "Bankruptcy"
    if any(token in normalized for token in regulatory_patterns):
        return "Regulatory issue"
    return "Other delisting reason"


def normalize_delisting_reason(reason_text: str, source: str) -> str:
    compact_reason = re.sub(r"\s+", " ", (reason_text or "").strip())
    category = _categorize_delisting_reason(compact_reason)
    if category not in KNOWN_DELISTING_CATEGORIES:
        category = "Other delisting reason"
    if compact_reason:
        return f"{category}: {compact_reason} (source: {source})"
    return f"{category}: Details unavailable (source: {source})"


def _fmp_record_matches_exchange(record: Dict[str, str], exchange: str) -> bool:
    expected = exchange.upper()
    value = (
        (record.get("exchange") or "")
        + " "
        + (record.get("exchangeShortName") or "")
        + " "
        + (record.get("exchangeSymbol") or "")
    ).upper()
    return expected in value if value.strip() else True


def _extract_fmp_delisting_reason(record: Dict[str, str]) -> str:
    candidate_fields = (
        "reason",
        "delistingReason",
        "delistedReason",
        "comment",
        "description",
    )
    for field in candidate_fields:
        value = str(record.get(field) or "").strip()
        if value:
            return value
    return ""


def fetch_fmp_delisting_reason(symbol: str, exchange: str, run_date: str) -> Optional[str]:
    api_key = os.getenv("FMP_API_KEY", "").strip()
    if not api_key:
        return None

    encoded_symbol = quote_plus(symbol)
    urls = [
        f"https://financialmodelingprep.com/stable/delisted-companies?symbol={encoded_symbol}&apikey={api_key}",
        f"https://financialmodelingprep.com/api/v3/delisted-companies?symbol={encoded_symbol}&apikey={api_key}",
    ]

    records: List[Dict[str, str]] = []
    for url in urls:
        try:
            payload = _fetch_json(url, headers=FMP_HEADERS)
        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError):
            continue

        if isinstance(payload, list):
            records = [row for row in payload if isinstance(row, dict)]
            if records:
                break

    if not records:
        return None

    target_date = _parse_iso_date(run_date)
    eligible_records: List[Tuple[date, Dict[str, str]]] = []
    for record in records:
        if (record.get("symbol") or "").strip().upper() != symbol.upper():
            continue
        if not _fmp_record_matches_exchange(record, exchange):
            continue
        record_date = _parse_iso_date(
            str(record.get("delistedDate") or record.get("delistingDate") or record.get("date") or "")
        )
        if record_date is None:
            continue
        if target_date and record_date > target_date:
            continue
        eligible_records.append((record_date, record))

    if not eligible_records:
        return None

    _, selected_record = sorted(eligible_records, key=lambda item: item[0], reverse=True)[0]
    raw_reason = _extract_fmp_delisting_reason(selected_record)
    return normalize_delisting_reason(raw_reason, "FMP Delisted API")


def resolve_delisting_reason(master_row: Dict[str, str], run_date: str) -> str:
    base_reason = f"Missing from current {master_row['Exchange']} source snapshot"
    external_reason = fetch_fmp_delisting_reason(master_row["Symbol"], master_row["Exchange"], run_date)
    if external_reason:
        return external_reason
    return f"{base_reason}; reason unavailable"


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
    reason_resolver: ReasonResolver = resolve_delisting_reason,
) -> List[Dict[str, str]]:
    current_snapshot = _current_snapshot_map(listings_by_exchange)
    active_rows = _active_master_map(master_rows)

    for key, master_row in active_rows.items():
        if key in current_snapshot:
            continue
        master_row["IsActive"] = "False"
        master_row["DateRemoved"] = run_date
        master_row["RemovalReason"] = reason_resolver(master_row, run_date)

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
                    "UniqueID": _generate_unique_id(current_row),
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
                "UniqueID": _generate_unique_id(current_row),
            }
        )

    master_rows.sort(key=lambda row: (row["Exchange"], row["Symbol"], row["DateAdded"], row["UniqueID"]))
    return master_rows


def run_update(
    data_dir: Path,
    run_date: str | None = None,
    fetcher: Callable[[str], List[Dict[str, str]]] = fetch_exchange_snapshot,
    reason_resolver: ReasonResolver = resolve_delisting_reason,
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
    master_rows = update_master_rows(
        _load_master(master_path),
        listings_by_exchange,
        resolved_run_date,
        reason_resolver=reason_resolver,
    )
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
