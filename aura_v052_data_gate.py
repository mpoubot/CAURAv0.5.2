#!/usr/bin/env python3
"""
AURA v0.5.2 - Historical Data Gate
----------------------------------

Purpose
-------
Acquire and validate the complete BTCUSDT 1H dataset required by the
pre-registered AURA v0.5.2 experiment.

IMPORTANT:
- The research window is frozen in this file.
- "1H" is represented to MEXC as interval="60m".
- No API credentials are used.
- The existing 500-row file is NOT used as an input to the experiment.
- Data are first written to a candidate file.
- The official raw CSV is replaced ONLY after the complete data gate passes.

Frozen research window
----------------------
Start: 2026-01-28 00:00:00 UTC
End:   2026-08-26 17:00:00 UTC

The END timestamp is an exclusive boundary for the experiment.
Therefore the final expected candle has open_time:
2026-08-26 16:00:00 UTC

Dependencies
------------
Python 3.10+ and requests.

Run
---
python aura_v052_data_gate.py
"""

from __future__ import annotations

import csv
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


# ============================================================
# AURA v0.5.2 - FROZEN EXPERIMENT PARAMETERS
# ============================================================

SYMBOL = "BTCUSDT"

# MEXC's API representation of 1-hour candles.
# This is NOT a change to the research timeframe.
INTERVAL = "60m"

INTERVAL_MS = 60 * 60 * 1000

START_UTC = "2026-01-28T00:00:00Z"
END_UTC = "2026-08-26T17:00:00Z"

# MEXC documents a maximum of 1000 klines per request.
REQUEST_LIMIT = 1000

BASE_URL = "https://api.mexc.com"
KLINES_ENDPOINT = "/api/v3/klines"

# Small delay between requests. The experiment uses only public market data.
REQUEST_DELAY_SECONDS = 0.25

# Retry settings for temporary server/rate-limit conditions.
MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 2.0

# ============================================================
# PATHS
# ============================================================

ROOT = Path(__file__).resolve().parent

RAW_DIR = ROOT / "data" / "raw"
REPORT_DIR = ROOT / "data" / "reports"

FINAL_RAW_PATH = RAW_DIR / f"{SYMBOL}_1h_raw.csv"
CANDIDATE_RAW_PATH = RAW_DIR / f"{SYMBOL}_1h_candidate.csv"
REPORT_PATH = REPORT_DIR / f"{SYMBOL}_1h_validation.json"

# ============================================================
# CSV STRUCTURE
# ============================================================

CSV_FIELDS = [
    "open_time_ms",
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time_ms",
    "close_time",
]


# ============================================================
# TIME HELPERS
# ============================================================

def parse_utc(value: str) -> datetime:
    """Parse a frozen ISO-8601 UTC timestamp."""
    if not value.endswith("Z"):
        raise ValueError(f"Expected UTC timestamp ending in Z: {value}")

    dt = datetime.fromisoformat(value[:-1] + "+00:00")

    if dt.tzinfo is None:
        raise ValueError(f"Timestamp has no timezone: {value}")

    return dt.astimezone(timezone.utc)


def dt_to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def ms_to_iso(ms: int) -> str:
    return (
        datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


START_MS = dt_to_ms(parse_utc(START_UTC))
END_MS = dt_to_ms(parse_utc(END_UTC))

# END is exclusive.
EXPECTED_COUNT = (END_MS - START_MS) // INTERVAL_MS
EXPECTED_LAST_OPEN_MS = END_MS - INTERVAL_MS


# ============================================================
# GENERAL HELPERS
# ============================================================

def print_header(title: str) -> None:
    print()
    print("=" * 58)
    print(f" {title}")
    print("=" * 58)


def atomic_write_text(path: Path, text: str) -> None:
    """Write a file atomically using a temporary file."""
    tmp = path.with_suffix(path.suffix + ".tmp")

    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)

    os.replace(tmp, path)


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    atomic_write_text(
        path,
        json.dumps(payload, indent=2, ensure_ascii=False),
    )


def safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(number):
        return None

    return number


# ============================================================
# MEXC API
# ============================================================

