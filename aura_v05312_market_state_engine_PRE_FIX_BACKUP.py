#!/usr/bin/env python3
"""
AURA v0.5.3.12 — Market State Engine

LOCKED ARCHITECTURE
-------------------
Single Source of Truth for downstream AURA decision modules.

The engine:
- consumes canonical CLOSED 1H OHLCV data produced by the data layer;
- validates chronology, duplicates, OHLCV integrity, freshness and 1H continuity;
- builds CLOSED 4H candles without interpolation/forward-fill;
- requires every 4H candle used by the current state to contain exactly
  four underlying 1H bars;
- computes the frozen research indicators once;
- publishes a deterministic JSON market-state fingerprint;
- fails closed when required data is missing or invalid;
- never places orders and never changes the frozen research hypothesis.

Expected input columns:
    timestamp,symbol,open,high,low,close,volume

Default input:
    data/prospective_alpaca/alpaca_1h_closed_bars.csv

The input may contain multiple symbols. BTC/USD and ETH/USD are expected.
Use --symbol to restrict processing to one symbol.

This module deliberately does NOT fetch data from Alpaca itself. That keeps
the Market State Engine deterministic and makes the existing Alpaca collector
the single upstream data adapter. A later live adapter must write the same
canonical schema before this engine consumes it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


VERSION = "AURA v0.5.3.12"

DEFAULT_INPUT = Path(r"data\prospective_alpaca\alpaca_1h_closed_bars.csv")
DEFAULT_OUTPUT = Path(r"regime_output\market_state")

SYMBOLS = ("BTC/USD", "ETH/USD")

# Frozen research configuration — DO NOT CHANGE in this layer.
FROZEN_CANDIDATE = "BEAR x LOW ATR x POSITIVE bar-2"
FROZEN_ATR_THRESHOLD_PCT = 0.596
FROZEN_EMA_PERIOD_4H = 50
ATR_PERIOD_1H = 14
BAR_2_LAG_HOURS = 2

HOUR = pd.Timedelta(hours=1)
FOUR_HOURS = pd.Timedelta(hours=4)

# 50 complete 4H candles require at least 200 1H bars. We require one
# additional completed 4H candle so the current state cannot accidentally
# depend on a partial warm-up boundary.
MIN_1H_BARS = FROZEN_EMA_PERIOD_4H * 4 + 4


REQUIRED_COLUMNS = {
    "timestamp",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "volume",
}

PRICE_COLUMNS = ("open", "high", "low", "close")


def utc_now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


def iso(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.isoformat().replace("+00:00", "Z")


def stable_json(obj: Any) -> str:
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def clean_number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return value


def load_input(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Canonical 1H input not found: {path}")

    df = pd.read_csv(path)
    missing = sorted(REQUIRED_COLUMNS - set(df.columns))
    if missing:
        raise ValueError(
            "Missing required columns: " + ", ".join(missing)
        )

    df = df.copy()
    df["timestamp"] = pd.to_datetime(
        df["timestamp"], utc=True, errors="coerce"
    )

    df["symbol"] = df["symbol"].astype(str).str.strip()

    for col in PRICE_COLUMNS + ("volume",):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    return df


def base_snapshot(
    *,
    generated_at: pd.Timestamp,
    input_path: Path,
    input_hash: str | None,
    requested_symbol: str | None,
) -> dict[str, Any]:
    return {
        "agent_version": VERSION,
        "engine": "MARKET_STATE_ENGINE",
        "generated_at": iso(generated_at),
        "requested_symbol": requested_symbol,
        "data_status": "INVALID",
        "market_state_valid": False,
        "state_id": None,
        "state_hash": None,
        "invalid_reasons": [],
        "frozen_configuration": {
            "candidate": FROZEN_CANDIDATE,
            "atr_threshold_pct": FROZEN_ATR_THRESHOLD_PCT,
            "ema_period_4h": FROZEN_EMA_PERIOD_4H,
            "atr_period_1h": ATR_PERIOD_1H,
            "bar_2_lag_hours": BAR_2_LAG_HOURS,
        },
        "guardrails": {
            "single_source_of_truth": True,
            "no_recalculation_downstream": True,
            "lookahead_allowed": False,
            "missing_data_policy": "FAIL_CLOSED",
            "interpolation_allowed": False,
            "forward_fill_allowed": False,
            "orders_allowed": False,
            "paper_execution": False,
            "live_execution": False,
            "strategy_changed": False,
            "parameters_changed": False,
        },
        "provenance": {
            "input_file": str(input_path.resolve()),
            "input_sha256": input_hash,
            "calculation_engine": VERSION,
        },
        "symbols": {},
    }


def validate_1h_frame(
    g: pd.DataFrame,
    as_of: pd.Timestamp,
) -> tuple[bool, list[str]]:
    errors: list[str] = []

    if g.empty:
        return False, ["NO_DATA"]

    if g["timestamp"].isna().any():
        errors.append("INVALID_TIMESTAMP")

    if g["timestamp"].duplicated().any():
        errors.append("DUPLICATE_TIMESTAMP")

    if not g["timestamp"].dropna().is_monotonic_increasing:
        errors.append("TIMESTAMPS_NOT_MONOTONIC")

    ts = g["timestamp"].dropna()

    if not ts.empty:
        if (ts.dt.minute != 0).any() or (ts.dt.second != 0).any() or (
            ts.dt.microsecond != 0
        ).any():
            errors.append("TIMESTAMP_NOT_HOUR_ALIGNED")

        if (ts >= as_of.floor("h")).any():
            errors.append("OPEN_OR_CURRENT_HOUR_PRESENT")

        if len(ts) > 1:
            gaps = ts.sort_values().diff().dropna()
            if (gaps != HOUR).any():
                errors.append("MISSING_OR_NON_1H_BAR")

    for col in PRICE_COLUMNS + ("volume",):
        if g[col].isna().any():
            errors.append(f"MISSING_NUMERIC_DATA:{col}")
        elif not np.isfinite(g[col].to_numpy(dtype=float)).all():
            errors.append(f"NON_FINITE_DATA:{col}")

    if (g[list(PRICE_COLUMNS)] <= 0).any().any():
        errors.append("NON_POSITIVE_PRICE")

    if (g["volume"] < 0).any():
        errors.append("NEGATIVE_VOLUME")

    o = g["open"]
    h = g["high"]
    l = g["low"]
    c = g["close"]

    bad_ohlc = (
        (h < pd.concat([o, c, l], axis=1).max(axis=1))
        | (l > pd.concat([o, c, h], axis=1).min(axis=1))
    )
    if bad_ohlc.any():
        errors.append("INVALID_OHLC_RELATIONSHIP")

    if len(g) < MIN_1H_BARS:
        errors.append(f"INSUFFICIENT_1H_WARMUP:{len(g)}<{MIN_1H_BARS}")

    return len(errors) == 0, sorted(set(errors))


def wilder_atr(series_df: pd.DataFrame) -> pd.Series:
    tr_prev = series_df["close"].shift(1)
    tr = pd.concat(
        [
            series_df["high"] - series_df["low"],
            (series_df["high"] - tr_prev).abs(),
            (series_df["low"] - tr_prev).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr = pd.Series(np.nan, index=series_df.index, dtype=float)
    if len(tr) < ATR_PERIOD_1H:
        return atr

    first = tr.iloc[:ATR_PERIOD_1H].mean()
    atr.iloc[ATR_PERIOD_1H - 1] = first

    for i in range(ATR_PERIOD_1H, len(tr)):
        atr.iloc[i] = (
            atr.iloc[i - 1] * (ATR_PERIOD_1H - 1) + tr.iloc[i]
        ) / ATR_PERIOD_1H

    return atr


def build_4h(g: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """
    Build CLOSED 4H candles from the 1H canonical stream.

    With label='right' and closed='right', a 4H candle ending at 16:00
    contains the four 1H bars timestamped 13:00,14:00,15:00,16:00.

    We explicitly count the underlying 1H rows. A missing constituent bar
    invalidates the 4H candle rather than silently producing a partial candle.
    """
    errors: list[str] = []
    x = g.set_index("timestamp").sort_index()

    grouped = x.resample("4h", label="right", closed="right")

    rows: list[dict[str, Any]] = []

    for end_ts, bucket in grouped:
        if bucket.empty:
            continue

        # Boundary buckets can legitimately be partial because the input
        # window may start/end inside a 4H period. They are never used as
        # market state. Missing 1H bars inside the canonical stream are
        # caught separately by validate_1h_frame(), which is fail-closed.
        if len(bucket) != 4:
            continue

        idx = bucket.index.sort_values()

        if len(idx) != 4 or not bool((idx.to_series().diff().dropna() == HOUR).all()):
            errors.append(f"NON_CONTIGUOUS_4H_CANDLE:{iso(end_ts)}")
            continue

        rows.append(
            {
                "timestamp": end_ts,
                "open": float(bucket["open"].iloc[0]),
                "high": float(bucket["high"].max()),
                "low": float(bucket["low"].min()),
                "close": float(bucket["close"].iloc[-1]),
                "volume": float(bucket["volume"].sum()),
                "underlying_1h_count": 4,
                "underlying_1h_first": idx[0],
                "underlying_1h_last": idx[-1],
            }
        )

    h4 = pd.DataFrame(rows)

    if h4.empty:
        return h4, sorted(set(errors))

    h4["timestamp"] = pd.to_datetime(h4["timestamp"], utc=True)
    h4 = h4.sort_values("timestamp").reset_index(drop=True)

    return h4, sorted(set(errors))


def calculate_state(g: pd.DataFrame) -> tuple[dict[str, Any] | None, list[str]]:
    """
    Calculate the current canonical state.

    ATR14 is calculated on the canonical 1H stream.
    EMA50 is calculated on CLOSED 4H closes.
    bar-2 is the frozen two-hour close-to-close return.
    """
    errors: list[str] = []

    g = g.sort_values("timestamp").reset_index(drop=True).copy()

    g["atr14"] = wilder_atr(g)
    g["atr14_pct"] = 100.0 * g["atr14"] / g["close"]

    h4, h4_errors = build_4h(g)

    if h4_errors:
        errors.extend(h4_errors)

    if h4.empty:
        return None, sorted(set(errors + ["NO_COMPLETE_4H_CANDLES"]))

    if len(h4) < FROZEN_EMA_PERIOD_4H:
        errors.append(
            f"INSUFFICIENT_4H_WARMUP:{len(h4)}<{FROZEN_EMA_PERIOD_4H}"
        )
        return None, sorted(set(errors))

    h4["ema50"] = h4["close"].ewm(
        span=FROZEN_EMA_PERIOD_4H,
        adjust=False,
        min_periods=FROZEN_EMA_PERIOD_4H,
    ).mean()

    latest_1h = g.iloc[-1]
    latest_4h = h4.iloc[-1]

    # The latest complete 4H candle must end at or before the latest 1H bar.
    if pd.Timestamp(latest_4h["timestamp"]) > pd.Timestamp(
        latest_1h["timestamp"]
    ):
        errors.append("4H_STATE_AHEAD_OF_1H_STATE")
        return None, sorted(set(errors))

    atr = clean_number(latest_1h["atr14_pct"])
    ema50 = clean_number(latest_4h["ema50"])
    close = clean_number(latest_1h["close"])

    # Frozen bar-2 definition from the research layer:
    # two-hour close-to-close return on the canonical 1H series.
    if len(g) < BAR_2_LAG_HOURS + 1:
        bar2 = None
    else:
        bar2 = clean_number(
            100.0 * (
                float(latest_1h["close"])
                / float(g.iloc[-1 - BAR_2_LAG_HOURS]["close"])
                - 1.0
            )
        )

    if any(v is None for v in (atr, ema50, close, bar2)):
        errors.append("REQUIRED_INDICATOR_UNAVAILABLE")
        return None, sorted(set(errors))

    trend = "BEAR" if close < ema50 else "BULL_OR_NEUTRAL"
    atr_regime = (
        "LOW"
        if atr < FROZEN_ATR_THRESHOLD_PCT
        else "HIGH_OR_EQUAL"
    )
    bar2_regime = "POSITIVE" if bar2 > 0 else "NON_POSITIVE"

    regime_state = f"{trend} x {atr_regime} x {bar2_regime}"
    candidate_match = regime_state == FROZEN_CANDIDATE

    state = {
        "timestamp": iso(latest_1h["timestamp"]),
        "symbol": str(latest_1h["symbol"]),
        "close": close,
        "atr14": clean_number(latest_1h["atr14"]),
        "atr14_pct": atr,
        "atr_regime": atr_regime,
        "ema50_4h": ema50,
        "ema50_4h_timestamp": iso(latest_4h["timestamp"]),
        "trend_regime": trend,
        "bar_2_return_pct": bar2,
        "bar_2_regime": bar2_regime,
        "regime_state": regime_state,
        "frozen_candidate_match": candidate_match,
        "source_1h_bars": int(len(g)),
        "source_4h_complete_bars": int(len(h4)),
        "latest_4h_underlying_1h_count": int(
            latest_4h["underlying_1h_count"]
        ),
        "latest_4h_underlying_1h_first": iso(
            latest_4h["underlying_1h_first"]
        ),
        "latest_4h_underlying_1h_last": iso(
            latest_4h["underlying_1h_last"]
        ),
    }

    return state, sorted(set(errors))


def canonical_state_payload(
    symbol_states: dict[str, dict[str, Any]],
    generated_at: pd.Timestamp,
) -> dict[str, Any]:
    return {
        "agent_version": VERSION,
        "engine": "MARKET_STATE_ENGINE",
        "generated_at": iso(generated_at),
        "symbols": symbol_states,
        "frozen_configuration": {
            "candidate": FROZEN_CANDIDATE,
            "atr_threshold_pct": FROZEN_ATR_THRESHOLD_PCT,
            "ema_period_4h": FROZEN_EMA_PERIOD_4H,
            "atr_period_1h": ATR_PERIOD_1H,
            "bar_2_lag_hours": BAR_2_LAG_HOURS,
        },
    }


def process(
    df: pd.DataFrame,
    *,
    input_path: Path,
    as_of: pd.Timestamp,
    requested_symbol: str | None,
) -> dict[str, Any]:
    input_hash = sha256_file(input_path) if input_path.exists() else None
    snap = base_snapshot(
        generated_at=as_of,
        input_path=input_path,
        input_hash=input_hash,
        requested_symbol=requested_symbol,
    )

    if requested_symbol:
        selected = [requested_symbol]
    else:
        selected = list(SYMBOLS)

    global_errors: list[str] = []

    # Validate that the file itself has no unknown/malformed symbol rows.
    if df["symbol"].isna().any() or (df["symbol"].str.len() == 0).any():
        global_errors.append("INVALID_SYMBOL")

    if df["symbol"].duplicated(["symbol", "timestamp"]).any():
        global_errors.append("DUPLICATE_SYMBOL_TIMESTAMP")

    all_states: dict[str, dict[str, Any]] = {}

    for symbol in selected:
        g = (
            df[df["symbol"] == symbol]
            .copy()
            .sort_values("timestamp")
            .reset_index(drop=True)
        )

        valid, validation_errors = validate_1h_frame(g, as_of)

        if not valid:
            snap["symbols"][symbol] = {
                "data_status": "INVALID",
                "market_state_valid": False,
                "invalid_reasons": validation_errors,
                "market_state": None,
            }
            global_errors.extend(
                [f"{symbol}:{e}" for e in validation_errors]
            )
            continue

        state, state_errors = calculate_state(g)

        if state_errors or state is None:
            snap["symbols"][symbol] = {
                "data_status": "INVALID",
                "market_state_valid": False,
                "invalid_reasons": state_errors,
                "market_state": None,
            }
            global_errors.extend([f"{symbol}:{e}" for e in state_errors])
            continue

        snap["symbols"][symbol] = {
            "data_status": "VALID",
            "market_state_valid": True,
            "invalid_reasons": [],
            "market_state": state,
        }
        all_states[symbol] = state

    # A single canonical state is valid only when every requested symbol is
    # valid. This prevents BTC/ETH modules from seeing different market
    # worlds because one side has a missing bar.
    if len(all_states) != len(selected):
        snap["invalid_reasons"] = sorted(set(global_errors + [
            "NOT_ALL_REQUIRED_SYMBOLS_VALID"
        ]))
        snap["data_status"] = "INVALID"
        snap["market_state_valid"] = False
        return snap

    canonical = canonical_state_payload(all_states, as_of)
    canonical_json = stable_json(canonical)
    state_hash = sha256_text(canonical_json)

    # State ID is deterministic for the exact market-state timestamps + hash.
    timestamps = "|".join(
        f"{symbol}:{all_states[symbol]['timestamp']}"
        for symbol in sorted(all_states)
    )
    state_id = (
        f"MS-{sha256_text(timestamps + '|' + state_hash)[:24]}"
    )

    snap["data_status"] = "VALID"
    snap["market_state_valid"] = True
    snap["invalid_reasons"] = []
    snap["state_hash"] = state_hash
    snap["state_id"] = state_id
    snap["canonical_state"] = canonical

    return snap


def print_report(snapshot: dict[str, Any], output_path: Path) -> None:
    print("=" * 96)
    print(f"{VERSION} — MARKET STATE ENGINE")
    print("=" * 96)
    print()
    print("MODE                 : RESEARCH ONLY")
    print("ORDERS               : DISABLED")
    print("PAPER EXECUTION      : DISABLED")
    print("LIVE EXECUTION       : DISABLED")
    print()
    print(f"FROZEN CANDIDATE     : {FROZEN_CANDIDATE}")
    print(f"ATR THRESHOLD        : {FROZEN_ATR_THRESHOLD_PCT:.3f}%")
    print(f"4H EMA               : EMA{FROZEN_EMA_PERIOD_4H}")
    print(f"BAR-2                : {BAR_2_LAG_HOURS}H CLOSE-TO-CLOSE")
    print()
    print("SINGLE SOURCE OF TRUTH: ENABLED")
    print("FAIL-CLOSED POLICY    : ENABLED")
    print()

    print("MARKET STATE")
    print("-" * 96)
    print(f"DATA STATUS          : {snapshot['data_status']}")
    print(f"MARKET STATE VALID   : {snapshot['market_state_valid']}")
    print(f"STATE ID             : {snapshot['state_id']}")
    print(f"STATE HASH           : {snapshot['state_hash']}")
    print()

    for symbol, item in snapshot["symbols"].items():
        print(symbol)
        print(f"  DATA STATUS        : {item['data_status']}")
        print(f"  STATE VALID        : {item['market_state_valid']}")

        if item["market_state_valid"]:
            s = item["market_state"]
            print(f"  TIMESTAMP          : {s['timestamp']}")
            print(f"  CLOSE              : {s['close']}")
            print(f"  ATR14 %            : {s['atr14_pct']}")
            print(f"  ATR REGIME         : {s['atr_regime']}")
            print(f"  EMA50 4H           : {s['ema50_4h']}")
            print(f"  EMA50 4H TIMESTAMP : {s['ema50_4h_timestamp']}")
            print(f"  TREND REGIME       : {s['trend_regime']}")
            print(f"  BAR-2 RETURN %     : {s['bar_2_return_pct']}")
            print(f"  BAR-2 REGIME       : {s['bar_2_regime']}")
            print(f"  REGIME STATE       : {s['regime_state']}")
            print(f"  CANDIDATE MATCH    : {s['frozen_candidate_match']}")
            print(
                "  4H SOURCE BARS     : "
                f"{s['latest_4h_underlying_1h_count']}/4"
            )
        else:
            for reason in item["invalid_reasons"]:
                print(f"  INVALID            : {reason}")
        print()

    if snapshot["invalid_reasons"]:
        print("FAIL-CLOSED REASONS")
        print("-" * 96)
        for reason in snapshot["invalid_reasons"]:
            print(f"  - {reason}")
        print()

    print(f"OUTPUT               : {output_path}")
    print("=" * 96)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AURA v0.5.3.12 canonical Market State Engine"
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help="Canonical closed 1H OHLCV CSV",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT / "market_state_snapshot.json",
        help="Canonical market-state JSON output",
    )
    parser.add_argument(
        "--symbol",
        default=None,
        help="Optional single-symbol restriction, e.g. BTC/USD",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run exactly one state-generation cycle and exit",
    )
    args = parser.parse_args()

    # --once is intentionally accepted for deterministic smoke testing.
    # Without it the engine still performs one cycle; the flag exists so
    # orchestration/supervisor layers can explicitly request one-shot mode.
    _ = args.once

    try:
        df = load_input(args.input)

        as_of = utc_now()
        snapshot = process(
            df,
            input_path=args.input,
            as_of=as_of,
            requested_symbol=args.symbol,
        )

        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(snapshot, indent=2, ensure_ascii=False, allow_nan=False),
            encoding="utf-8",
        )

        print_report(snapshot, args.output)

        # A valid state is exit 0. Invalid data is exit 2 so supervisors can
        # distinguish "engine ran and failed closed" from an engine crash.
        return 0 if snapshot["market_state_valid"] else 2

    except Exception as exc:
        print()
        print("ENGINE ERROR")
        print("-" * 96)
        print(f"{type(exc).__name__}: {exc}")
        print("FAIL-CLOSED: no valid market state was published.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
