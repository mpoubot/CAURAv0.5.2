import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

VERSION = "AURA v0.5.3.2"
CANDIDATE = "BEAR_LOW_ATR_POSITIVE_BAR2"

# C0 FROZEN — DO NOT CHANGE FOR THIS DIAGNOSTIC RUN
ATR_THRESHOLD = 0.00596
EMA_SPAN = 50

ORDERS_ALLOWED = False
PAPER_EXECUTION = False
LIVE_EXECUTION = False


def die(msg):
    raise RuntimeError(msg)


def parse_utc(value):
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def load_bars(path, symbol):
    p = Path(path)
    if not p.exists():
        die(f"{symbol}: market data file not found: {p}")

    df = pd.read_csv(p)

    required = {"open", "high", "low", "close"}
    missing = required - set(df.columns)
    if missing:
        die(f"{symbol}: missing columns: {sorted(missing)}")

    if "open_time" in df.columns:
        time_column = "open_time"
    elif "timestamp" in df.columns:
        time_column = "timestamp"
    else:
        die(
            f"{symbol}: no supported timestamp column. "
            f"Expected open_time or timestamp. Found: {list(df.columns)}"
        )

    df["ts"] = pd.to_datetime(df[time_column], utc=True, errors="coerce")

    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = (
        df.dropna(subset=["ts", "open", "high", "low", "close"])
        .sort_values("ts")
        .drop_duplicates("ts")
        .reset_index(drop=True)
    )

    if df.empty:
        die(f"{symbol}: no usable market rows after parsing")

    return df


