#!/usr/bin/env python3
"""
AURA v0.5.3.3 - FRESH 1H MARKET DATA COLLECTOR

Purpose:
    Append fresh CLOSED 1H BTCUSDT and ETHUSDT candles to the existing
    AURA raw datasets without changing historical rows.

Safety:
    - MARKET DATA ONLY
    - NO API KEY
    - NO ORDERS
    - NO TRADING
    - NO PARAMETER CHANGES
    - Existing rows are preserved
    - Duplicate timestamps are removed
    - The currently OPEN 1H candle is never appended

Source:
    Binance public Spot REST API /api/v3/klines.
"""

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

VERSION = "AURA v0.5.3.3"
API_URL = "https://api.binance.com/api/v3/klines"
INTERVAL = "1h"
DEFAULT_LIMIT = 100

SYMBOLS = {
    "BTCUSDT": r".\data\raw\BTCUSDT_1h_raw.csv",
    "ETHUSDT": r".\data\raw\ETHUSDT_1h_raw.csv",
}

# Binance kline response indices:
# 0 open time
# 1 open
# 2 high
# 3 low
# 4 close
# 5 volume
# 6 close time
# 7 quote asset volume
# 8 number of trades
# 9 taker buy base asset volume
# 10 taker buy quote asset volume
# 11 ignore


def die(message):
    print()
    print("ERROR")
    print("-" * 82)
    print(message)
    sys.exit(1)


def utc_now():
    return datetime.now(timezone.utc)


def iso_utc_from_ms(ms):
    return (
        datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def fetch_klines(symbol, limit=DEFAULT_LIMIT):
    params = urlencode(
        {
            "symbol": symbol,
            "interval": INTERVAL,
            "limit": limit,
        }
    )

    url = f"{API_URL}?{params}"

    request = Request(
        url,
        headers={
            "User-Agent": "AURA-v0.5.3.3-market-data-collector/1.0",
            "Accept": "application/json",
        },
        method="GET",
    )

    try:
        with urlopen(request, timeout=20) as response:
            payload = response.read().decode("utf-8")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        die(f"{symbol}: Binance HTTP {exc.code}: {body[:500]}")
    except URLError as exc:
        die(f"{symbol}: could not reach Binance API: {exc.reason}")
    except Exception as exc:
        die(f"{symbol}: unexpected API error: {exc}")

    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        die(f"{symbol}: Binance returned invalid JSON: {exc}")

    if not isinstance(data, list):
        die(f"{symbol}: unexpected Binance response: {data}")

    return data


def read_existing(path):
    if not path.exists():
        die(f"Raw data file does not exist: {path}")

    with path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames or []

    if not fieldnames:
        die(f"{path}: CSV has no header")

    # Existing AURA files use either open_time or timestamp.
    if "open_time_ms" in fieldnames:
        time_column = "open_time_ms"
    elif "timestamp" in fieldnames:
        time_column = "timestamp"
    elif "open_time" in fieldnames:
        time_column = "open_time"
    else:
        die(
            f"{path}: cannot identify the existing timestamp column. "
            f"Columns: {fieldnames}"
        )

    return rows, fieldnames, time_column


def row_to_aura(row, fieldnames):
    open_ms = int(row[0])
    close_ms = int(row[6])

    values = {
        "open_time_ms": str(open_ms),
        "open_time": iso_utc_from_ms(open_ms),
        "timestamp": iso_utc_from_ms(open_ms),
        "open": str(row[1]),
        "high": str(row[2]),
        "low": str(row[3]),
        "close": str(row[4]),
        "volume": str(row[5]),
        "close_time_ms": str(close_ms),
        "close_time": iso_utc_from_ms(close_ms),
    }

    # Preserve the exact existing schema.
    return {column: values.get(column, "") for column in fieldnames}


def get_existing_open_times(rows, time_column):
    result = set()

    for row in rows:
        value = str(row.get(time_column, "")).strip()

        if not value:
            continue

        try:
            if time_column in {"open_time_ms", "timestamp"}:
                if value.isdigit():
                    result.add(int(value))
                else:
                    ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    result.add(int(ts.timestamp() * 1000))
            else:
                ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
                result.add(int(ts.timestamp() * 1000))
        except Exception:
            # Do not destroy or rewrite an existing row merely because its
            # timestamp cannot be parsed.
            continue

    return result


def is_closed_kline(row, now_ms):
    # A Binance kline is closed once current time is beyond its close time.
    return int(row[6]) < now_ms


def append_new_rows(path, symbol, api_rows):
    rows, fieldnames, time_column = read_existing(path)
    existing_times = get_existing_open_times(rows, time_column)

    now_ms = int(utc_now().timestamp() * 1000)

    new_rows = []

    for kline in api_rows:
        if len(kline) < 7:
            continue

        open_ms = int(kline[0])

        # Never append the currently open candle.
        if not is_closed_kline(kline, now_ms):
            continue

        if open_ms in existing_times:
            continue

        new_rows.append(row_to_aura(kline, fieldnames))

    if new_rows:
        rows.extend(new_rows)

        # Keep chronological order.
        def sort_key(r):
            value = r.get(time_column, "")
            try:
                if time_column in {"open_time_ms", "timestamp"} and value.isdigit():
                    return int(value)
                return int(
                    datetime.fromisoformat(
                        value.replace("Z", "+00:00")
                    ).timestamp()
                    * 1000
                )
            except Exception:
                return 0

        rows.sort(key=sort_key)

        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=fieldnames,
                extrasaction="ignore",
            )
            writer.writeheader()
            writer.writerows(rows)

    return {
        "symbol": symbol,
        "path": str(path),
        "old_rows": len(rows) - len(new_rows),
        "new_rows": len(new_rows),
        "total_rows": len(rows),
        "latest_open_time": (
            iso_utc_from_ms(max(existing_times | {
                int(r[0]) for r in api_rows if len(r) >= 7 and is_closed_kline(r, now_ms)
            }))
            if (existing_times or api_rows)
            else "NONE"
        ),
        "new_open_times": [
            iso_utc_from_ms(int(k[0]))
            for k in api_rows
            if len(k) >= 7
            and is_closed_kline(k, now_ms)
            and int(k[0]) not in existing_times
        ],
    }


