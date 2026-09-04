#!/usr/bin/env python3
"""
AURA v0.5.3.5
ALPACA WARM-UP + PROSPECTIVE HOLDOUT DATA BUILDER

Purpose
-------
Build a clean prospective dataset for AURA v0.5.3 without contaminating the
chronological holdout.

The script fetches:
    1) indicator warm-up bars BEFORE the frozen holdout boundary
    2) prospective holdout bars AT/AFTER the frozen boundary

Warm-up bars are used ONLY for indicator state (EMA50 / ATR14 / bar history).
Their returns are NEVER counted as prospective observations.

Safety
------
- MARKET DATA ONLY
- NO trading endpoint
- NO order placement
- Existing output files are replaced only in this dedicated Alpaca directory
- Current/open 1H candle is never written
- Frozen holdout boundary is preserved

Default boundary
----------------
2026-08-26T17:00:00Z

Default warm-up
---------------
120 completed 1H bars before the holdout boundary.

Outputs
-------
data/holdout_alpaca/
    BTCUSD_1h_alpaca_warmup.csv
    BTCUSD_1h_alpaca_holdout.csv
    BTCUSD_1h_alpaca_prospective.csv
    ETHUSD_1h_alpaca_warmup.csv
    ETHUSD_1h_alpaca_holdout.csv
    ETHUSD_1h_alpaca_prospective.csv

regime_output/prospective_holdout_diagnostic/
    alpaca_warmup_collection_manifest.json
"""

import argparse
import csv
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

DEFAULT_HOLDOUT_START = "2026-08-26T17:00:00Z"
DEFAULT_WARMUP_HOURS = 120

SYMBOLS = {
    "BTC/USD": "BTCUSD",
    "ETH/USD": "ETHUSD",
}

COLUMNS = [
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "trade_count",
    "vwap",
]

PROSPECTIVE_COLUMNS = COLUMNS + ["phase"]


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


def latest_closed_hour(now=None):
    if now is None:
        now = datetime.now(timezone.utc)
    return floor_hour(now) - timedelta(hours=1)


def auth_headers():
    key = os.getenv("APCA_API_KEY_ID", "").strip()
    secret = os.getenv("APCA_API_SECRET_KEY", "").strip()

    headers = {
        "Accept": "application/json",
        "User-Agent": "AURA-v0.5.3.5",
    }

    if key and secret:
        headers["APCA-API-KEY-ID"] = key
        headers["APCA-API-SECRET-KEY"] = secret

    return headers


def fetch_bars(symbol, start_dt, end_dt):
    rows = []
    page_token = None
    pages = 0

    while True:
        params = {
            "symbols": symbol,
            "timeframe": "1Hour",
            "start": iso_z(start_dt),
            "end": iso_z(end_dt),
            "limit": 10000,
            "sort": "asc",
        }

        if page_token:
            params["page_token"] = page_token

        req = Request(
            BASE_URL + "?" + urlencode(params),
            headers=auth_headers(),
            method="GET",
        )

        try:
            with urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Alpaca HTTP {e.code}: {body}")
        except URLError as e:
            raise RuntimeError(f"Alpaca connection error: {e}")

        pages += 1

        for bar in payload.get("bars", {}).get(symbol, []):
            ts = bar.get("t")
            if not ts:
                continue

            rows.append({
                "timestamp": iso_z(parse_utc(ts)),
                "open": bar.get("o"),
                "high": bar.get("h"),
                "low": bar.get("l"),
                "close": bar.get("c"),
                "volume": bar.get("v"),
                "trade_count": bar.get("n"),
                "vwap": bar.get("vw"),
            })

        page_token = payload.get("next_page_token")
        if not page_token:
            break

        time.sleep(0.15)

    return rows, pages


def dedupe_closed(rows, cutoff):
    unique = {}

    for row in rows:
        try:
            ts = parse_utc(row["timestamp"])
        except Exception:
            continue

        # A 1H bar stamped at T represents [T, T+1h).
        # Therefore the bar is closed when T <= latest closed-hour start.
        if ts <= cutoff:
            unique[iso_z(ts)] = row

    return [unique[k] for k in sorted(unique)]


def write_csv(path, rows, columns):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")

    with tmp.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)

    tmp.replace(path)