def request_klines(
    session: requests.Session,
    start_ms: int,
    request_number: int,
) -> list[list[Any]]:
    """
    Request one page of MEXC klines.

    startTime is inclusive.
    endTime is sent as END_MS - 1 because the AURA experiment defines
    END_UTC as an exclusive boundary.
    """

    params = {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "startTime": start_ms,
        "endTime": END_MS - 1,
        "limit": REQUEST_LIMIT,
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.get(
                BASE_URL + KLINES_ENDPOINT,
                params=params,
                timeout=30,
            )
        except requests.RequestException as exc:
            if attempt >= MAX_RETRIES:
                raise RuntimeError(
                    f"Network error after {MAX_RETRIES} attempts: {exc}"
                ) from exc

            wait = BACKOFF_BASE_SECONDS ** (attempt - 1)

            print(
                f"  Network error on attempt {attempt}/{MAX_RETRIES}: {exc}"
            )
            print(f"  Retrying in {wait:.1f}s...")

            time.sleep(wait)
            continue

        # Success
        if response.status_code == 200:
            try:
                payload = response.json()
            except ValueError as exc:
                raise RuntimeError(
                    f"MEXC returned HTTP 200 but invalid JSON on request "
                    f"{request_number}: {response.text[:500]}"
                ) from exc

            if not isinstance(payload, list):
                raise RuntimeError(
                    f"MEXC returned unexpected payload on request "
                    f"{request_number}: {payload}"
                )

            return payload

        # Rate limit
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")

            try:
                wait = float(retry_after) if retry_after else (
                    BACKOFF_BASE_SECONDS ** attempt
                )
            except ValueError:
                wait = BACKOFF_BASE_SECONDS ** attempt

            if attempt >= MAX_RETRIES:
                raise RuntimeError(
                    f"MEXC HTTP 429 after {MAX_RETRIES} attempts: "
                    f"{response.text[:500]}"
                )

            print(
                f"  MEXC rate limit (429), attempt "
                f"{attempt}/{MAX_RETRIES}. Waiting {wait:.1f}s..."
            )

            time.sleep(wait)
            continue

        # Temporary server/WAF errors
        if response.status_code in (500, 502, 503, 504):
            if attempt >= MAX_RETRIES:
                raise RuntimeError(
                    f"MEXC HTTP {response.status_code} after "
                    f"{MAX_RETRIES} attempts: {response.text[:500]}"
                )

            wait = BACKOFF_BASE_SECONDS ** attempt

            print(
                f"  MEXC HTTP {response.status_code}, attempt "
                f"{attempt}/{MAX_RETRIES}. Waiting {wait:.1f}s..."
            )

            time.sleep(wait)
            continue

        # Permanent client/API error
        raise RuntimeError(
            f"MEXC HTTP error {response.status_code}: "
            f"{response.text[:1000]}"
        )

    raise RuntimeError("Unexpected retry-loop termination.")


# ============================================================
# CONVERT API ROW
# ============================================================

def convert_api_row(row: list[Any]) -> dict[str, Any]:
    """
    Convert MEXC response:

    [0] open time
    [1] open
    [2] high
    [3] low
    [4] close
    [5] volume
    [6] close time
    [7] quote asset volume
    """

    if len(row) < 7:
        raise ValueError(
            f"Malformed MEXC kline row with only {len(row)} fields: {row}"
        )

    open_ms = int(row[0])
    close_ms = int(row[6])

    return {
        "open_time_ms": open_ms,
        "open_time": ms_to_iso(open_ms),
        "open": str(row[1]),
        "high": str(row[2]),
        "low": str(row[3]),
        "close": str(row[4]),
        "volume": str(row[5]),
        "close_time_ms": close_ms,
        "close_time": ms_to_iso(close_ms),
    }


# ============================================================
# ACQUISITION
# ============================================================

def acquire_complete_dataset() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Download the complete registered window.

    Returns:
        rows
        request_log
    """

    session = requests.Session()

    # Do not send API credentials. This is public market data.
    session.headers.update(
        {
            "User-Agent": "AURA-v0.5.2-Data-Gate/1.0",
            "Accept": "application/json",
        }
    )

    all_rows: list[dict[str, Any]] = []
    request_log: list[dict[str, Any]] = []

    current_start = START_MS
    request_number = 0

    while current_start < END_MS:
        request_number += 1

        print()
        print(
            f"Request {request_number:03d} | "
            f"from {ms_to_iso(current_start)}"
        )

        raw_batch = request_klines(
            session=session,
            start_ms=current_start,
            request_number=request_number,
        )

        if not raw_batch:
            raise RuntimeError(
                f"MEXC returned zero candles at "
                f"{ms_to_iso(current_start)} before reaching the "
                f"registered end boundary."
            )

        batch: list[dict[str, Any]] = []

        for raw_row in raw_batch:
            try:
                converted = convert_api_row(raw_row)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"Malformed candle in request {request_number}: {exc}"
                ) from exc

            # Do not allow candles outside the registered experiment window.
            if START_MS <= converted["open_time_ms"] < END_MS:
                batch.append(converted)

        if not batch:
            raise RuntimeError(
                f"Request {request_number} returned data, but none of the "
                f"candles were inside the registered window."
            )

        batch.sort(key=lambda r: r["open_time_ms"])

        first_ms = batch[0]["open_time_ms"]
        last_ms = batch[-1]["open_time_ms"]

        print(
            f"  received {len(batch)} candles | "
            f"first: {ms_to_iso(first_ms)} | "
            f"last: {ms_to_iso(last_ms)}"
        )

        request_log.append(
            {
                "request": request_number,
                "requested_start": ms_to_iso(current_start),
                "returned_rows": len(raw_batch),
                "accepted_rows": len(batch),
                "first_open": ms_to_iso(first_ms),
                "last_open": ms_to_iso(last_ms),
            }
        )

        all_rows.extend(batch)

        # The API must make forward progress.
        next_start = last_ms + INTERVAL_MS

        if next_start <= current_start:
            raise RuntimeError(
                "Pagination failed to make forward progress. "
                f"current_start={ms_to_iso(current_start)}, "
                f"last_open={ms_to_iso(last_ms)}"
            )

        current_start = next_start

        if current_start < END_MS:
            time.sleep(REQUEST_DELAY_SECONDS)

    return all_rows, request_log


# ============================================================
# VALIDATION
# ============================================================

def validate_dataset(
    rows: list[dict[str, Any]],
    request_log: list[dict[str, Any]],
) -> dict[str, Any]:

    checks: dict[str, str] = {}
    counts: dict[str, int] = {}

    # --------------------------------------------------------
    # Row structure
    # --------------------------------------------------------

    required_fields_ok = all(
        all(field in row for field in CSV_FIELDS)
        for row in rows
    )

    checks["row_structure"] = "PASS" if required_fields_ok else "FAIL"

    # --------------------------------------------------------
    # Empty dataset
    # --------------------------------------------------------

    if not rows:
        checks.update(
            {
                "strict_timestamp_order": "FAIL",
                "duplicate_timestamps": "FAIL",
                "1h_continuity": "FAIL",
                "expected_count_matches": "FAIL",
                "expected_first_timestamp": "FAIL",
                "expected_last_timestamp": "FAIL",
                "valid_open": "FAIL",
                "valid_high": "FAIL",
                "valid_low": "FAIL",
                "valid_close": "FAIL",
                "valid_volume": "FAIL",
                "valid_close_time": "FAIL",
                "valid_ohlc_relationship": "FAIL",
            }
        )

        return {
            "checks": checks,
            "counts": {
                "candles": 0,
                "expected_candles": EXPECTED_COUNT,
                "duplicates": 0,
                "interval_errors": 0,
                "invalid_open": 0,
                "invalid_high": 0,
                "invalid_low": 0,
                "invalid_close": 0,
                "invalid_volume": 0,
                "invalid_close_time": 0,
                "invalid_ohlc_relationship": 0,
            },
            "request_count": len(request_log),
            "eligible": False,
        }

    # --------------------------------------------------------
    # Timestamp sequence
    # --------------------------------------------------------

    timestamps = [
        int(row["open_time_ms"])
        for row in rows
    ]

    strict_order = all(
        timestamps[i] < timestamps[i + 1]
        for i in range(len(timestamps) - 1)
    )

    checks["strict_timestamp_order"] = (
        "PASS" if strict_order else "FAIL"
    )

    duplicate_count = (
        len(timestamps) - len(set(timestamps))
    )

    counts["duplicates"] = duplicate_count

    checks["duplicate_timestamps"] = (
        "PASS" if duplicate_count == 0 else "FAIL"
    )

    # --------------------------------------------------------
    # Sort a copy for deterministic validation
    # --------------------------------------------------------

    sorted_rows = sorted(
        rows,
        key=lambda row: int(row["open_time_ms"]),
    )

    sorted_ts = [
        int(row["open_time_ms"])
        for row in sorted_rows
    ]

    # --------------------------------------------------------
    # Continuity
    # --------------------------------------------------------

    interval_errors = 0

    for i in range(len(sorted_ts) - 1):
        if sorted_ts[i + 1] - sorted_ts[i] != INTERVAL_MS:
            interval_errors += 1

    counts["interval_errors"] = interval_errors

    checks["1h_continuity"] = (
        "PASS" if interval_errors == 0 else "FAIL"
    )

    # --------------------------------------------------------
    # Expected count
    # --------------------------------------------------------

    actual_count = len(rows)
    counts["candles"] = actual_count
    counts["expected_candles"] = EXPECTED_COUNT

    checks["expected_count_matches"] = (
        "PASS" if actual_count == EXPECTED_COUNT else "FAIL"
    )

    # --------------------------------------------------------
    # Exact first/last timestamps
    # --------------------------------------------------------

    first_ok = sorted_ts[0] == START_MS
    last_ok = sorted_ts[-1] == EXPECTED_LAST_OPEN_MS

    checks["expected_first_timestamp"] = (
        "PASS" if first_ok else "FAIL"
    )

    checks["expected_last_timestamp"] = (
        "PASS" if last_ok else "FAIL"
    )

    # --------------------------------------------------------
    # OHLCV validation
    # --------------------------------------------------------

    invalid_open = 0
    invalid_high = 0
    invalid_low = 0
    invalid_close = 0
    invalid_volume = 0
    invalid_close_time = 0
    invalid_relationship = 0

    for row in sorted_rows:
        open_price = safe_float(row["open"])
        high_price = safe_float(row["high"])
        low_price = safe_float(row["low"])
        close_price = safe_float(row["close"])
        volume = safe_float(row["volume"])

        if open_price is None or open_price <= 0:
            invalid_open += 1

        if high_price is None or high_price <= 0:
            invalid_high += 1

        if low_price is None or low_price <= 0:
            invalid_low += 1

        if close_price is None or close_price <= 0:
            invalid_close += 1

        # Zero volume is technically possible in some markets, so
        # the gate rejects only negative/non-finite volume.
        if volume is None or volume < 0:
            invalid_volume += 1

        expected_close_ms = (
            int(row["open_time_ms"]) + INTERVAL_MS
        )

        if int(row["close_time_ms"]) != expected_close_ms:
            invalid_close_time += 1

        if all(
            value is not None
            for value in (
                open_price,
                high_price,
                low_price,
                close_price,
            )
        ):
            if (
                high_price < max(open_price, close_price)
                or low_price > min(open_price, close_price)
                or high_price < low_price
            ):
                invalid_relationship += 1

    counts.update(
        {
            "invalid_open": invalid_open,
            "invalid_high": invalid_high,
            "invalid_low": invalid_low,
            "invalid_close": invalid_close,
            "invalid_volume": invalid_volume,
            "invalid_close_time": invalid_close_time,
            "invalid_ohlc_relationship": invalid_relationship,
        }
    )

    checks["valid_open"] = (
        "PASS" if invalid_open == 0 else "FAIL"
    )

    checks["valid_high"] = (
        "PASS" if invalid_high == 0 else "FAIL"
    )

    checks["valid_low"] = (
        "PASS" if invalid_low == 0 else "FAIL"
    )

    checks["valid_close"] = (
        "PASS" if invalid_close == 0 else "FAIL"
    )

    checks["valid_volume"] = (
        "PASS" if invalid_volume == 0 else "FAIL"
    )

    checks["valid_close_time"] = (
        "PASS" if invalid_close_time == 0 else "FAIL"
    )

    checks["valid_ohlc_relationship"] = (
        "PASS" if invalid_relationship == 0 else "FAIL"
    )

    # --------------------------------------------------------
    # Final eligibility
    # --------------------------------------------------------

    eligible = all(
        status == "PASS"
        for status in checks.values()
    )

    return {
        "checks": checks,
        "counts": counts,
        "request_count": len(request_log),
        "eligible": eligible,
    }


# ============================================================
# SAVE CSV
# ============================================================

def write_csv(
    path: Path,
    rows: list[dict[str, Any]],
) -> None:

    path.parent.mkdir(parents=True, exist_ok=True)

    tmp = path.with_suffix(path.suffix + ".tmp")

    with open(
        tmp,
        "w",
        encoding="utf-8",
        newline="",
    ) as f:
        writer = csv.DictWriter(
            f,
            fieldnames=CSV_FIELDS,
        )

        writer.writeheader()

        for row in rows:
            writer.writerow(
                {
                    field: row[field]
                    for field in CSV_FIELDS
                }
            )

    os.replace(tmp, path)


# ============================================================
# MAIN
# ============================================================

def main() -> int:

    print_header("AURA v0.5.2 - MEXC DATA ACQUISITION")

    print(f"Symbol:              {SYMBOL}")
    print(f"Interval:            {INTERVAL}  (1H)")
    print(f"Start:               {START_UTC}")
    print(f"End (exclusive):     {END_UTC}")
    print(f"Expected candles:    {EXPECTED_COUNT}")
    print(f"Expected last open:  {ms_to_iso(EXPECTED_LAST_OPEN_MS)}")
    print()
    print("IMPORTANT:")
    print("  The existing raw CSV is NOT used as acquisition input.")
    print("  The complete window is downloaded from MEXC from scratch.")
    print("  The official raw CSV is replaced only after DATA GATE PASS.")

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    try:
        rows, request_log = acquire_complete_dataset()

        report = validate_dataset(
            rows=rows,
            request_log=request_log,
        )

    except Exception as exc:
        print_header("AURA v0.5.2 - DATA GATE ERROR")
        print(str(exc))
        print()
        print("DATA GATE: FAIL")

        error_report = {
            "experiment": "AURA v0.5.2",
            "status": "FAIL",
            "error": str(exc),
            "symbol": SYMBOL,
            "interval_api": INTERVAL,
            "interval_research": "1H",
            "start_utc": START_UTC,
            "end_utc_exclusive": END_UTC,
            "expected_candles": EXPECTED_COUNT,
        }

        save_json(REPORT_PATH, error_report)

        return 1

    # Candidate is always written before final eligibility is decided.
    write_csv(CANDIDATE_RAW_PATH, rows)

    full_report = {
        "experiment": "AURA v0.5.2",
        "status": "PASS" if report["eligible"] else "FAIL",
        "eligible_for_next_stage": report["eligible"],
        "symbol": SYMBOL,
        "interval_api": INTERVAL,
        "interval_research": "1H",
        "api_endpoint": BASE_URL + KLINES_ENDPOINT,
        "start_utc": START_UTC,
        "end_utc_exclusive": END_UTC,
        "expected_last_open_utc": ms_to_iso(
            EXPECTED_LAST_OPEN_MS
        ),
        "expected_candles": EXPECTED_COUNT,
        "request_limit": REQUEST_LIMIT,
        "requests": request_log,
        "checks": report["checks"],
        "counts": report["counts"],
        "candidate_raw_csv": str(CANDIDATE_RAW_PATH),
    }

    save_json(
        REPORT_PATH,
        full_report,
    )

    # --------------------------------------------------------
    # DATA GATE RESULT
    # --------------------------------------------------------

    print_header("AURA v0.5.2 - DATA GATE RESULT")

    print(f"Candles:              {report['counts'].get('candles', 0)}")
    print(f"Expected candles:     {EXPECTED_COUNT}")
    print(f"Requests:             {report['request_count']}")
    print(f"Duplicates:           {report['counts'].get('duplicates', 0)}")
    print(
        f"Interval errors:      "
        f"{report['counts'].get('interval_errors', 0)}"
    )

    invalid_ohlcv_rows = sum(
        report["counts"].get(k, 0)
        for k in [
            "invalid_open",
            "invalid_high",
            "invalid_low",
            "invalid_close",
            "invalid_volume",
            "invalid_close_time",
            "invalid_ohlc_relationship",
        ]
    )

    print(f"Invalid OHLCV rows:   {invalid_ohlcv_rows}")

    print()
    print("CHECKS")
    print("-" * 58)

    for name, status in report["checks"].items():
        print(f"{name:<32} {status}")

    print()
    print("-" * 58)

    if report["eligible"]:

        # Only now is the candidate promoted to the official raw dataset.
        os.replace(
            CANDIDATE_RAW_PATH,
            FINAL_RAW_PATH,
        )

        full_report["raw_data"] = str(FINAL_RAW_PATH)
        full_report["candidate_raw_csv"] = None

        save_json(
            REPORT_PATH,
            full_report,
        )

        print("FINAL DATA GATE: PASS")
        print()
        print(
            "Dataset is eligible for the next AURA v0.5.2 stage."
        )
        print()
        print(f"Raw data:  {FINAL_RAW_PATH}")
        print(f"Report:    {REPORT_PATH}")

        return 0

    print("FINAL DATA GATE: FAIL")
    print()
    print(
        "Dataset is NOT eligible for the next AURA v0.5.2 stage."
    )
    print()
    print(
        "The candidate dataset has been preserved for diagnosis:"
    )
    print(f"Candidate: {CANDIDATE_RAW_PATH}")
    print(f"Report:    {REPORT_PATH}")
    print()
    print(
        "The existing official raw CSV was NOT replaced."
    )

    return 1


if __name__ == "__main__":
    sys.exit(main())
