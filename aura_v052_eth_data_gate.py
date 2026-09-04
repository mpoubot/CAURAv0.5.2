#!/usr/bin/env python3
"""
AURA v0.5.2 - ETHUSDT 1H DATA GATE

Purpose
-------
Acquire and validate the ETHUSDT 1-hour historical spot series required by
the frozen AURA v0.5.2 regime experiment.

This is a DATA GATE only:
- no trading
- no API key
- no strategy calculations
- no regime assignment
- no parameter optimisation

Frozen research window
----------------------
Start: 2026-01-28 00:00:00 UTC
End:   2026-08-26 17:00:00 UTC

The end timestamp is exclusive. Therefore the expected hourly bars are:
17:00 UTC on Jan 28 through 16:00 UTC on Aug 26 = 5,057 bars.

MEXC public REST API
--------------------
GET https://api.mexc.com/api/v3/klines
interval=60m
limit=500

The script deliberately uses the public endpoint and does not require
credentials. MEXC identifies /api/v3/klines as a public interface.

Outputs
-------
data/raw/ETHUSDT_1h_raw.csv
data/reports/ETHUSDT_1h_validation.json
"""

from __future__ import annotations

import csv
import json
import math
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests


# ---------------------------------------------------------------------------
# FROZEN EXPERIMENT CONSTANTS
# ---------------------------------------------------------------------------

SYMBOL = "ETHUSDT"
INTERVAL = "60m"

START_UTC = datetime(2026, 1, 28, 0, 0, 0, tzinfo=timezone.utc)
END_UTC = datetime(2026, 8, 26, 17, 0, 0, tzinfo=timezone.utc)

BAR_SECONDS = 60 * 60
BAR_MS = BAR_SECONDS * 1000
REQUEST_LIMIT = 500

EXPECTED_CANDLES = int((END_UTC - START_UTC).total_seconds() / BAR_SECONDS)

BASE_URL = "https://api.mexc.com/api/v3/klines"

MAX_RETRIES = 5
REQUEST_TIMEOUT = 30
REQUEST_PAUSE_SECONDS = 0.25


# ---------------------------------------------------------------------------
# PATHS
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
RAW_DIR = ROOT / "data" / "raw"
REPORT_DIR = ROOT / "data" / "reports"

