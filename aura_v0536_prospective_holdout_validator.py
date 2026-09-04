#!/usr/bin/env python3
"""
AURA v0.5.3.6
PROSPECTIVE HOLDOUT VALIDATOR

Purpose
-------
Evaluate the FROZEN AURA research candidate on genuinely prospective Alpaca
1H observations collected after the immutable holdout boundary.

Candidate:
    BEAR x LOW ATR x POSITIVE bar-2

Frozen parameters:
    ATR threshold = 0.596%
    4H trend      = EMA50
    bar-2         = 2-hour close-to-close return > 0

Safety
------
- RESEARCH ONLY
- NO ORDERS
- NO strategy parameter optimization
- Holdout observations are never used to alter the candidate
- Warm-up data is used only for indicator initialization
- Current/open 1H candle is never evaluated
- 4H EMA50 is independently fetched with sufficient warm-up
- Output explicitly separates SIGNAL OBSERVATION from STRATEGY P&L

IMPORTANT
---------
This validator reports forward returns as OBSERVATION METRICS.  It does not
claim they are the historical "net_return" used by earlier AURA research,
because that would require the exact frozen entry/exit/cost specification.
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

ALPACA_1H_URL = "https://data.alpaca.markets/v1beta3/crypto/us/bars"
ALPACA_4H_URL = "https://data.alpaca.markets/v1beta3/crypto/us/bars"

DEFAULT_HOLDOUT_START = "2026-08-26T17:00:00Z"
DEFAULT_ATR_THRESHOLD_PCT = 0.596
DEFAULT_FORWARD_HOURS = 4
DEFAULT_EMA_PERIOD = 50
DEFAULT_EMA_WARMUP_4H = 220

SYMBOLS = {
    "BTC/USD": "BTCUSD",
    "ETH/USD": "ETHUSD",
}

HOLDOUT_COLUMNS = [
    "timestamp", "open", "high", "low", "close",
    "volume", "trade_count", "vwap"
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


def latest_closed_hour():
    return floor_hour(datetime.now(timezone.utc)) - timedelta(hours=1)


def auth_headers():
    key = os.getenv("APCA_API_KEY_ID", "").strip()
    secret = os.getenv("APCA_API_SECRET_KEY", "").strip()

    if not key or not secret:
        die("Missing APCA_API_KEY_ID / APCA_API_SECRET_KEY environment variables.")

    return {
        "Accept": "application/json",
        "User-Agent": "AURA-v0.5.3.6",
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
    }


def fetch_bars(symbol, timeframe, start_dt, end_dt):
    rows = []
    token = None

    while True:
        params = {
            "symbols": symbol,
            "timeframe": timeframe,
            "start": iso_z(start_dt),
            "end": iso_z(end_dt),
            "limit": 10000,
            "sort": "asc",
        }
        if token:
            params["page_token"] = token

        req = Request(
            ALPACA_1H_URL + "?" + urlencode(params),
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

        for bar in payload.get("bars", {}).get(symbol, []):
            ts = bar.get("t")
            if not ts:
                continue
            rows.append({
                "timestamp": iso_z(parse_utc(ts)),
                "open": float(bar["o"]),
                "high": float(bar["h"]),
                "low": float(bar["l"]),
                "close": float(bar["c"]),
                "volume": float(bar.get("v", 0)),
                "trade_count": bar.get("n"),
                "vwap": bar.get("vw"),
            })

        token = payload.get("next_page_token")
        if not token:
            break
        time.sleep(0.10)

    return rows


def read_csv(path):
    if not path.exists():
        die(f"Missing file: {path}")
    rows = []
    with path.open("r", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    return rows


def numeric_rows(rows):
    out = []
    for r in rows:
        try:
            x = dict(r)
            x["dt"] = parse_utc(r["timestamp"])
            for k in ("open", "high", "low", "close"):
                x[k] = float(r[k])
            out.append(x)
        except Exception:
            continue
    return sorted(out, key=lambda z: z["dt"])


def true_range(prev_close, row):
    return max(
        row["high"] - row["low"],
        abs(row["high"] - prev_close),
        abs(row["low"] - prev_close),
    )


def add_atr(rows, period=14):
    rows = [dict(r) for r in rows]
    trs = []
    for i, r in enumerate(rows):
        if i == 0:
            trs.append(None)
        else:
            trs.append(true_range(rows[i-1]["close"], r))

    # Wilder ATR: seed with SMA, then recursive smoothing.
    atr = [None] * len(rows)
    if len(rows) >= period + 1:
        seed = [x for x in trs[1:period+1] if x is not None]
        if len(seed) == period:
            atr[period] = sum(seed) / period
            for i in range(period + 1, len(rows)):
                atr[i] = ((atr[i-1] * (period - 1)) + trs[i]) / period

    for i, r in enumerate(rows):
        r["atr14_pct"] = (
            (atr[i] / r["close"]) * 100.0
            if atr[i] is not None and r["close"] != 0
            else None
        )
    return rows


def resample_4h(rows):
    """
    Build completed UTC 4H candles from 1H bars.
    4H buckets begin at 00,04,08,12,16,20 UTC.
    """
    buckets = {}
    for r in rows:
        dt = r["dt"]
        bucket_hour = (dt.hour // 4) * 4
        start = dt.replace(hour=bucket_hour, minute=0, second=0, microsecond=0)
        buckets.setdefault(start, []).append(r)

    out = []
    for start in sorted(buckets):
        group = sorted(buckets[start], key=lambda x: x["dt"])
        if len(group) != 4:
            continue
        out.append({
            "dt": start,
            "open": group[0]["open"],
            "high": max(x["high"] for x in group),
            "low": min(x["low"] for x in group),
            "close": group[-1]["close"],
            "complete": True,
        })
    return out


def ema(values, period):
    if len(values) < period:
        return [None] * len(values)

    out = [None] * len(values)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    alpha = 2.0 / (period + 1.0)

    for i in range(period, len(values)):
        out[i] = (values[i] - out[i-1]) * alpha + out[i-1]
    return out


def build_4h_regime_map(rows, period):
    bars = resample_4h(rows)
    if len(bars) < period:
        die(
            f"Insufficient completed 4H bars for EMA{period}: "
            f"{len(bars)} available, {period} required."
        )

    values = [x["close"] for x in bars]
    emas = ema(values, period)

    out = []
    for b, e in zip(bars, emas):
        if e is None:
            continue
        out.append({
            "dt": b["dt"],
            "close": b["close"],
            "ema": e,
            "regime": "BEAR" if b["close"] < e else "BULL",
        })
    return out


def regime_at_or_before(regimes, ts):
    best = None
    for r in regimes:
        if r["dt"] <= ts:
            best = r
        else:
            break
    return best


def pct_return(a, b):
    if a == 0:
        return None
    return (b / a - 1.0) * 100.0


def bootstrap_ci(values, seed=20260827, iterations=5000):
    import random
    vals = [float(x) for x in values if x is not None]
    n = len(vals)
    if n < 2:
        return None, None

    rng = random.Random(seed)
    means = []
    for _ in range(iterations):
        sample = [vals[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)

    means.sort()
    lo = means[int(0.025 * len(means))]
    hi = means[int(0.975 * len(means)) - 1]
    return lo, hi


def evidence_status(n):
    if n < 10:
        return "INSUFFICIENT_EVIDENCE"
    if n < 20:
        return "PRELIMINARY_INCONCLUSIVE"
    return "FORMAL_HOLDOUT_EVALUATION"


def evaluate_symbol(symbol, stem, holdout_start, latest_closed,
                    output_dir, atr_threshold, forward_hours, ema_period):
    holdout_path = output_dir / f"{stem}_1h_alpaca_holdout.csv"
    warmup_path = output_dir / f"{stem}_1h_alpaca_warmup.csv"

    holdout = numeric_rows(read_csv(holdout_path))
    warmup = numeric_rows(read_csv(warmup_path))

    all_1h = sorted(warmup + holdout, key=lambda x: x["dt"])
    all_1h = [x for x in all_1h if x["dt"] <= latest_closed]

    # Fetch independent 4H context with enough warm-up for EMA50.
    context_end = latest_closed + timedelta(hours=4)
    context_start = context_end - timedelta(hours=4 * 250)
    context = numeric_rows(fetch_bars(symbol, "4Hour", context_start, context_end))
    context = [x for x in context if x["dt"] <= latest_closed]

    if len(context) < ema_period:
        die(
            f"{symbol}: only {len(context)} completed 4H bars available; "
            f"EMA{ema_period} requires at least {ema_period}."
        )

    regimes = []
    closes = [x["close"] for x in context]
    emas = ema(closes, ema_period)
    for r, e in zip(context, emas):
        if e is not None:
            regimes.append({
                "dt": r["dt"],
                "close": r["close"],
                "ema": e,
                "regime": "BEAR" if r["close"] < e else "BULL",
            })

    all_1h = add_atr(all_1h, 14)

    # Only bars in the frozen prospective window can become observations.
    candidates = []
    diagnostic = []

    for i, r in enumerate(all_1h):
        if r["dt"] < holdout_start or r["dt"] > latest_closed:
            continue

        prev2 = all_1h[i-2]["close"] if i >= 2 else None
        bar2 = pct_return(prev2, r["close"]) if prev2 is not None else None

        reg = regime_at_or_before(regimes, r["dt"])
        bear = reg is not None and reg["regime"] == "BEAR"
        low_atr = r["atr14_pct"] is not None and r["atr14_pct"] < atr_threshold
        positive = bar2 is not None and bar2 > 0

        is_candidate = bear and low_atr and positive

        row = {
            "symbol": symbol,
            "timestamp": iso_z(r["dt"]),
            "close": r["close"],
            "atr14_pct": r["atr14_pct"],
            "atr_threshold_pct": atr_threshold,
            "bar_2_close_return_pct": bar2,
            "btc_4h_regime": reg["regime"] if reg else None,
            "btc_4h_close": reg["close"] if reg else None,
            "btc_4h_ema50": reg["ema"] if reg else None,
            "candidate": is_candidate,
            "forward_1h_return_pct": None,
            "forward_4h_return_pct": None,
            "forward_hours_available": 0,
            "note": "",
        }

        if is_candidate:
            # Returns are measured from the signal bar close, only when all
            # future bars are already closed. These are observation metrics.
            target_1h = r["dt"] + timedelta(hours=1)
            target_4h = r["dt"] + timedelta(hours=forward_hours)

            future_1h = next((x for x in all_1h if x["dt"] == target_1h), None)
            future_4h = next((x for x in all_1h if x["dt"] == target_4h), None)

            if future_1h:
                row["forward_1h_return_pct"] = pct_return(r["close"], future_1h["close"])
                row["forward_hours_available"] = 1

            if future_4h:
                row["forward_4h_return_pct"] = pct_return(r["close"], future_4h["close"])
                row["forward_hours_available"] = forward_hours

            if future_4h is None:
                row["note"] = "CANDIDATE_OBSERVED_BUT_FORWARD_HORIZON_NOT_COMPLETE"

            candidates.append(row)

        diagnostic.append(row)

    return {
        "symbol": symbol,
        "total_holdout_rows": len([x for x in holdout if holdout_start <= x["dt"] <= latest_closed]),
        "candidate_rows": candidates,
        "diagnostic_rows": diagnostic,
        "ema_context_bars": len(context),
        "ema_period": ema_period,
    }


def write_csv(path, rows, columns):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--holdout-start", default=DEFAULT_HOLDOUT_START)
    p.add_argument("--data-dir", default=r"./data/holdout_alpaca")
    p.add_argument("--output-dir", default=r"./regime_output/prospective_holdout")
    p.add_argument("--atr-threshold", type=float, default=DEFAULT_ATR_THRESHOLD_PCT)
    p.add_argument("--forward-hours", type=int, default=DEFAULT_FORWARD_HOURS)
    p.add_argument("--ema-period", type=int, default=DEFAULT_EMA_PERIOD)
    args = p.parse_args()

    holdout_start = parse_utc(args.holdout_start)
    latest_closed = latest_closed_hour()

    data_dir = Path(args.data_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if holdout_start > latest_closed:
        die(
            f"Holdout start {iso_z(holdout_start)} is after latest closed hour "
            f"{iso_z(latest_closed)}."
        )

    print("=" * 88)
    print("AURA v0.5.3.6 - PROSPECTIVE HOLDOUT VALIDATOR")
    print("=" * 88)
    print("MODE                : RESEARCH ONLY - NO ORDERS")
    print("CONTROL             : C0 FROZEN")
    print("CANDIDATE           : BEAR x LOW ATR x POSITIVE bar-2")
    print(f"ATR THRESHOLD      : {args.atr_threshold:.3f}%")
    print(f"4H EMA              : EMA{args.ema_period}")
    print(f"HOLDOUT START       : {iso_z(holdout_start)}")
    print(f"LATEST CLOSED HOUR  : {iso_z(latest_closed)}")
    print(f"FORWARD OBSERVATION: {args.forward_hours}H")
    print("ORDERS ALLOWED      : False")
    print("PAPER EXECUTION     : False")
    print("LIVE EXECUTION      : False")
    print()
    print("IMPORTANT: forward returns are observation metrics, NOT claimed strategy P&L.")
    print("The historical exit/cost model remains frozen and is not re-created here.")
    print()

    results = []
    all_candidates = []
    all_diagnostics = []

    for symbol, stem in SYMBOLS.items():
        print(f"VALIDATING {symbol}")
        print("-" * 88)

        r = evaluate_symbol(
            symbol=symbol,
            stem=stem,
            holdout_start=holdout_start,
            latest_closed=latest_closed,
            output_dir=data_dir,
            atr_threshold=args.atr_threshold,
            forward_hours=args.forward_hours,
            ema_period=args.ema_period,
        )
        results.append(r)
        all_candidates.extend(r["candidate_rows"])
        all_diagnostics.extend(r["diagnostic_rows"])

        usable = [
            x for x in r["candidate_rows"]
            if x["forward_4h_return_pct"] is not None
        ]
        print(f"Holdout rows       : {r['total_holdout_rows']}")
        print(f"Candidate events   : {len(r['candidate_rows'])}")
        print(f"Usable {args.forward_hours}H obs : {len(usable)}")
        if usable:
            vals = [x["forward_4h_return_pct"] for x in usable]
            mean = sum(vals) / len(vals)
            hit = sum(1 for x in vals if x > 0) / len(vals) * 100.0
            lo, hi = bootstrap_ci(vals)
            print(f"Mean forward return: {mean:+.3f}%")
            print(f"Hit rate           : {hit:.1f}%")
            print(
                f"Bootstrap 95% CI   : "
                f"{lo:+.3f}% to {hi:+.3f}%"
            )
        else:
            print("No complete forward observations yet.")
        print()

    usable_all = [
        x for x in all_candidates
        if x["forward_4h_return_pct"] is not None
    ]

    vals = [x["forward_4h_return_pct"] for x in usable_all]
    n = len(vals)
    mean = sum(vals) / n if n else None
    median = sorted(vals)[n // 2] if n else None
    hit = (sum(1 for x in vals if x > 0) / n * 100.0) if n else None
    lo, hi = bootstrap_ci(vals) if n else (None, None)

    observation_cols = [
        "symbol", "timestamp", "close", "atr14_pct",
        "atr_threshold_pct", "bar_2_close_return_pct",
        "btc_4h_regime", "btc_4h_close", "btc_4h_ema50",
        "candidate", "forward_1h_return_pct",
        "forward_4h_return_pct", "forward_hours_available", "note"
    ]

    write_csv(
        output_dir / "prospective_candidate_observations.csv",
        all_candidates,
        observation_cols,
    )

    write_csv(
        output_dir / "prospective_signal_diagnostics.csv",
        all_diagnostics,
        observation_cols,
    )

    summary = {
        "agent_version": "AURA v0.5.3.6",
        "mode": "RESEARCH_ONLY",
        "orders_allowed": False,
        "candidate": "BEAR_LOW_ATR_POSITIVE_BAR2",
        "atr_threshold_pct": args.atr_threshold,
        "ema_period": args.ema_period,
        "holdout_start": iso_z(holdout_start),
        "latest_closed_hour": iso_z(latest_closed),
        "forward_observation_hours": args.forward_hours,
        "btc_candidate_events": sum(
            1 for x in all_candidates if x["symbol"] == "BTC/USD"
        ),
        "eth_candidate_events": sum(
            1 for x in all_candidates if x["symbol"] == "ETH/USD"
        ),
        "combined_candidate_events": len(all_candidates),
        "valid_holdout_observations": n,
        "mean_forward_return_pct": mean,
        "median_forward_return_pct": median,
        "hit_rate_pct": hit,
        "bootstrap_95_ci_low_pct": lo,
        "bootstrap_95_ci_high_pct": hi,
        "evidence_status": evidence_status(n),
        "interpretation": (
            "PROSPECTIVE OBSERVATION ONLY. No strategy deployment decision "
            "is permitted from this run."
        ),
        "files": {
            "observations": str(output_dir / "prospective_candidate_observations.csv"),
            "diagnostics": str(output_dir / "prospective_signal_diagnostics.csv"),
            "summary": str(output_dir / "prospective_holdout_summary.json"),
        },
    }

    (output_dir / "prospective_holdout_summary.json").write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )

    print("=" * 88)
    print("PROSPECTIVE HOLDOUT RESULT")
    print("=" * 88)
    print(f"BTC candidate events : {summary['btc_candidate_events']}")
    print(f"ETH candidate events : {summary['eth_candidate_events']}")
    print(f"VALID HOLDOUT N      : {n}")

    if n:
        print(f"Mean forward return  : {mean:+.3f}%")
        print(f"Median forward return: {median:+.3f}%")
        print(f"Hit rate             : {hit:.1f}%")
        print(f"Bootstrap 95% CI     : {lo:+.3f}% to {hi:+.3f}%")
    else:
        print("Mean forward return  : N/A")
        print("Median forward return: N/A")
        print("Hit rate             : N/A")
        print("Bootstrap 95% CI     : N/A")

    print(f"EVIDENCE STATUS      : {summary['evidence_status']}")
    print()
    print("GUARDRAILS")
    print("Strategy filter      : NO")
    print("Strategy changed     : NO")
    print("Orders allowed       : NO")
    print("Live execution       : NO")
    print("Interpretation       : RESEARCH OBSERVATION ONLY")
    print()
    print("FILES")
    print(f"Observations         : {output_dir / 'prospective_candidate_observations.csv'}")
    print(f"Diagnostics          : {output_dir / 'prospective_signal_diagnostics.csv'}")
    print(f"Summary              : {output_dir / 'prospective_holdout_summary.json'}")
    print("=" * 88)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nValidation cancelled by user.")
        sys.exit(130)
    except Exception as exc:
        print("\nAURA v0.5.3.6 ERROR")
        print("-" * 88)
        print(str(exc))
        sys.exit(1)