def prepare_asset_bars(df):
    x = df.copy()

    prev_close = x["close"].shift(1)
    tr1 = x["high"] - x["low"]
    tr2 = (x["high"] - prev_close).abs()
    tr3 = (x["low"] - prev_close).abs()

    x["true_range"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    x["atr14"] = x["true_range"].rolling(14, min_periods=14).mean()
    x["atr_pct"] = x["atr14"] / x["close"]

    # Preserve the frozen C0 definition:
    # POSITIVE means the 2-bar close-to-close return is >= 0.
    x["bar2_return"] = x["close"].pct_change(2)

    x["atr_regime"] = np.where(
        x["atr_pct"].notna(),
        np.where(x["atr_pct"] < ATR_THRESHOLD, "LOW", "HIGH"),
        "UNAVAILABLE",
    )

    x["bar2_regime"] = np.where(
        x["bar2_return"].notna(),
        np.where(x["bar2_return"] >= 0, "POSITIVE", "NEGATIVE"),
        "UNAVAILABLE",
    )

    return x


def build_btc_4h_reference(btc):
    h4 = (
        btc.set_index("ts")["close"]
        .resample("4h")
        .last()
        .dropna()
        .to_frame("btc_4h_close")
    )

    h4["btc_4h_ema50"] = h4["btc_4h_close"].ewm(
        span=EMA_SPAN,
        adjust=False,
        min_periods=EMA_SPAN,
    ).mean()

    h4["btc_4h_regime"] = np.where(
        h4["btc_4h_ema50"].notna(),
        np.where(h4["btc_4h_close"] < h4["btc_4h_ema50"], "BEAR", "BULL"),
        "UNAVAILABLE",
    )

    return h4


def apply_btc_reference(asset_df, btc_h4):
    x = asset_df.copy().set_index("ts")

    x = x.join(
        btc_h4[["btc_4h_close", "btc_4h_ema50", "btc_4h_regime"]],
        how="left",
    )

    ref_cols = ["btc_4h_close", "btc_4h_ema50", "btc_4h_regime"]
    x[ref_cols] = x[ref_cols].ffill()

    x["candidate"] = (
        (x["btc_4h_regime"] == "BEAR")
        & (x["atr_regime"] == "LOW")
        & (x["bar2_regime"] == "POSITIVE")
    )

    return x.reset_index()


def diagnostics_for_symbol(df, symbol, holdout_start):
    holdout_start = parse_utc(holdout_start)

    holdout = df[df["ts"] >= holdout_start].copy()

    result = {
        "symbol": symbol,
        "total_bars": int(len(df)),
        "first_timestamp": str(df["ts"].min()),
        "last_timestamp": str(df["ts"].max()),
        "holdout_start": str(holdout_start),
        "holdout_bars": int(len(holdout)),
        "holdout_first_timestamp": str(holdout["ts"].min()) if not holdout.empty else None,
        "holdout_last_timestamp": str(holdout["ts"].max()) if not holdout.empty else None,
    }

    if holdout.empty:
        result.update(
            {
                "gate_bear": 0,
                "gate_low_atr": 0,
                "gate_positive_bar2": 0,
                "candidate_events": 0,
                "diagnosis": "NO_DATA_AT_OR_AFTER_HOLDOUT_START",
            }
        )
        return result

    result["gate_bear"] = int((holdout["btc_4h_regime"] == "BEAR").sum())
    result["gate_low_atr"] = int((holdout["atr_regime"] == "LOW").sum())
    result["gate_positive_bar2"] = int((holdout["bar2_regime"] == "POSITIVE").sum())
    result["gate_usable_ema"] = int(
        holdout["btc_4h_ema50"].notna().sum()
    )
    result["gate_usable_atr"] = int(
        holdout["atr_pct"].notna().sum()
    )
    result["gate_usable_bar2"] = int(
        holdout["bar2_return"].notna().sum()
    )
    result["candidate_events"] = int(holdout["candidate"].sum())

    # Stepwise intersection counts show exactly which gate removes observations.
    step1 = holdout[
        holdout["btc_4h_regime"].eq("BEAR")
    ]
    step2 = step1[
        step1["atr_regime"].eq("LOW")
    ]
    step3 = step2[
        step2["bar2_regime"].eq("POSITIVE")
    ]

    result["intersection_bear"] = int(len(step1))
    result["intersection_bear_low_atr"] = int(len(step2))
    result["intersection_candidate"] = int(len(step3))

    if result["candidate_events"] > 0:
        result["diagnosis"] = "CANDIDATE_EVENTS_PRESENT"
    elif result["gate_bear"] == 0:
        result["diagnosis"] = "NO_BEAR_REFERENCE_IN_HOLDOUT"
    elif result["gate_low_atr"] == 0:
        result["diagnosis"] = "NO_LOW_ATR_IN_HOLDOUT"
    elif result["gate_positive_bar2"] == 0:
        result["diagnosis"] = "NO_POSITIVE_BAR2_IN_HOLDOUT"
    else:
        result["diagnosis"] = "COMBINATION_OF_GATES_ELIMINATED_ALL_EVENTS"

    return result


def make_event_ledger(df, symbol, holdout_start):
    holdout_start = parse_utc(holdout_start)
    full = df.copy()

    # Observation is measured only after the signal timestamp.
    full["next_close"] = full["close"].shift(-1)
    full["observation_return"] = (
        full["next_close"] / full["close"] - 1.0
    )

    events = full[
        (full["ts"] >= holdout_start)
        & full["candidate"]
        & full["observation_return"].notna()
    ].copy()

    events["symbol"] = symbol
    events["candidate_name"] = CANDIDATE
    events["holdout"] = True

    return events


def main():
    parser = argparse.ArgumentParser(
        description="AURA v0.5.3.2 prospective holdout diagnostic engine"
    )
    parser.add_argument(
        "--btc",
        default=r".\data\raw\BTCUSDT_1h_raw.csv",
    )
    parser.add_argument(
        "--eth",
        default=r".\data\raw\ETHUSDT_1h_raw.csv",
    )
    parser.add_argument(
        "--holdout-start",
        required=True,
        help="UTC timestamp, e.g. 2026-08-26T17:00:00Z",
    )
    parser.add_argument(
        "--output",
        default=r".\regime_output\prospective_holdout_diagnostic",
    )
    args = parser.parse_args()

    holdout_start = parse_utc(args.holdout_start)

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    print("=" * 82)
    print(f"{VERSION} - PROSPECTIVE HOLDOUT DIAGNOSTIC ENGINE")
    print("=" * 82)
    print("MODE                 : RESEARCH ONLY - NO ORDERS")
    print("CONTROL              : C0 FROZEN")
    print(f"CANDIDATE            : {CANDIDATE}")
    print(f"ATR THRESHOLD        : {ATR_THRESHOLD:.4%}")
    print(f"4H EMA               : EMA{EMA_SPAN}")
    print(f"HOLDOUT START        : {holdout_start}")
    print(f"ORDERS ALLOWED       : {ORDERS_ALLOWED}")
    print(f"PAPER EXECUTION      : {PAPER_EXECUTION}")
    print(f"LIVE EXECUTION       : {LIVE_EXECUTION}")
    print()

    btc = prepare_asset_bars(load_bars(args.btc, "BTC"))
    eth = prepare_asset_bars(load_bars(args.eth, "ETH"))

    btc_h4 = build_btc_4h_reference(btc)

    btc = apply_btc_reference(btc, btc_h4)
    eth = apply_btc_reference(eth, btc_h4)

    btc_diag = diagnostics_for_symbol(btc, "BTC", holdout_start)
    eth_diag = diagnostics_for_symbol(eth, "ETH", holdout_start)

    print("DATA COVERAGE")
    print("-" * 82)

    for d in [btc_diag, eth_diag]:
        print(f"{d['symbol']}")
        print(f"  Total bars          : {d['total_bars']:,}")
        print(f"  First timestamp     : {d['first_timestamp']}")
        print(f"  Last timestamp      : {d['last_timestamp']}")
        print(f"  Holdout bars        : {d['holdout_bars']:,}")
        print(
            f"  Holdout first       : "
            f"{d['holdout_first_timestamp'] or 'NONE'}"
        )
        print(
            f"  Holdout last        : "
            f"{d['holdout_last_timestamp'] or 'NONE'}"
        )
        print(f"  Diagnosis           : {d['diagnosis']}")
        print()

    print("GATE DIAGNOSTICS")
    print("-" * 82)

    for d in [btc_diag, eth_diag]:
        print(f"{d['symbol']}")
        print(f"  BEAR bars           : {d['gate_bear']:,}")
        print(f"  LOW ATR bars        : {d['gate_low_atr']:,}")
        print(f"  POSITIVE bar-2      : {d['gate_positive_bar2']:,}")
        print(f"  Usable EMA          : {d.get('gate_usable_ema', 0):,}")
        print(f"  Usable ATR          : {d.get('gate_usable_atr', 0):,}")
        print(f"  Usable bar-2        : {d.get('gate_usable_bar2', 0):,}")
        print(f"  BEAR ∩ LOW ATR      : {d.get('intersection_bear_low_atr', 0):,}")
        print(f"  FINAL CANDIDATE     : {d['candidate_events']:,}")
        print()

    btc_events = make_event_ledger(btc, "BTC", holdout_start)
    eth_events = make_event_ledger(eth, "ETH", holdout_start)

    events = pd.concat(
        [btc_events, eth_events],
        ignore_index=True,
    )

    if not events.empty:
        events = events.sort_values("ts").reset_index(drop=True)

    print("PROSPECTIVE RESULT")
    print("-" * 82)
    print(f"BTC candidate events  : {len(btc_events):,}")
    print(f"ETH candidate events  : {len(eth_events):,}")
    print(f"VALID HOLDOUT N       : {len(events):,}")
    print()

    if len(events) == 0:
        print("STATUS                : NO PROSPECTIVE OBSERVATIONS YET")
        print(
            "INTERPRETATION       : "
            "This is a data-availability/qualification result, "
            "NOT a strategy failure."
        )
        print()
        print("IMPORTANT")
        print(
            "Do NOT change ATR, EMA, bar-2, or the candidate definition "
            "to manufacture observations."
        )

    else:
        values = events["observation_return"].astype(float).to_numpy()
        mean_return = float(np.mean(values))
        median_return = float(np.median(values))
        hit_rate = float(np.mean(values > 0))

        print(f"Mean observation ret. : {mean_return:+.3%}")
        print(f"Median observation    : {median_return:+.3%}")
        print(f"Hit rate              : {hit_rate:.1%}")

    # Files
    ledger_cols = [
        "ts",
        "symbol",
        "candidate_name",
        "btc_4h_regime",
        "btc_4h_close",
        "btc_4h_ema50",
        "atr_pct",
        "atr_regime",
        "bar2_return",
        "bar2_regime",
        "observation_return",
        "holdout",
    ]

    ledger_path = out / "prospective_decision_ledger.csv"
    events.reindex(columns=ledger_cols).to_csv(
        ledger_path,
        index=False,
    )

    diagnostics = pd.DataFrame([btc_diag, eth_diag])
    diagnostics_path = out / "holdout_gate_diagnostics.csv"
    diagnostics.to_csv(diagnostics_path, index=False)

    summary = {
        "version": VERSION,
        "candidate": CANDIDATE,
        "holdout_start": str(holdout_start),
        "orders_allowed": False,
        "paper_execution": False,
        "live_execution": False,
        "c0_frozen": True,
        "btc": btc_diag,
        "eth": eth_diag,
        "valid_holdout_n": int(len(events)),
        "status": (
            "NO_PROSPECTIVE_OBSERVATIONS_YET"
            if len(events) == 0
            else "CANDIDATE_EVENTS_PRESENT"
        ),
        "parameters": {
            "atr_threshold": ATR_THRESHOLD,
            "ema_span": EMA_SPAN,
            "bar2_rule": ">= 0",
            "trend_reference": "BTC_4H_EMA50",
        },
    }

    json_path = out / "holdout_diagnostic_summary.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str)

    print()
    print("FILES")
    print("-" * 82)
    print(f"Decision ledger       : {ledger_path}")
    print(f"Gate diagnostics      : {diagnostics_path}")
    print(f"JSON summary          : {json_path}")
    print()
    print("=" * 82)
    print("FINAL STATUS")
    print("=" * 82)

    if len(events) == 0:
        print("NO PROSPECTIVE OBSERVATIONS YET")
        print("RESEARCH ONLY - NO ORDERS")
    else:
        print("CANDIDATE EVENTS PRESENT")
        print("RESEARCH ONLY - NO ORDERS")

    print("=" * 82)


if __name__ == "__main__":
    main()