def with_phase(rows, phase):
    out = []
    for row in rows:
        x = dict(row)
        x["phase"] = phase
        out.append(x)
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--holdout-start",
        default=DEFAULT_HOLDOUT_START,
    )
    parser.add_argument(
        "--warmup-hours",
        type=int,
        default=DEFAULT_WARMUP_HOURS,
    )
    parser.add_argument(
        "--output-dir",
        default=r"./data/holdout_alpaca",
    )
    parser.add_argument(
        "--manifest",
        default=r"./regime_output/prospective_holdout_diagnostic/alpaca_warmup_collection_manifest.json",
    )
    args = parser.parse_args()

    if args.warmup_hours < 50:
        die("warmup-hours must be at least 50 for EMA50 context.")

    holdout_start = parse_utc(args.holdout_start)
    cutoff = latest_closed_hour()
    warmup_start = holdout_start - timedelta(hours=args.warmup_hours)

    if holdout_start > cutoff:
        print("No completed holdout bars exist yet.")
        print(f"Holdout start      : {iso_z(holdout_start)}")
        print(f"Latest closed hour : {iso_z(cutoff)}")
        return 0

    output_dir = Path(args.output_dir)
    manifest_path = Path(args.manifest)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    print("=" * 88)
    print("AURA v0.5.3.5 - ALPACA WARM-UP + PROSPECTIVE HOLDOUT BUILDER")
    print("=" * 88)
    print("SOURCE              : Alpaca Crypto Historical Market Data")
    print("TIMEFRAME           : 1Hour")
    print("MODE                : PROSPECTIVE RESEARCH ONLY - NO ORDERS")
    print("TRADING             : DISABLED")
    print("OPEN CANDLE         : NEVER WRITTEN")
    print(f"WARM-UP START       : {iso_z(warmup_start)}")
    print(f"HOLDOUT START       : {iso_z(holdout_start)}")
    print(f"LATEST CLOSED HOUR  : {iso_z(cutoff)}")
    print(f"WARM-UP HOURS       : {args.warmup_hours}")
    print()
    print("RULE: WARM-UP DATA MAY INITIALIZE INDICATORS.")
    print("RULE: WARM-UP RETURNS MUST NEVER COUNT AS HOLDOUT EVIDENCE.")
    print()

    manifest = {
        "agent_version": "AURA v0.5.3.5",
        "source": "Alpaca Crypto Historical Market Data",
        "endpoint": BASE_URL,
        "timeframe": "1Hour",
        "warmup_hours": args.warmup_hours,
        "warmup_start": iso_z(warmup_start),
        "holdout_start": iso_z(holdout_start),
        "latest_closed_hour": iso_z(cutoff),
        "orders_allowed": False,
        "trading_enabled": False,
        "symbols": {},
    }

    total_warmup = 0
    total_holdout = 0

    for symbol, stem in SYMBOLS.items():
        print(f"FETCHING {symbol}")
        print("-" * 88)

        # Fetch the entire warm-up + holdout window in one chronological request.
        fresh, pages = fetch_bars(symbol, warmup_start, cutoff)
        fresh = dedupe_closed(fresh, cutoff)

        warmup = [
            r for r in fresh
            if parse_utc(r["timestamp"]) < holdout_start
        ]
        holdout = [
            r for r in fresh
            if parse_utc(r["timestamp"]) >= holdout_start
        ]

        prospective = with_phase(warmup, "WARMUP") + with_phase(holdout, "HOLDOUT")

        warmup_path = output_dir / f"{stem}_1h_alpaca_warmup.csv"
        holdout_path = output_dir / f"{stem}_1h_alpaca_holdout.csv"
        prospective_path = output_dir / f"{stem}_1h_alpaca_prospective.csv"

        write_csv(warmup_path, warmup, COLUMNS)
        write_csv(holdout_path, holdout, COLUMNS)
        write_csv(prospective_path, prospective, PROSPECTIVE_COLUMNS)

        total_warmup += len(warmup)
        total_holdout += len(holdout)

        print(f"Alpaca rows fetched : {len(fresh):,}")
        print(f"Warm-up rows        : {len(warmup):,}")
        print(f"Holdout rows        : {len(holdout):,}")
        print(f"Combined rows       : {len(prospective):,}")
        print(
            f"Warm-up range       : "
            f"{warmup[0]['timestamp'] if warmup else 'NONE'}"
            f" -> {warmup[-1]['timestamp'] if warmup else 'NONE'}"
        )
        print(
            f"Holdout range       : "
            f"{holdout[0]['timestamp'] if holdout else 'NONE'}"
            f" -> {holdout[-1]['timestamp'] if holdout else 'NONE'}"
        )
        print(f"Warm-up file        : {warmup_path}")
        print(f"Holdout file        : {holdout_path}")
        print(f"Prospective file    : {prospective_path}")
        print()

        manifest["symbols"][symbol] = {
            "pages": pages,
            "fetched_closed_rows": len(fresh),
            "warmup_rows": len(warmup),
            "holdout_rows": len(holdout),
            "combined_rows": len(prospective),
            "warmup_first": warmup[0]["timestamp"] if warmup else None,
            "warmup_last": warmup[-1]["timestamp"] if warmup else None,
            "holdout_first": holdout[0]["timestamp"] if holdout else None,
            "holdout_last": holdout[-1]["timestamp"] if holdout else None,
            "warmup_file": str(warmup_path),
            "holdout_file": str(holdout_path),
            "prospective_file": str(prospective_path),
        }

    manifest["total_warmup_rows"] = total_warmup
    manifest["total_holdout_rows"] = total_holdout
    manifest["total_rows"] = total_warmup + total_holdout
    manifest["status"] = (
        "READY_FOR_PROSPECTIVE_VALIDATION"
        if total_holdout > 0
        else "NO_HOLDOUT_BARS"
    )

    manifest_path.write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )

    print("=" * 88)
    print("AURA v0.5.3.5 COLLECTION COMPLETE")
    print("=" * 88)
    print(f"TOTAL WARM-UP BARS   : {total_warmup:,}")
    print(f"TOTAL HOLDOUT BARS   : {total_holdout:,}")
    print(f"TOTAL BARS           : {total_warmup + total_holdout:,}")
    print(f"MANIFEST             : {manifest_path}")
    print("ORDERS               : NONE")
    print("STATUS               :", manifest["status"])
    print("=" * 88)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCollection cancelled by user.")
        sys.exit(130)
    except Exception as exc:
        print("\nAURA v0.5.3.5 ERROR")
        print("-" * 88)
        print(str(exc))
        sys.exit(1)