def main():
    parser = argparse.ArgumentParser(
        description="AURA v0.5.3.3 fresh closed 1H market data collector"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help="Number of latest 1H candles requested from Binance.",
    )
    parser.add_argument(
        "--btc",
        default=SYMBOLS["BTCUSDT"],
        help="BTC raw CSV path.",
    )
    parser.add_argument(
        "--eth",
        default=SYMBOLS["ETHUSDT"],
        help="ETH raw CSV path.",
    )
    args = parser.parse_args()

    if args.limit < 5 or args.limit > 1000:
        die("--limit must be between 5 and 1000")

    print("=" * 82)
    print(f"{VERSION} - FRESH 1H MARKET DATA COLLECTOR")
    print("=" * 82)
    print("SOURCE               : Binance public Spot market-data API")
    print("INTERVAL             : 1H")
    print("MODE                 : MARKET DATA ONLY")
    print("ORDERS               : DISABLED")
    print("TRADING              : DISABLED")
    print("OPEN CANDLE          : NEVER APPENDED")
    print("HISTORICAL DATA      : PRESERVED")
    print()

    results = []

    for symbol, path_text in [
        ("BTCUSDT", args.btc),
        ("ETHUSDT", args.eth),
    ]:
        path = Path(path_text)

        print(f"FETCHING {symbol}")
        print("-" * 82)

        api_rows = fetch_klines(symbol, args.limit)

        if not api_rows:
            die(f"{symbol}: Binance returned zero candles.")

        result = append_new_rows(path, symbol, api_rows)
        results.append(result)

        print(f"Raw file             : {path}")
        print(f"Rows added           : {result['new_rows']}")
        print(f"Total rows           : {result['total_rows']}")
        print(
            f"Latest CLOSED candle: "
            f"{result['latest_open_time']}"
        )

        if result["new_open_times"]:
            print("New candle opens:")
            for ts in result["new_open_times"]:
                print(f"  {ts}")

        print()

        # Avoid unnecessary rapid consecutive requests.
        time.sleep(0.25)

    print("=" * 82)
    print("COLLECTOR RESULT")
    print("=" * 82)

    total_new = sum(r["new_rows"] for r in results)

    for r in results:
        print(
            f"{r['symbol']:<10} "
            f"new={r['new_rows']:<4} "
            f"total={r['total_rows']}"
        )

    print()
    print(f"TOTAL NEW CLOSED 1H CANDLES : {total_new}")
    print()
    print("IMPORTANT")
    print("C0 remains frozen.")
    print("No ATR/EMA/bar-2 parameters were changed.")
    print("No orders were created.")
    print("No trading API was used.")
    print()

    if total_new == 0:
        print("STATUS : NO NEW CLOSED CANDLES TO APPEND")
        print(
            "This normally means the raw files are already up to date "
            "or Binance has not closed another 1H candle yet."
        )
    else:
        print("STATUS : FRESH MARKET DATA APPENDED")

    print("=" * 82)


if __name__ == "__main__":
    main()
