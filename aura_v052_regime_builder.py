#!/usr/bin/env python3
"""
AURA v0.5.2 — Schema-Aware Regime Builder
RESEARCH ONLY / NO ORDERS

Purpose
-------
Assign the 55 frozen C0 trades to the pre-registered 2 x 2 x 2 regime matrix:

    BTC 4H trend:  BULL / BEAR
    Asset ATR14:   HIGH / LOW
    Bar 2:         NEGATIVE / POSITIVE

Frozen controls
---------------
- Expected C0 trades: 55
- BTC trend: completed 4H EMA50
- EMA warm-up requirement: 200 completed 4H bars
- ATR: 1H ATR14 / close
- Frozen ATR threshold: 0.596%
- No lookahead: only bars whose completed close_time <= signal_timestamp
- C0 trades and Bar-2 values are not modified
- Raw market-data CSVs are never modified

The builder is deliberately schema-aware:
BTC source:
    open_time_ms,open_time,open,high,low,close,volume,close_time_ms,close_time

ETH source:
    timestamp,open_time_ms,close_time,open,high,low,close,volume

Both are normalized internally to:
    open_time, close_time, open, high, low, close, volume
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


VERSION = "AURA v0.5.2 — SCHEMA-AWARE REGIME BUILDER"
EXPECTED_TRADES = 55
ATR_THRESHOLD_PCT = 0.596
EMA_PERIOD = 50
EMA_WARMUP_BARS = 200
BAR2_NEGATIVE_CUTOFF = 0.0


def utc_parse(value: str) -> datetime:
    s = str(value).strip()
    if not s:
        raise ValueError("empty timestamp")

    if s.endswith("Z"):
        s = s[:-1] + "+00:00"

    dt = datetime.fromisoformat(s)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    return dt


def iso(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def floor_4h(dt: datetime) -> datetime:
    dt = dt.astimezone(timezone.utc)
    hour = (dt.hour // 4) * 4
    return dt.replace(hour=hour, minute=0, second=0, microsecond=0)


def to_float(value: str, field: str) -> float:
    try:
        return float(str(value).strip())
    except Exception as exc:
        raise ValueError(f"invalid {field}: {value!r}") from exc


def normalize_row(row: dict, source: str, line_no: int) -> dict:
    """
    Normalize the two approved source schemas without changing the raw files.
    """
    # CSV headers can carry a UTF-8 BOM.
    row = {str(k).lstrip("\ufeff"): v for k, v in row.items()}

    if source == "BTC":
        if "open_time" not in row:
            raise ValueError("BTC schema missing open_time")
        if "close_time" not in row:
            raise ValueError("BTC schema missing close_time")

        open_time = utc_parse(row["open_time"])
        close_time = utc_parse(row["close_time"])

    elif source == "ETH":
        if "timestamp" not in row:
            raise ValueError("ETH schema missing timestamp")
        if "close_time" not in row:
            raise ValueError("ETH schema missing close_time")

        open_time = utc_parse(row["timestamp"])
        close_time = utc_parse(row["close_time"])

    else:
        raise ValueError(f"unsupported source {source}")

    return {
        "open_time": open_time,
        "close_time": close_time,
        "open": to_float(row["open"], "open"),
        "high": to_float(row["high"], "high"),
        "low": to_float(row["low"], "low"),
        "close": to_float(row["close"], "close"),
        "volume": to_float(row["volume"], "volume"),
        "source": source,
        "source_line": line_no,
    }


def load_market_csv(path: Path, source: str) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(str(path))

    rows = []

    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)

        if reader.fieldnames is None:
            raise ValueError(f"{path}: no CSV header")

        headers = {h.lstrip("\ufeff") for h in reader.fieldnames}

        if source == "BTC":
            required = {"open_time", "open", "high", "low", "close", "volume", "close_time"}
        else:
            required = {"timestamp", "open", "high", "low", "close", "volume", "close_time"}

        missing = required - headers
        if missing:
            raise ValueError(
                f"{path}: {source} schema missing columns: {sorted(missing)}"
            )

        for line_no, row in enumerate(reader, start=2):
            try:
                item = normalize_row(row, source, line_no)
            except Exception as exc:
                raise ValueError(f"{path}:{line_no}: {exc}") from exc

            if item["high"] < max(item["open"], item["close"], item["low"]):
                raise ValueError(f"{path}:{line_no}: invalid high")
            if item["low"] > min(item["open"], item["close"], item["high"]):
                raise ValueError(f"{path}:{line_no}: invalid low")
            if item["volume"] < 0:
                raise ValueError(f"{path}:{line_no}: negative volume")
            if item["close_time"] <= item["open_time"]:
                raise ValueError(f"{path}:{line_no}: close_time <= open_time")

            rows.append(item)

    rows.sort(key=lambda x: x["open_time"])

    # Strict timestamp integrity. Do not repair anything.
    seen = set()
    for r in rows:
        ts = r["open_time"]
        if ts in seen:
            raise ValueError(f"{path}: duplicate open_time {iso(ts)}")
        seen.add(ts)

    for a, b in zip(rows, rows[1:]):
        delta = b["open_time"] - a["open_time"]
        if delta != timedelta(hours=1):
            raise ValueError(
                f"{path}: non-1H continuity between {iso(a['open_time'])} and {iso(b['open_time'])}"
            )

    return rows


def wilder_atr_series(rows: list[dict], period: int = 14) -> list[float | None]:
    """
    Wilder ATR14. Value is ATR in price units at each completed 1H bar.
    """
    if len(rows) < period + 1:
        return [None] * len(rows)

    trs: list[float | None] = [None] * len(rows)

    for i in range(1, len(rows)):
        prev_close = rows[i - 1]["close"]
        high = rows[i]["high"]
        low = rows[i]["low"]
        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )
        trs[i] = tr

    atr: list[float | None] = [None] * len(rows)

    first_values = [trs[i] for i in range(1, period + 1)]
    if any(v is None for v in first_values):
        return atr

    current = sum(first_values) / period
    atr[period] = current

    alpha = 1.0 / period

    for i in range(period + 1, len(rows)):
        tr = trs[i]
        if tr is None:
            continue
        current = ((current * (period - 1)) + tr) / period
        atr[i] = current

    return atr


def build_4h(rows: list[dict]) -> list[dict]:
    """
    Construct completed 4H bars from exactly four contiguous 1H bars.

    A 4H bar is eligible only if all four underlying 1H bars exist.
    """
    groups: dict[datetime, list[dict]] = defaultdict(list)

    for row in rows:
        groups[floor_4h(row["open_time"])].append(row)

    bars = []

    for bucket_start in sorted(groups):
        g = sorted(groups[bucket_start], key=lambda x: x["open_time"])

        if len(g) != 4:
            continue

        expected = [
            bucket_start + timedelta(hours=i)
            for i in range(4)
        ]

        if [x["open_time"] for x in g] != expected:
            continue

        bars.append(
            {
                "open_time": bucket_start,
                "close_time": g[-1]["close_time"],
                "open": g[0]["open"],
                "high": max(x["high"] for x in g),
                "low": min(x["low"] for x in g),
                "close": g[-1]["close"],
                "volume": sum(x["volume"] for x in g),
            }
        )

    return bars


def ema_series(values: list[float], period: int) -> list[float | None]:
    if len(values) < period:
        return [None] * len(values)

    result: list[float | None] = [None] * len(values)

    # Standard EMA seed = SMA of first period observations.
    seed = sum(values[:period]) / period
    result[period - 1] = seed

    alpha = 2.0 / (period + 1.0)
    current = seed

    for i in range(period, len(values)):
        current = (values[i] * alpha) + (current * (1.0 - alpha))
        result[i] = current

    return result


def prepare_market(rows: list[dict]) -> dict:
    atr_values = wilder_atr_series(rows, 14)

    one_h = []
    for i, row in enumerate(rows):
        item = dict(row)
        item["atr14"] = atr_values[i]
        one_h.append(item)

    four_h = build_4h(rows)
    ema_values = ema_series([x["close"] for x in four_h], EMA_PERIOD)

    for i, bar in enumerate(four_h):
        bar["ema50"] = ema_values[i]

    return {
        "one_h": one_h,
        "four_h": four_h,
    }


def latest_completed_1h(market: dict, signal_time: datetime) -> tuple[dict | None, int]:
    eligible = [
        r for r in market["one_h"]
        if r["close_time"] <= signal_time
    ]

    if not eligible:
        return None, 0

    return eligible[-1], len(eligible)


def latest_completed_4h(btc_market: dict, signal_time: datetime) -> tuple[dict | None, int]:
    eligible = [
        r for r in btc_market["four_h"]
        if r["close_time"] <= signal_time and r["ema50"] is not None
    ]

    if not eligible:
        return None, 0

    return eligible[-1], len(eligible)


def read_trades(path: Path) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(str(path))

    rows = []

    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)

        if reader.fieldnames is None:
            raise ValueError("trade CSV has no header")

        required = {
            "trade_id",
            "symbol",
            "signal_timestamp",
            "bar_2_close_return_before_costs",
            "net_return",
        }

        missing = required - set(reader.fieldnames)
        if missing:
            raise ValueError(f"trade CSV missing columns: {sorted(missing)}")

        for line_no, row in enumerate(reader, start=2):
            try:
                signal_time = utc_parse(row["signal_timestamp"])
                bar2 = to_float(
                    row["bar_2_close_return_before_costs"],
                    "bar_2_close_return_before_costs",
                )
                net_return = to_float(row["net_return"], "net_return")

                rows.append(
                    {
                        "trade_id": row["trade_id"].strip(),
                        "symbol": row["symbol"].strip().upper(),
                        "signal_timestamp": signal_time,
                        "entry_timestamp": (
                            utc_parse(row["entry_timestamp"])
                            if row.get("entry_timestamp")
                            else None
                        ),
                        "bar2": bar2,
                        "net_return": net_return,
                        "period": row.get("period", "").strip().upper(),
                    }
                )
            except Exception as exc:
                raise ValueError(f"{path}:{line_no}: {exc}") from exc

    return rows


def asset_from_symbol(symbol: str) -> str | None:
    s = symbol.upper().replace("-", "_")
    if s.startswith("BTC"):
        return "BTC"
    if s.startswith("ETH"):
        return "ETH"
    return None


def diagnostic_status(n: int) -> str:
    if n < 5:
        return "DIAGNOSTICALLY WEAK"
    if n < 10:
        return "SMALL SAMPLE"
    return "NORMAL"


def mean_ci(values: list[float]) -> tuple[float | None, float | None]:
    if not values:
        return None, None

    if len(values) == 1:
        return values[0], values[0]

    mean = statistics.mean(values)
    sd = statistics.stdev(values)
    se = sd / math.sqrt(len(values))
    margin = 1.96 * se

    return mean - margin, mean + margin


def stats_for(rows: list[dict]) -> dict:
    values = [r["net_return"] for r in rows]

    if not values:
        return {
            "n": 0,
            "mean": None,
            "median": None,
            "hit_rate": None,
            "ci_low": None,
            "ci_high": None,
            "diagnostic_status": "DIAGNOSTICALLY WEAK",
        }

    ci_low, ci_high = mean_ci(values)

    return {
        "n": len(values),
        "mean": statistics.mean(values),
        "median": statistics.median(values),
        "hit_rate": sum(v > 0 for v in values) / len(values),
        "ci_low": ci_low,
        "ci_high": ci_high,
        "diagnostic_status": diagnostic_status(len(values)),
    }


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=fieldnames,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)


def pct(v):
    if v is None or v == "":
        return ""
    return f"{v:.8f}"


def pct_display(v):
    if v is None:
        return "NA"
    return f"{v * 100:.3f}%"


def build_assignments(trades: list[dict], btc_market: dict, eth_market: dict) -> list[dict]:
    output = []

    for trade in trades:
        asset = asset_from_symbol(trade["symbol"])

        record = {
            "trade_id": trade["trade_id"],
            "symbol": trade["symbol"],
            "signal_timestamp": iso(trade["signal_timestamp"]),
            "entry_timestamp": iso(trade["entry_timestamp"]),
            "bar_2_close_return_before_costs": pct(trade["bar2"]),
            "net_return": pct(trade["net_return"]),
            "period": trade["period"],
            "asset_atr_pct": "",
            "atr_reference_close_time": "",
            "btc_4h_open": "",
            "btc_4h_close": "",
            "btc_4h_close_price": "",
            "btc_4h_ema50": "",
            "btc_4h_regime": "UNAVAILABLE",
            "btc_4h_lookback_count": 0,
            "bar2_regime": "NEGATIVE" if trade["bar2"] < 0 else "POSITIVE",
            "assignment_status": "",
            "failure_reason": "",
            "regime_cell": "UNASSIGNED",
        }

        if asset is None:
            record["assignment_status"] = "FAIL_UNSUPPORTED_ASSET"
            record["failure_reason"] = "Symbol is neither BTC nor ETH."
            output.append(record)
            continue

        asset_market = btc_market if asset == "BTC" else eth_market

        atr_bar, atr_count = latest_completed_1h(
            asset_market,
            trade["signal_timestamp"],
        )

        if atr_bar is None:
            record["assignment_status"] = "FAIL_MISSING_ASSET_BARS"
            record["failure_reason"] = (
                f"No completed {asset} 1H bar with close_time <= signal_timestamp."
            )
            output.append(record)
            continue

        if atr_bar["atr14"] is None:
            record["assignment_status"] = "FAIL_ATR_WARMUP"
            record["failure_reason"] = "ATR14 unavailable at signal timestamp."
            output.append(record)
            continue

        atr_pct = atr_bar["atr14"] / atr_bar["close"] * 100.0

        record["asset_atr_pct"] = pct(atr_pct / 100.0)
        record["atr_reference_close_time"] = iso(atr_bar["close_time"])

        btc_bar, btc_count = latest_completed_4h(
            btc_market,
            trade["signal_timestamp"],
        )

        if btc_bar is None:
            record["assignment_status"] = "FAIL_MISSING_BTC_4H_BAR"
            record["failure_reason"] = (
                "No completed BTC 4H bar with EMA50 and close_time <= signal_timestamp."
            )
            output.append(record)
            continue

        if btc_count < EMA_WARMUP_BARS:
            record["assignment_status"] = "FAIL_EMA_WARMUP"
            record["failure_reason"] = (
                f"Only {btc_count} eligible completed BTC 4H bars; "
                f"{EMA_WARMUP_BARS} required."
            )
            output.append(record)
            continue

        btc_regime = "BULL" if btc_bar["close"] > btc_bar["ema50"] else "BEAR"
        atr_regime = "HIGH" if atr_pct > ATR_THRESHOLD_PCT else "LOW"
        bar2_regime = "NEGATIVE" if trade["bar2"] < 0 else "POSITIVE"

        record["btc_4h_open"] = iso(btc_bar["open_time"])
        record["btc_4h_close"] = iso(btc_bar["close_time"])
        record["btc_4h_close_price"] = pct(btc_bar["close"])
        record["btc_4h_ema50"] = pct(btc_bar["ema50"])
        record["btc_4h_regime"] = btc_regime
        record["btc_4h_lookback_count"] = btc_count

        record["assignment_status"] = "PASS"
        record["failure_reason"] = ""
        record["regime_cell"] = f"{btc_regime}__{atr_regime}__{bar2_regime}"

        output.append(record)

    return output


def matrix_rows(assignments: list[dict]) -> list[dict]:
    trends = ["BULL", "BEAR"]
    atrs = ["HIGH", "LOW"]
    bar2s = ["NEGATIVE", "POSITIVE"]

    result = []

    for trend in trends:
        for atr in atrs:
            for bar2 in bar2s:
                subset = [
                    r for r in assignments
                    if r["assignment_status"] == "PASS"
                    and r["btc_4h_regime"] == trend
                    and (
                        ("HIGH" if float(r["asset_atr_pct"]) * 100 > ATR_THRESHOLD_PCT else "LOW")
                        == atr
                    )
                    and r["bar2_regime"] == bar2
                ]

                values = [float(r["net_return"]) for r in subset]
                st = stats_for(
                    [{"net_return": v} for v in values]
                )

                result.append(
                    {
                        "btc_trend": trend,
                        "atr_regime": atr,
                        "bar2_regime": bar2,
                        "n": st["n"],
                        "mean_remaining_or_final_c0_return": (
                            "" if st["mean"] is None else pct(st["mean"])
                        ),
                        "median_return": (
                            "" if st["median"] is None else pct(st["median"])
                        ),
                        "hit_rate": (
                            "" if st["hit_rate"] is None else pct(st["hit_rate"])
                        ),
                        "ci95_mean_low": (
                            "" if st["ci_low"] is None else pct(st["ci_low"])
                        ),
                        "ci95_mean_high": (
                            "" if st["ci_high"] is None else pct(st["ci_high"])
                        ),
                        "diagnostic_status": st["diagnostic_status"],
                    }
                )

    return result


def period_rows(assignments: list[dict]) -> list[dict]:
    periods = ["EARLY", "MIDDLE", "LATE"]
    trends = ["BULL", "BEAR"]
    atrs = ["HIGH", "LOW"]
    bar2s = ["NEGATIVE", "POSITIVE"]

    result = []

    for period in periods:
        for trend in trends:
            for atr in atrs:
                for bar2 in bar2s:
                    subset = [
                        r for r in assignments
                        if r["assignment_status"] == "PASS"
                        and r["period"] == period
                        and r["btc_4h_regime"] == trend
                        and (
                            ("HIGH" if float(r["asset_atr_pct"]) * 100 > ATR_THRESHOLD_PCT else "LOW")
                            == atr
                        )
                        and r["bar2_regime"] == bar2
                    ]

                    st = stats_for(
                        [{"net_return": float(r["net_return"])} for r in subset]
                    )

                    result.append(
                        {
                            "period": period,
                            "btc_trend": trend,
                            "atr_regime": atr,
                            "bar2_regime": bar2,
                            "n": st["n"],
                            "mean_return": (
                                "" if st["mean"] is None else pct(st["mean"])
                            ),
                            "median_return": (
                                "" if st["median"] is None else pct(st["median"])
                            ),
                            "hit_rate": (
                                "" if st["hit_rate"] is None else pct(st["hit_rate"])
                            ),
                            "diagnostic_status": st["diagnostic_status"],
                        }
                    )

    return result


def print_matrix(rows: list[dict]) -> None:
    print("\n" + "=" * 110)
    print("2 × 2 × 2 REGIME MATRIX")
    print("=" * 110)
    print(
        f"{'btc_trend':<10} {'atr_regime':<10} {'bar2_regime':<10} "
        f"{'N':>4} {'mean':>12} {'median':>12} {'hit_rate':>10} {'status'}"
    )
    print("-" * 110)

    for r in rows:
        mean = "NA" if not r["mean_remaining_or_final_c0_return"] else pct_display(float(r["mean_remaining_or_final_c0_return"]))
        median = "NA" if not r["median_return"] else pct_display(float(r["median_return"]))
        hit = "NA" if not r["hit_rate"] else f"{float(r['hit_rate']) * 100:.1f}%"

        print(
            f"{r['btc_trend']:<10} {r['atr_regime']:<10} {r['bar2_regime']:<10} "
            f"{r['n']:>4} {mean:>12} {median:>12} {hit:>10} "
            f"{r['diagnostic_status']}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AURA v0.5.2 schema-aware frozen C0 regime assignment builder."
    )

    parser.add_argument(
        "--trades",
        required=True,
        help="Path to AURA v0.5.1 early_path_diagnostics.csv",
    )
    parser.add_argument(
        "--bars-dir",
        default=None,
        help="Directory containing BTCUSDT_1h_raw.csv and ETHUSDT_1h_raw.csv",
    )
    parser.add_argument(
        "--btc-bars",
        default=None,
        help="Explicit BTCUSDT 1H CSV path",
    )
    parser.add_argument(
        "--eth-bars",
        default=None,
        help="Explicit ETHUSDT 1H CSV path",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Output directory",
    )
    parser.add_argument(
        "--expected-trades",
        type=int,
        default=EXPECTED_TRADES,
        help="Expected frozen C0 trade count. Default: 55",
    )

    args = parser.parse_args()

    try:
        bars_dir = Path(args.bars_dir).resolve() if args.bars_dir else None

        btc_path = (
            Path(args.btc_bars).resolve()
            if args.btc_bars
            else (bars_dir / "BTCUSDT_1h_raw.csv").resolve()
            if bars_dir
            else None
        )

        eth_path = (
            Path(args.eth_bars).resolve()
            if args.eth_bars
            else (bars_dir / "ETHUSDT_1h_raw.csv").resolve()
            if bars_dir
            else None
        )

        if btc_path is None or eth_path is None:
            raise ValueError(
                "Provide --bars-dir or both --btc-bars and --eth-bars."
            )

        trades_path = Path(args.trades).resolve()
        output_dir = Path(args.output).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        print("=" * 90)
        print(VERSION)
        print("=" * 90)
        print("MODE                 : RESEARCH ONLY — NO ORDERS")
        print("CONTROL              : C0 FROZEN")
        print(f"EXPECTED TRADES      : {args.expected_trades}")
        print("BTC TREND             : 4H EMA50")
        print("EMA WARM-UP           : 200 completed 4H bars")
        print("ATR                   : 1H ATR14 / close")
        print(f"ATR THRESHOLD         : {ATR_THRESHOLD_PCT:.3f}%")
        print("LOOKAHEAD             : DISABLED")
        print()
        print(f"Trades source         : {trades_path}")
        print(f"BTC market source     : {btc_path}")
        print(f"ETH market source     : {eth_path}")
        print(f"Output                : {output_dir}")
        print()

        if not btc_path.exists():
            raise FileNotFoundError(f"BTC file not found: {btc_path}")
        if not eth_path.exists():
            raise FileNotFoundError(f"ETH file not found: {eth_path}")

        trades = read_trades(trades_path)

        print(f"C0 trades loaded      : {len(trades)}")

        if len(trades) != args.expected_trades:
            raise ValueError(
                f"Frozen C0 trade-count guard failed: "
                f"expected {args.expected_trades}, got {len(trades)}"
            )

        btc_rows = load_market_csv(btc_path, "BTC")
        eth_rows = load_market_csv(eth_path, "ETH")

        print(f"BTC 1H bars loaded    : {len(btc_rows)}")
        print(f"ETH 1H bars loaded    : {len(eth_rows)}")

        btc_market = prepare_market(btc_rows)
        eth_market = prepare_market(eth_rows)

        print(f"BTC 4H bars built     : {len(btc_market['four_h'])}")
        print(f"ETH 4H bars built     : {len(eth_market['four_h'])}")

        assignments = build_assignments(
            trades,
            btc_market,
            eth_market,
        )

        pass_count = sum(
            1 for r in assignments if r["assignment_status"] == "PASS"
        )
        fail_count = len(assignments) - pass_count

        print("\n" + "=" * 90)
        print("ASSIGNMENT STATUS")
        print("=" * 90)
        print(f"PASS assignments      : {pass_count}")
        print(f"FAILED assignments    : {fail_count}")

        if fail_count:
            reasons = defaultdict(int)
            for r in assignments:
                if r["assignment_status"] != "PASS":
                    reasons[r["assignment_status"]] += 1

            print("\nFAILURE REASONS")
            for key, count in sorted(reasons.items()):
                print(f"{key:<35} {count}")

        matrix = matrix_rows(assignments)
        periods = period_rows(assignments)

        print_matrix(matrix)

        ledger_fields = [
            "trade_id",
            "symbol",
            "signal_timestamp",
            "entry_timestamp",
            "bar_2_close_return_before_costs",
            "net_return",
            "period",
            "asset_atr_pct",
            "atr_reference_close_time",
            "btc_4h_open",
            "btc_4h_close",
            "btc_4h_close_price",
            "btc_4h_ema50",
            "btc_4h_regime",
            "btc_4h_lookback_count",
            "bar2_regime",
            "assignment_status",
            "failure_reason",
            "regime_cell",
        ]

        matrix_fields = [
            "btc_trend",
            "atr_regime",
            "bar2_regime",
            "n",
            "mean_remaining_or_final_c0_return",
            "median_return",
            "hit_rate",
            "ci95_mean_low",
            "ci95_mean_high",
            "diagnostic_status",
        ]

        period_fields = [
            "period",
            "btc_trend",
            "atr_regime",
            "bar2_regime",
            "n",
            "mean_return",
            "median_return",
            "hit_rate",
            "diagnostic_status",
        ]

        write_csv(
            output_dir / "regime_assignment_ledger.csv",
            assignments,
            ledger_fields,
        )
        write_csv(
            output_dir / "regime_matrix.csv",
            matrix,
            matrix_fields,
        )
        write_csv(
            output_dir / "regime_period_summary.csv",
            periods,
            period_fields,
        )

        manifest = {
            "version": VERSION,
            "mode": "RESEARCH_ONLY_NO_ORDERS",
            "control": "C0_FROZEN",
            "expected_trades": args.expected_trades,
            "trades_loaded": len(trades),
            "pass_assignments": pass_count,
            "failed_assignments": fail_count,
            "btc_source": str(btc_path),
            "eth_source": str(eth_path),
            "atr_threshold_pct": ATR_THRESHOLD_PCT,
            "atr_definition": "Wilder ATR14 / completed 1H close",
            "btc_trend_definition": "completed BTC 4H close > EMA50",
            "ema_period": EMA_PERIOD,
            "ema_warmup_bars": EMA_WARMUP_BARS,
            "lookahead_rule": "only bars with close_time <= signal_timestamp",
            "raw_data_modified": False,
            "outputs": [
                str(output_dir / "regime_assignment_ledger.csv"),
                str(output_dir / "regime_matrix.csv"),
                str(output_dir / "regime_period_summary.csv"),
            ],
        }

        with (output_dir / "regime_run_manifest.json").open(
            "w", encoding="utf-8"
        ) as fh:
            json.dump(manifest, fh, indent=2)

        print("\n" + "=" * 90)

        if fail_count == 0:
            print("REGIME ASSIGNMENT GATE: PASS")
            print("All frozen C0 trades received a valid regime assignment.")
        else:
            print("REGIME ASSIGNMENT GATE: FAIL")
            print(
                "Do NOT interpret the regime matrix as a result until all "
                "assignment failures have been explained."
            )

        print("=" * 90)
        print(f"Ledger     : {output_dir / 'regime_assignment_ledger.csv'}")
        print(f"Matrix     : {output_dir / 'regime_matrix.csv'}")
        print(f"Periods    : {output_dir / 'regime_period_summary.csv'}")
        print(f"Manifest   : {output_dir / 'regime_run_manifest.json'}")

        return 0 if fail_count == 0 else 2

    except Exception as exc:
        print("\n" + "=" * 90)
        print("AURA v0.5.2 — REGIME BUILDER ERROR")
        print("=" * 90)
        print(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
