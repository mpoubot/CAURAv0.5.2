#!/usr/bin/env python3
"""
AURA v0.5.3.4
Alpaca Fresh 1H Crypto Market Data Collector

Purpose:
    Collect prospective BTC/USD and ETH/USD 1H bars from Alpaca's
    crypto market-data API without placing orders.

Safety:
    - DATA COLLECTION ONLY
    - NO trading endpoints
    - NO order placement
    - Only closed 1H candles are appended
    - Existing files are preserved
    - Duplicate timestamps are removed deterministically

Default prospective holdout start:
    2026-08-26T17:00:00Z

Output:
    ./data/holdout_alpaca/BTCUSD_1h_alpaca_holdout.csv
    ./data/holdout_alpaca/ETHUSD_1h_alpaca_holdout.csv
    ./regime_output/prospective_holdout_diagnostic/alpaca_collection_manifest.json
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

BASE_URL = "https://data.alpaca.markets/v1beta3/crypto/us/bars"

DEFAULT_START = "2026-08-26T17:00:00Z"
SYMBOLS = {
    "BTC/USD": "BTCUSD_1h_alpaca_holdout.csv",
    "ETH/USD": "ETHUSD_1h_alpaca_holdout.csv",
}

CSV_COLUMNS = [
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "trade_count",
    "vwap",
]


def die(msg):
    raise RuntimeError(msg)


def parse_utc(value):
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso_z(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def floor_hour(dt):
    return dt.replace(minute=0, second=0, microsecond=0)


def last_closed_hour(now=None):
    if now is None:
        now = datetime.now(timezone.utc)
    return floor_hour(now) - timedelta(hours=1)


def http_get(params):
    key = os.getenv("APCA_API_KEY_ID", "").strip()
    secret = os.getenv("APCA_API_SECRET_KEY", "").strip()

    headers = {"Accept": "application/json", "User-Agent": "AURA-v0.5.3.4"}
    # Alpaca's crypto historical-data client can work without keys; if keys
    # are supplied locally, use them for the REST request.
    if key and secret:
        headers["APCA-API-KEY-ID"] = key
        headers["APCA-API-SECRET-KEY"] = secret

    url = BASE_URL + "?" + urlencode(params)
    req = Request(url, headers=headers, method="GET")

    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Alpaca HTTP {e.code}: {body}")
    except URLError as e:
        raise RuntimeError(f"Alpaca connection error: {e}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid JSON from Alpaca: {e}")


def fetch_symbol(symbol, start_dt, end_dt):
    rows = []
    token = None
    page_count = 0

    while True:
        params = {
            "symbols": symbol,
            "timeframe": "1Hour",
            "start": iso_z(start_dt),
            "end": iso_z(end_dt),
            "limit": 10000,
            "sort": "asc",
        }
        if token:
            params["page_token"] = token

        payload = http_get(params)
        page_count += 1

        bars = payload.get("bars", {})
        symbol_rows = bars.get(symbol, [])

        for b in symbol_rows:
            # Alpaca timestamps are RFC3339 strings.
            ts = b.get("t")
            if not ts:
                continue
            rows.append({
                "timestamp": iso_z(parse_utc(ts)),
                "open": b.get("o"),
                "high": b.get("h"),
                "low": b.get("l"),
                "close": b.get("c"),
                "volume": b.get("v"),
                "trade_count": b.get("n"),
                "vwap": b.get("vw"),
            })

        token = payload.get("next_page_token")
        if not token:
            break

        # Defensive pause between pages.
        time.sleep(0.15)

    return rows, page_count


def read_existing(path):
    if not path.exists():
        return []

    import csv
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            return []
        missing = [c for c in CSV_COLUMNS if c not in reader.fieldnames]
        if missing:
            die(f"Existing file schema mismatch: {path} missing {missing}")
        return [{c: row.get(c, "") for c in CSV_COLUMNS} for row in reader]


def write_csv(path, rows):
    import csv
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")

    with tmp.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    tmp.replace(path)


def merge_rows(existing, fresh, cutoff):
    merged = {}

    for row in existing:
        try:
            ts = parse_utc(row["timestamp"])
        except Exception:
            continue
        if ts < cutoff:
            merged[iso_z(ts)] = {c: row.get(c, "") for c in CSV_COLUMNS}

    for row in fresh:
        try:
            ts = parse_utc(row["timestamp"])
        except Exception:
            continue

        # Never write an open/current 1H candle.
        if ts <= cutoff:
            merged[iso_z(ts)] = {c: row.get(c, "") for c in CSV_COLUMNS}

    out = [merged[k] for k in sorted(merged)]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--holdout-start", default=DEFAULT_START)
    ap.add_argument(
        "--output-dir",
        default=r".\data\holdout_alpaca",
    )
    ap.add_argument(
        "--manifest",
        default=r"./regime_output/prospective_holdout_diagnostic/alpaca_collection_manifest.json",
    )
    args = ap.parse_args()

    start_dt = parse_utc(args.holdout_start)
    cutoff = last_closed_hour()
    now = datetime.now(timezone.utc)

    if start_dt > cutoff:
        print("No completed 1H candles exist at/after the requested holdout start yet.")
        print(f"Holdout start : {iso_z(start_dt)}")
        print(f"Latest closed : {iso_z(cutoff)}")
        return 0

    outdir = Path(args.output_dir)
    manifest_path = Path(args.manifest)
    outdir.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    print("=" * 88)
    print("AURA v0.5.3.4 - ALPACA FRESH 1H MARKET DATA COLLECTOR")
    print("=" * 88)
    print("SOURCE              : Alpaca Crypto Historical Market Data")
    print("ENDPOINT            : /v1beta3/crypto/us/bars")
    print("SYMBOLS             : BTC/USD, ETH/USD")
    print("TIMEFRAME           : 1Hour")
    print("MODE                : PROSPECTIVE DATA COLLECTION ONLY")
    print("ORDERS              : DISABLED")
    print("TRADING             : DISABLED")
    print("OPEN CANDLE         : NEVER APPENDED")
    print(f"HOLDOUT START       : {iso_z(start_dt)}")
    print(f"LATEST CLOSED HOUR  : {iso_z(cutoff)}")
    print()

    manifest = {
        "agent_version": "AURA v0.5.3.4",
        "source": "Alpaca Crypto Historical Market Data",
        "endpoint": BASE_URL,
        "timeframe": "1Hour",
        "holdout_start": iso_z(start_dt),
        "collection_time_utc": iso_z(now),
        "latest_closed_hour": iso_z(cutoff),
        "orders_allowed": False,
        "trading_enabled": False,
        "symbols": {},
    }

    total_new = 0

    for symbol, filename in SYMBOLS.items():
        print(f"FETCHING {symbol}")
        print("-" * 88)

        path = outdir / filename
        existing = read_existing(path)

        fresh, pages = fetch_symbol(symbol, start_dt, cutoff)
        merged = merge_rows(existing, fresh, cutoff)

        existing_keys = set()
        for r in existing:
            try:
                existing_keys.add(iso_z(parse_utc(r["timestamp"])))
            except Exception:
                pass

        fresh_closed = []
        for r in fresh:
            try:
                if parse_utc(r["timestamp"]) <= cutoff:
                    fresh_closed.append(r)
            except Exception:
                pass

        new_rows = [
            r for r in fresh_closed
            if iso_z(parse_utc(r["timestamp"])) not in existing_keys
        ]

        write_csv(path, merged)
        total_new += len(new_rows)

        first = merged[0]["timestamp"] if merged else "NONE"
        last = merged[-1]["timestamp"] if merged else "NONE"

        print(f"Existing rows       : {len(existing):,}")
        print(f"Alpaca rows fetched : {len(fresh):,}")
        print(f"New rows appended   : {len(new_rows):,}")
        print(f"Total output rows   : {len(merged):,}")
        print(f"First output        : {first}")
        print(f"Last output         : {last}")
        print(f"Output              : {path}")
        print()

        manifest["symbols"][symbol] = {
            "output": str(path),
            "existing_rows_before": len(existing),
            "alpaca_rows_fetched": len(fresh),
            "new_rows_appended": len(new_rows),
            "total_output_rows": len(merged),
            "first_output": first,
            "last_output": last,
            "pages": pages,
        }

    manifest["total_new_rows"] = total_new
    manifest["status"] = "DATA_APPENDED" if total_new else "NO_NEW_CLOSED_BARS"

    manifest_path.write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )

    print("=" * 88)
    print("COLLECTION COMPLETE")
    print("=" * 88)
    print(f"TOTAL NEW CLOSED 1H BARS : {total_new:,}")
    print(f"MANIFEST                  : {manifest_path}")
    print("ORDERS                    : NONE")
    print("STATUS                    :", manifest["status"])
    print("=" * 88)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCollection cancelled by user.")
        sys.exit(130)
    except Exception as exc:
        print("\nAURA v0.5.3.4 ERROR")
        print("-" * 88)
        print(str(exc))
        sys.exit(1)