RAW_FILE = RAW_DIR / "ETHUSDT_1h_raw.csv"
REPORT_FILE = REPORT_DIR / "ETHUSDT_1h_validation.json"


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def utc_iso_from_ms(ms: int) -> str:
    return (
        datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def parse_float(value: Any) -> float:
    x = float(value)
    if not math.isfinite(x):
        raise ValueError(f"non-finite numeric value: {value!r}")
    return x


def request_klines(session: requests.Session, start_ms: int, end_ms: int) -> list:
    params = {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "startTime": start_ms,
        "endTime": end_ms - 1,
        "limit": REQUEST_LIMIT,
    }

    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.get(
                BASE_URL,
                params=params,
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code != 200:
                last_error = (
                    f"HTTP {response.status_code}: "
                    f"{response.text[:500]}"
                )

                # Retry transient server/rate-limit failures.
                if response.status_code in (429, 500, 502, 503, 504):
                    time.sleep(min(2 ** (attempt - 1), 8))
                    continue

                raise RuntimeError(last_error)

            payload = response.json()

            if not isinstance(payload, list):
                raise RuntimeError(
                    f"Unexpected MEXC response: {json.dumps(payload)[:1000]}"
                )

            return payload

        except (requests.RequestException, ValueError) as exc:
            last_error = repr(exc)
            if attempt < MAX_RETRIES:
                time.sleep(min(2 ** (attempt - 1), 8))
            else:
                break

    raise RuntimeError(
        f"MEXC request failed after {MAX_RETRIES} attempts: {last_error}"
    )


def normalise_row(raw: list) -> dict:
    """
    MEXC kline payloads can contain additional fields. We only retain the
    fields required for the AURA 1H OHLCV research dataset.

    Expected first six fields:
      0 open time (ms)
      1 open
      2 high
      3 low
      4 close
      5 volume

    Close time is derived from the 60-minute bar interval rather than
    trusting an optional exchange-specific field.
    """
    if len(raw) < 6:
        raise ValueError(f"Kline row has fewer than 6 fields: {raw!r}")

    open_ms = int(raw[0])

    return {
        "timestamp": utc_iso_from_ms(open_ms),
        "open_time_ms": open_ms,
        "close_time": utc_iso_from_ms(open_ms + BAR_MS - 1),
        "open": parse_float(raw[1]),
        "high": parse_float(raw[2]),
        "low": parse_float(raw[3]),
        "close": parse_float(raw[4]),
        "volume": parse_float(raw[5]),
    }


# ---------------------------------------------------------------------------
# VALIDATION
# ---------------------------------------------------------------------------

def validate(rows: list[dict]) -> tuple[dict, list[str]]:
    errors: list[str] = []
    checks: dict[str, str] = {}

    timestamps = [r["open_time_ms"] for r in rows]

    # Row structure.
    required = {
        "timestamp",
        "open_time_ms",
        "close_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
    }
    row_structure_ok = all(required.issubset(r.keys()) for r in rows)
    checks["row_structure"] = "PASS" if row_structure_ok else "FAIL"
    if not row_structure_ok:
        errors.append("One or more rows have missing required fields.")

    # Strict order.
    strict_order_ok = all(
        timestamps[i] < timestamps[i + 1]
        for i in range(len(timestamps) - 1)
    )
    checks["strict_timestamp_order"] = (
        "PASS" if strict_order_ok else "FAIL"
    )
    if not strict_order_ok:
        errors.append("Timestamps are not strictly increasing.")

    # Duplicates.
    duplicate_count = len(timestamps) - len(set(timestamps))
    checks["duplicate_timestamps"] = (
        "PASS" if duplicate_count == 0 else "FAIL"
    )
    if duplicate_count:
        errors.append(f"Duplicate timestamps: {duplicate_count}")

    # Expected first timestamp.
    first_ok = (
        len(rows) > 0
        and timestamps[0] == int(START_UTC.timestamp() * 1000)
    )
    checks["expected_first_timestamp"] = "PASS" if first_ok else "FAIL"
    if not first_ok:
        errors.append(
            "First timestamp does not equal the frozen experiment start."
        )

    # Expected last timestamp.
    expected_last_ms = int(END_UTC.timestamp() * 1000) - BAR_MS
    last_ok = (
        len(rows) > 0
        and timestamps[-1] == expected_last_ms
    )
    checks["expected_last_timestamp"] = "PASS" if last_ok else "FAIL"
    if not last_ok:
        errors.append(
            "Last timestamp does not equal the frozen experiment end - 1 bar."
        )

    # Expected count.
    expected_count_ok = len(rows) == EXPECTED_CANDLES
    checks["expected_count_matches"] = (
        "PASS" if expected_count_ok else "FAIL"
    )
    if not expected_count_ok:
        errors.append(
            f"Expected {EXPECTED_CANDLES} candles, got {len(rows)}."
        )

    # 1H continuity.
    continuity_ok = all(
        timestamps[i + 1] - timestamps[i] == BAR_MS
        for i in range(len(timestamps) - 1)
    )
    checks["1h_continuity"] = "PASS" if continuity_ok else "FAIL"
    if not continuity_ok:
        errors.append("One or more 1H gaps or irregular intervals detected.")

    # OHLCV validity.
    invalid_rows: list[str] = []

    for index, r in enumerate(rows):
        try:
            o = r["open"]
            h = r["high"]
            l = r["low"]
            c = r["close"]
            v = r["volume"]

            valid_numbers = all(
                math.isfinite(x) for x in (o, h, l, c, v)
            )

            valid_positive_prices = all(
                x > 0 for x in (o, h, l, c)
            )

            valid_volume = v >= 0

            valid_ohlc_relationship = (
                l <= o <= h
                and l <= c <= h
                and l <= h
            )

            if not (
                valid_numbers
                and valid_positive_prices
                and valid_volume
                and valid_ohlc_relationship
            ):
                invalid_rows.append(
                    f"index={index}, timestamp={r.get('timestamp')}"
                )

        except Exception:
            invalid_rows.append(
                f"index={index}, timestamp={r.get('timestamp')}"
            )

    checks["valid_open"] = (
        "PASS"
        if all(math.isfinite(r["open"]) and r["open"] > 0 for r in rows)
        else "FAIL"
    )
    checks["valid_high"] = (
        "PASS"
        if all(math.isfinite(r["high"]) and r["high"] > 0 for r in rows)
        else "FAIL"
    )
    checks["valid_low"] = (
        "PASS"
        if all(math.isfinite(r["low"]) and r["low"] > 0 for r in rows)
        else "FAIL"
    )
    checks["valid_close"] = (
        "PASS"
        if all(math.isfinite(r["close"]) and r["close"] > 0 for r in rows)
        else "FAIL"
    )
    checks["valid_volume"] = (
        "PASS"
        if all(math.isfinite(r["volume"]) and r["volume"] >= 0 for r in rows)
        else "FAIL"
    )
    checks["valid_ohlc_relationship"] = (
        "PASS" if not invalid_rows else "FAIL"
    )

    if invalid_rows:
        errors.append(
            f"Invalid OHLCV rows: {len(invalid_rows)}"
        )

    return {
        "checks": checks,
        "errors": errors,
        "invalid_ohlcv_rows": len(invalid_rows),
        "duplicate_timestamps": duplicate_count,
    }, errors


# ---------------------------------------------------------------------------
# MAIN ACQUISITION
# ---------------------------------------------------------------------------

def main() -> int:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    start_ms = int(START_UTC.timestamp() * 1000)
    end_ms = int(END_UTC.timestamp() * 1000)

    print()
    print("=" * 58)
    print(" AURA v0.5.2 - ETH DATA ACQUISITION")
    print("=" * 58)
    print(f"Symbol:       {SYMBOL}")
    print(f"Interval:     {INTERVAL}")
    print(f"Start:        {START_UTC.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    print(f"End:          {END_UTC.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    print(f"Expected:     {EXPECTED_CANDLES} candles")
    print()
    print("Mode: PUBLIC MEXC REST API - NO API KEY")
    print()

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "AURA-v0.5.2-research-data-gate/1.0",
            "Accept": "application/json",
        }
    )

    raw_rows: list[list] = []
    cursor_ms = start_ms
    request_number = 0

    try:
        while cursor_ms < end_ms:
            request_number += 1

            print(
                f"Request {request_number:03d} | "
                f"from {utc_iso_from_ms(cursor_ms)}"
            )

            batch = request_klines(session, cursor_ms, end_ms)

            if not batch:
                raise RuntimeError(
                    "MEXC returned an empty batch before the frozen "
                    "end timestamp was reached."
                )

            raw_rows.extend(batch)

            first_ms = int(batch[0][0])
            last_ms = int(batch[-1][0])

            print(
                f"  received {len(batch):3d} candles | "
                f"first: {utc_iso_from_ms(first_ms)} | "
                f"last:  {utc_iso_from_ms(last_ms)}"
            )

            # Defensive pagination checks.
            if last_ms < cursor_ms:
                raise RuntimeError(
                    "MEXC pagination moved backwards."
                )

            next_cursor = last_ms + BAR_MS

            if next_cursor <= cursor_ms:
                raise RuntimeError(
                    "MEXC pagination did not advance."
                )

            cursor_ms = next_cursor

            time.sleep(REQUEST_PAUSE_SECONDS)

            # Safety stop against an unexpectedly huge response stream.
            if request_number > 100:
                raise RuntimeError(
                    "Safety stop: more than 100 API requests were required."
                )

    except Exception as exc:
        print()
        print("=" * 58)
        print(" AURA v0.5.2 - DATA GATE ERROR")
        print("=" * 58)
        print(str(exc))
        print()
        print("DATA GATE: FAIL")
        return 1

    # Normalise and deduplicate only for the purpose of detecting duplicates.
    # We do NOT silently repair or fill the dataset.
    try:
        rows = [normalise_row(x) for x in raw_rows]
    except Exception as exc:
        print()
        print("DATA GATE: FAIL")
        print(f"Normalization error: {exc}")
        return 1

    report, errors = validate(rows)

    # Write raw CSV only after successful validation of the structural rows.
    # The file is still the exact acquired series; no missing bars are filled.
    with RAW_FILE.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "timestamp",
                "open_time_ms",
                "close_time",
                "open",
                "high",
                "low",
                "close",
                "volume",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    final_pass = len(errors) == 0

    report_payload = {
        "experiment": "AURA v0.5.2",
        "stage": "ETHUSDT 1H DATA GATE",
        "mode": "RESEARCH ONLY - NO ORDERS",
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "api": {
            "base_url": BASE_URL,
            "endpoint": "/api/v3/klines",
            "authenticated": False,
            "limit": REQUEST_LIMIT,
        },
        "frozen_window": {
            "start_utc": START_UTC.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_utc_exclusive": END_UTC.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "bar_seconds": BAR_SECONDS,
            "expected_candles": EXPECTED_CANDLES,
        },
        "acquisition": {
            "requests": request_number,
            "candles_received": len(rows),
        },
        "validation": report,
        "final_data_gate": "PASS" if final_pass else "FAIL",
        "eligible_for": (
            "AURA v0.5.2 regime builder"
            if final_pass
            else "NONE"
        ),
        "output": {
            "raw_csv": str(RAW_FILE),
            "validation_json": str(REPORT_FILE),
        },
    }

    REPORT_FILE.write_text(
        json.dumps(report_payload, indent=2),
        encoding="utf-8",
    )

    print()
    print("=" * 58)
    print(" AURA v0.5.2 - DATA GATE RESULT")
    print("=" * 58)
    print(f"Candles:              {len(rows)}")
    print(f"Expected candles:     {EXPECTED_CANDLES}")
    print(f"Requests:             {request_number}")
    print(f"Duplicates:           {report['duplicate_timestamps']}")
    print(f"Interval errors:      {0 if report['checks']['1h_continuity'] == 'PASS' else 'PRESENT'}")
    print(f"Invalid OHLCV rows:   {report['invalid_ohlcv_rows']}")
    print()
    print("CHECKS")
    print("-" * 58)

    for name, status in report["checks"].items():
        print(f"{name:<35} {status}")

    print()
    print("-" * 58)
    print(
        f"FINAL DATA GATE: "
        f"{'PASS' if final_pass else 'FAIL'}"
    )

    if final_pass:
        print()
        print(
            "Dataset is eligible for the next AURA v0.5.2 stage."
        )
    else:
        print()
        print("Dataset is NOT eligible for the next stage.")
        print()
        print("FAIL REASONS:")
        for error in errors:
            print(f"  - {error}")

    print()
    print(f"Raw data:  {RAW_FILE}")
    print(f"Report:    {REPORT_FILE}")
    print()

    return 0 if final_pass else 1


if __name__ == "__main__":
    sys.exit(main())
