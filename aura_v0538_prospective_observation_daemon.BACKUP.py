"""
AURA v0.5.3.7 â€” PROSPECTIVE OBSERVATION DAEMON
Research-only. No orders. No trading.

Purpose:
- Poll Alpaca Crypto historical 1H bars.
- Never append an open/incomplete 1H candle.
- Preserve a warm-up window for indicator calculation.
- Evaluate the FROZEN candidate:
    BEAR Ã— LOW ATR Ã— POSITIVE bar-2
- Record candidate events immediately.
- Complete the event after 4 closed hours and record the forward observation.
- Keep an append-only decision/observation ledger.
- Survive restarts without duplicating bars or observations.

Frozen parameters:
    ATR threshold = 0.596%
    4H EMA       = EMA50
    forward obs  = 4 closed 1H bars
    symbols       = BTC/USD, ETH/USD
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests


VERSION = "AURA v0.5.3.7"
API_BASE = "https://data.alpaca.markets/v1beta3/crypto/us/bars"

SYMBOLS = ["BTC/USD", "ETH/USD"]

ATR_THRESHOLD_PCT = 0.596
EMA_PERIOD_4H = 50
ATR_PERIOD_1H = 14
FORWARD_HOURS = 4

POLL_SECONDS = 300
WARMUP_HOURS = 220

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data" / "prospective_alpaca"
OUT_DIR = ROOT / "regime_output" / "prospective_daemon"

STATE_FILE = OUT_DIR / "daemon_state.json"
BARS_FILE = DATA_DIR / "alpaca_1h_closed_bars.csv"
OBS_FILE = OUT_DIR / "prospective_observations.csv"
EVENT_FILE = OUT_DIR / "candidate_events.csv"
DECISION_FILE = OUT_DIR / "decision_ledger.csv"
LOG_FILE = OUT_DIR / "daemon_log.csv"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def die(msg: str) -> None:
    raise RuntimeError(msg)


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)


def headers() -> dict[str, str]:
    key = os.getenv("APCA_API_KEY_ID")
    secret = os.getenv("APCA_API_SECRET_KEY")
    if not key or not secret:
        die(
            "Missing APCA_API_KEY_ID / APCA_API_SECRET_KEY. "
            "Set them in PowerShell before starting the daemon."
        )
    return {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
        "Accept": "application/json",
    }


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {
            "version": VERSION,
            "last_closed_bar": {},
            "processed_events": [],
            "completed_events": [],
            "started_at": iso(utc_now()),
        }
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_state(state: dict[str, Any]) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def append_csv(path: Path, row: dict[str, Any], fieldnames: list[str]) -> None:
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if not exists:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in fieldnames})


def fetch_bars(symbol: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    params = {
        "symbols": symbol,
        "timeframe": "1Hour",
        "start": iso(start),
        "end": iso(end),
        "limit": 10000,
        "sort": "asc",
    }

    r = requests.get(API_BASE, headers=headers(), params=params, timeout=30)

    if r.status_code != 200:
        die(f"Alpaca HTTP {r.status_code}: {r.text[:1000]}")

    payload = r.json()
    bars = payload.get("bars", {}).get(symbol, [])

    result = []
    for b in bars:
        ts = pd.to_datetime(b["t"], utc=True).to_pydatetime()
        result.append(
            {
                "timestamp": iso(ts),
                "symbol": symbol,
                "open": float(b["o"]),
                "high": float(b["h"]),
                "low": float(b["l"]),
                "close": float(b["c"]),
                "volume": float(b.get("v", 0)),
            }
        )

    return result


def latest_closed_hour(now: datetime | None = None) -> datetime:
    now = now or utc_now()
    floored = now.replace(minute=0, second=0, microsecond=0)
    # The currently running hour is never eligible.
    return floored - timedelta(hours=1)


def read_bars() -> pd.DataFrame:
    if not BARS_FILE.exists():
        return pd.DataFrame(
            columns=["timestamp", "symbol", "open", "high", "low", "close", "volume"]
        )

    df = pd.read_csv(BARS_FILE)
    if df.empty:
        return df

    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return (
        df.drop_duplicates(["symbol", "timestamp"])
        .sort_values(["symbol", "timestamp"])
        .reset_index(drop=True)
    )


def write_bars(df: pd.DataFrame) -> None:
    out = df.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True, errors="coerce").dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    out.to_csv(BARS_FILE, index=False)


def collect_closed_bars() -> tuple[pd.DataFrame, int]:
    df = read_bars()
    end = latest_closed_hour()

    total_new = 0

    for symbol in SYMBOLS:
        existing = df.loc[df["symbol"] == symbol, "timestamp"]
        if len(existing):
            start = existing.max().to_pydatetime() + timedelta(hours=1)
        else:
            start = end - timedelta(hours=WARMUP_HOURS)

        if start > end:
            continue

        rows = fetch_bars(symbol, start, end)
        if not rows:
            continue

        add = pd.DataFrame(rows)
        add["timestamp"] = pd.to_datetime(add["timestamp"], utc=True)

        df = pd.concat([df, add], ignore_index=True)
        total_new += len(add)

    if not df.empty:
        df = (
            df.drop_duplicates(["symbol", "timestamp"])
            .sort_values(["symbol", "timestamp"])
            .reset_index(drop=True)
        )
        write_bars(df)

    return df, total_new


def true_range(g: pd.DataFrame) -> pd.Series:
    prev_close = g["close"].shift(1)
    return pd.concat(
        [
            g["high"] - g["low"],
            (g["high"] - prev_close).abs(),
            (g["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def prepare_symbol(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    g = df[df["symbol"] == symbol].copy().sort_values("timestamp")
    if g.empty:
        return g

    # 1H ATR14, Wilder-style using exponential smoothing.
    tr = true_range(g)
    g["atr14"] = tr.ewm(alpha=1 / ATR_PERIOD_1H, adjust=False, min_periods=ATR_PERIOD_1H).mean()
    g["atr_pct"] = 100.0 * g["atr14"] / g["close"]

    # Bar-2 is deliberately frozen as the two-hour close-to-close return.
    g["bar2_return_pct"] = 100.0 * (g["close"] / g["close"].shift(2) - 1.0)

    return g.reset_index(drop=True)


def btc_4h_context(btc: pd.DataFrame) -> pd.DataFrame:
    if btc.empty:
        return btc

    # Defensive timestamp normalization: resample requires a DatetimeIndex.
    btc = btc.copy()
    btc["timestamp"] = pd.to_datetime(
        btc["timestamp"], utc=True, errors="coerce"
    )
    btc = btc.dropna(subset=["timestamp"])

    x = btc.set_index("timestamp").sort_index()

    # Explicitly guarantee a DatetimeIndex before resampling.
    if not isinstance(x.index, pd.DatetimeIndex):
        die("BTC 4H context: timestamp index is not a DatetimeIndex.")

    h4 = x["close"].resample("4h", label="right", closed="right").last().dropna()
    ema = h4.ewm(span=EMA_PERIOD_4H, adjust=False, min_periods=EMA_PERIOD_4H).mean()

    ctx = pd.DataFrame({"btc_4h_close": h4, "btc_4h_ema50": ema})
    ctx["btc_4h_regime"] = "UNKNOWN"
    ctx.loc[ctx["btc_4h_close"] < ctx["btc_4h_ema50"], "btc_4h_regime"] = "BEAR"
    ctx.loc[ctx["btc_4h_close"] >= ctx["btc_4h_ema50"], "btc_4h_regime"] = "BULL"

    return ctx


def evaluate_new_events(df: pd.DataFrame, state: dict[str, Any]) -> None:
    if df.empty:
        return

    btc = prepare_symbol(df, "BTC/USD")
    eth = prepare_symbol(df, "ETH/USD")
    ctx = btc_4h_context(btc)

    if ctx.empty:
        return

    ctx2 = ctx.reset_index().rename(columns={"timestamp": "context_time"})
    processed = set(state.get("processed_events", []))

    for symbol, g in [("BTC/USD", btc), ("ETH/USD", eth)]:
        if g.empty:
            continue

        x = g.copy()

        # Attach latest completed 4H context at or before each 1H close.
        x = pd.merge_asof(
            x.sort_values("timestamp"),
            ctx2.sort_values("context_time"),
            left_on="timestamp",
            right_on="context_time",
            direction="backward",
        )

        for _, r in x.iterrows():
            ts = r["timestamp"].to_pydatetime()
            event_id = f"{symbol}|{iso(ts)}"

            if event_id in processed:
                continue

            # Only evaluate observations from the prospectively collected
            # dataset after the first 1H bar that has a complete warm-up.
            if pd.isna(r["atr_pct"]) or pd.isna(r["bar2_return_pct"]):
                continue
            if pd.isna(r.get("btc_4h_ema50")):
                continue

            bear = r["btc_4h_regime"] == "BEAR"
            low_atr = float(r["atr_pct"]) <= ATR_THRESHOLD_PCT
            positive_bar2 = float(r["bar2_return_pct"]) > 0.0
            candidate = bear and low_atr and positive_bar2

            row = {
                "event_id": event_id,
                "timestamp": iso(ts),
                "symbol": symbol,
                "agent_version": VERSION,
                "candidate": "BEAR_LOW_ATR_POSITIVE_BAR2",
                "btc_4h_regime": r["btc_4h_regime"],
                "btc_4h_close": r["btc_4h_close"],
                "btc_4h_ema50": r["btc_4h_ema50"],
                "atr14_pct": r["atr_pct"],
                "atr_threshold_pct": ATR_THRESHOLD_PCT,
                "bar2_return_pct": r["bar2_return_pct"],
                "candidate_triggered": candidate,
                "orders_allowed": False,
                "action": "OBSERVE_ONLY" if candidate else "NO_EVENT",
            }

            append_csv(
                EVENT_FILE,
                row,
                list(row.keys()),
            )

            decision = {
                "timestamp": iso(ts),
                "agent_version": VERSION,
                "symbol": symbol,
                "candidate": "BEAR_LOW_ATR_POSITIVE_BAR2",
                "btc_4h_regime": r["btc_4h_regime"],
                "atr14_pct": r["atr_pct"],
                "bar2_return_pct": r["bar2_return_pct"],
                "candidate_triggered": candidate,
                "research_status": "HYPOTHESIS_REJECTED_HISTORICALLY_OBSERVE_PROSPECTIVELY",
                "orders_allowed": False,
                "paper_execution": False,
                "live_execution": False,
                "action": "OBSERVE_ONLY" if candidate else "NO_EVENT",
            }

            append_csv(DECISION_FILE, decision, list(decision.keys()))
            processed.add(event_id)

    state["processed_events"] = sorted(processed)
    save_state(state)


def complete_forward_observations(df: pd.DataFrame, state: dict[str, Any]) -> int:
    if df.empty or not EVENT_FILE.exists():
        return 0

    events = pd.read_csv(EVENT_FILE)
    if events.empty:
        return 0

    events["timestamp"] = pd.to_datetime(events["timestamp"], utc=True)

    completed = set(state.get("completed_events", []))
    count = 0

    for _, e in events[events["candidate_triggered"] == True].iterrows():
        event_id = e["event_id"]
        if event_id in completed:
            continue

        symbol = e["symbol"]
        event_ts = e["timestamp"]
        target_ts = event_ts + pd.Timedelta(hours=FORWARD_HOURS)

        g = df[df["symbol"] == symbol].sort_values("timestamp").copy()

        future = g[g["timestamp"] >= target_ts]
        if future.empty:
            continue

        exit_row = future.iloc[0]

        entry_rows = g[g["timestamp"] == event_ts]
        if entry_rows.empty:
            continue

        entry_close = float(entry_rows.iloc[0]["close"])
        exit_close = float(exit_row["close"])

        forward_return_pct = 100.0 * (exit_close / entry_close - 1.0)

        row = {
            "event_id": event_id,
            "event_timestamp": iso(event_ts.to_pydatetime()),
            "symbol": symbol,
            "agent_version": VERSION,
            "entry_close": entry_close,
            "observation_timestamp": iso(exit_row["timestamp"].to_pydatetime()),
            "observation_close": exit_close,
            "forward_hours": FORWARD_HOURS,
            "forward_return_pct": forward_return_pct,
            "complete": True,
            "orders_allowed": False,
            "interpretation": "PROSPECTIVE_OBSERVATION_ONLY",
        }

        append_csv(OBS_FILE, row, list(row.keys()))
        completed.add(event_id)
        count += 1

    state["completed_events"] = sorted(completed)
    save_state(state)
    return count


def print_status(df: pd.DataFrame, new_rows: int, completed: int) -> None:
    latest = latest_closed_hour()

    print()
    print("=" * 88)
    print(f"{VERSION} â€” PROSPECTIVE OBSERVATION DAEMON")
    print("=" * 88)
    print("MODE                 : RESEARCH ONLY â€” NO ORDERS")
    print("CONTROL              : C0 FROZEN")
    print("CANDIDATE            : BEAR Ã— LOW ATR Ã— POSITIVE bar-2")
    print(f"ATR THRESHOLD        : {ATR_THRESHOLD_PCT:.3f}%")
    print(f"4H EMA               : EMA{EMA_PERIOD_4H}")
    print(f"FORWARD OBSERVATION  : {FORWARD_HOURS}H")
    print("ORDERS ALLOWED       : False")
    print("PAPER EXECUTION      : False")
    print("LIVE EXECUTION       : False")
    print(f"LATEST CLOSED HOUR   : {iso(latest)}")
    print()
    print(f"NEW CLOSED BARS      : {new_rows}")
    print(f"COMPLETED OBSERVATIONS: {completed}")

    if not df.empty:
        for symbol in SYMBOLS:
            g = df[df["symbol"] == symbol]
            if not g.empty:
                print(
                    f"{symbol:<20}: {len(g):>6} bars | "
                    f"{iso(g['timestamp'].min().to_pydatetime())} -> "
                    f"{iso(g['timestamp'].max().to_pydatetime())}"
                )

    if OBS_FILE.exists():
        obs = pd.read_csv(OBS_FILE)
        print(f"PROSPECTIVE N        : {len(obs)}")
        if len(obs):
            mean = obs["forward_return_pct"].mean()
            hit = (obs["forward_return_pct"] > 0).mean() * 100
            print(f"MEAN FORWARD RETURN  : {mean:+.4f}%")
            print(f"HIT RATE             : {hit:.1f}%")

    print()
    print("STATUS               : RUNNING â€” WAITING FOR NEXT CLOSED 1H BAR")
    print("OUTPUT               :", OUT_DIR)
    print("=" * 88)


def run_once() -> None:
    ensure_dirs()
    state = load_state()

    df, new_rows = collect_closed_bars()
    evaluate_new_events(df, state)
    completed = complete_forward_observations(df, state)
    print_status(df, new_rows, completed)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run one polling cycle and exit.")
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=POLL_SECONDS,
        help="Polling interval. Default 300 seconds.",
    )
    args = parser.parse_args()

    ensure_dirs()

    print(f"{VERSION} starting...")
    print("Research-only. Orders are permanently disabled in this program.")

    if args.once:
        run_once()
        return

    while True:
        try:
            run_once()
        except KeyboardInterrupt:
            print("\nDaemon stopped by user.")
            return
        except Exception as exc:
            print()
            print("DAEMON ERROR:", type(exc).__name__, str(exc))
            print("Will retry on the next polling cycle.")

        time.sleep(max(30, args.poll_seconds))


if __name__ == "__main__":
    main()




