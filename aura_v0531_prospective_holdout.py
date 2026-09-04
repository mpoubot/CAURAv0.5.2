import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


VERSION = "AURA v0.5.3.1"
CANDIDATE = "BEAR_LOW_ATR_POSITIVE_BAR2"

ATR_THRESHOLD = 0.00596
EMA_SPAN = 50

ORDERS_ALLOWED = False
PAPER_EXECUTION = False
LIVE_EXECUTION = False


def die(msg):
    raise RuntimeError(msg)


def load_bars(path, symbol):
    p = Path(path)

    if not p.exists():
        die(f"Market data file not found: {p}")

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
            f"Expected 'open_time' or 'timestamp'. "
            f"Found: {list(df.columns)}"
        )

    df["ts"] = pd.to_datetime(
        df[time_column],
        utc=True,
        errors="coerce"
    )

    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = (
        df.dropna(subset=["ts", "open", "high", "low", "close"])
          .sort_values("ts")
          .drop_duplicates("ts")
          .reset_index(drop=True)
    )

    return df


def prepare_bars(df):
    x = df.copy()

    # 1H ATR(14)
    prev_close = x["close"].shift(1)

    tr1 = x["high"] - x["low"]
    tr2 = (x["high"] - prev_close).abs()
    tr3 = (x["low"] - prev_close).abs()

    x["true_range"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # Wilder-style rolling ATR
    x["atr14"] = x["true_range"].rolling(14, min_periods=14).mean()
    x["atr_pct"] = x["atr14"] / x["close"]

    # 4H BTC trend reference
    h4 = (
        x.set_index("ts")["close"]
         .resample("4h")
         .last()
         .dropna()
         .to_frame("btc_4h_close")
    )

    h4["btc_4h_ema50"] = h4["btc_4h_close"].ewm(
        span=EMA_SPAN,
        adjust=False,
        min_periods=EMA_SPAN
    ).mean()

    h4["btc_4h_regime"] = np.where(
        h4["btc_4h_close"] < h4["btc_4h_ema50"],
        "BEAR",
        "BULL"
    )

    x = x.set_index("ts")
    x = x.join(h4[["btc_4h_close", "btc_4h_ema50", "btc_4h_regime"]], how="left")
    x[["btc_4h_close", "btc_4h_ema50", "btc_4h_regime"]] = (
        x[["btc_4h_close", "btc_4h_ema50", "btc_4h_regime"]].ffill()
    )

    # bar-2 return = close versus close two hours earlier
    x["bar2_return"] = x["close"].pct_change(2)

    x["atr_regime"] = np.where(
        x["atr_pct"] < ATR_THRESHOLD,
        "LOW",
        "HIGH"
    )

    x["bar2_regime"] = np.where(
        x["bar2_return"] >= 0,
        "POSITIVE",
        "NEGATIVE"
    )

    x["candidate"] = (
        (x["btc_4h_regime"] == "BEAR")
        & (x["atr_regime"] == "LOW")
        & (x["bar2_regime"] == "POSITIVE")
    )

    return x.reset_index()


def classify_evidence(n):
    if n < 10:
        return "INSUFFICIENT_EVIDENCE"
    if n < 20:
        return "PRELIMINARY_INCONCLUSIVE"
    return "FORMAL_HOLDOUT_EVALUATION"


def bootstrap_ci(values, seed=42, iterations=10000):
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]

    if len(values) < 2:
        return np.nan, np.nan

    rng = np.random.default_rng(seed)

    samples = rng.choice(
        values,
        size=(iterations, len(values)),
        replace=True
    )

    means = samples.mean(axis=1)

    return (
        float(np.percentile(means, 2.5)),
        float(np.percentile(means, 97.5)),
    )


def evaluate_symbol(df, symbol, holdout_start):
    holdout_start = pd.Timestamp(holdout_start, tz="UTC")

    warmup = df[df["ts"] < holdout_start].copy()
    holdout = df[df["ts"] >= holdout_start].copy()

    # Candidate observations are ONLY counted after the holdout boundary.
    events = holdout[holdout["candidate"]].copy()

    events["symbol"] = symbol
    events["holdout"] = True
    events["candidate"] = CANDIDATE

    # Prospective observation return:
    # next completed 1H close versus signal-bar close.
    #
    # This is intentionally an observation metric, not an execution model.
    full = df.copy()
    full["next_close"] = full["close"].shift(-1)
    full["observation_return"] = full["next_close"] / full["close"] - 1.0

    returns = full.loc[
        full["ts"].isin(events["ts"]),
        ["ts", "observation_return"]
    ]

    events = events.merge(returns, on="ts", how="left")

    return warmup, holdout, events


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--btc",
        default=r".\data\raw\BTCUSDT_1h_raw.csv"
    )

    parser.add_argument(
        "--eth",
        default=r".\data\raw\ETHUSDT_1h_raw.csv"
    )

    parser.add_argument(
        "--holdout-start",
        required=True,
        help="UTC timestamp, e.g. 2026-08-26T17:00:00Z"
    )

    parser.add_argument(
        "--output",
        default=r".\regime_output\prospective_holdout"
    )

    args = parser.parse_args()

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    print("=" * 78)
    print(f"{VERSION} — PROSPECTIVE HOLDOUT ENGINE")
    print("=" * 78)
    print(f"MODE                 : RESEARCH ONLY — NO ORDERS")
    print(f"CONTROL              : C0 FROZEN")
    print(f"CANDIDATE            : {CANDIDATE}")
    print(f"ATR THRESHOLD        : {ATR_THRESHOLD:.4%}")
    print(f"4H EMA               : EMA{EMA_SPAN}")
    print(f"HOLDOUT START        : {args.holdout_start}")
    print(f"ORDERS ALLOWED       : {ORDERS_ALLOWED}")
    print(f"PAPER EXECUTION      : {PAPER_EXECUTION}")
    print(f"LIVE EXECUTION       : {LIVE_EXECUTION}")
    print()

    btc = prepare_bars(load_bars(args.btc, "BTC"))
    eth = prepare_bars(load_bars(args.eth, "ETH"))

    btc_warmup, btc_holdout, btc_events = evaluate_symbol(
        btc, "BTC", args.holdout_start
    )

    eth_warmup, eth_holdout, eth_events = evaluate_symbol(
        eth, "ETH", args.holdout_start
    )

    events = pd.concat(
        [btc_events, eth_events],
        ignore_index=True
    )

    events = events.sort_values("ts").reset_index(drop=True)

    valid = events["observation_return"].notna()
    events = events[valid].copy()

    n = len(events)

    print("DATA")
    print("-" * 78)
    print(f"BTC bars total        : {len(btc):,}")
    print(f"ETH bars total        : {len(eth):,}")
    print(f"BTC holdout bars      : {len(btc_holdout):,}")
    print(f"ETH holdout bars      : {len(eth_holdout):,}")
    print()

    print("PROSPECTIVE EVENTS")
    print("-" * 78)
    print(f"BTC candidate events  : {(btc_events.shape[0]):,}")
    print(f"ETH candidate events  : {(eth_events.shape[0]):,}")
    print(f"VALID HOLDOUT N       : {n:,}")

    if n:
        values = events["observation_return"].astype(float).to_numpy()

        mean_return = float(np.mean(values))
        median_return = float(np.median(values))
        hit_rate = float(np.mean(values > 0))

        ci_low, ci_high = bootstrap_ci(values)

        print(f"Mean observation ret. : {mean_return:+.3%}")
        print(f"Median observation    : {median_return:+.3%}")
        print(f"Hit rate              : {hit_rate:.1%}")
        print(f"Bootstrap 95% CI      : {ci_low:+.3%} to {ci_high:+.3%}")
    else:
        mean_return = np.nan
        median_return = np.nan
        hit_rate = np.nan
        ci_low = np.nan
        ci_high = np.nan

        print("Mean observation ret. : NA")
        print("Median observation    : NA")
        print("Hit rate              : NA")
        print("Bootstrap 95% CI      : NA")

    evidence = classify_evidence(n)

    print()
    print("EVIDENCE GATE")
    print("-" * 78)
    print(f"Evidence status       : {evidence}")

    if n < 10:
        interpretation = (
            "PIPELINE WORKS — TOO LITTLE PROSPECTIVE EVIDENCE "
            "FOR A STATISTICAL CONCLUSION"
        )
    elif n < 20:
        interpretation = (
            "PRELIMINARY HOLDOUT — OBSERVE, DO NOT CONCLUDE"
        )
    else:
        interpretation = (
            "FORMAL HOLDOUT SAMPLE SIZE REACHED — "
            "APPLY PRE-REGISTERED CRITERIA"
        )

    print(f"Interpretation        : {interpretation}")

    print()
    print("GUARDRAILS")
    print("-" * 78)
    print("C0 frozen             : YES")
    print("Parameter optimization: NO")
    print("Orders allowed        : NO")
    print("Paper execution       : OFF")
    print("Live execution        : OFF")

    # Save event ledger
    ledger_path = out / "prospective_decision_ledger.csv"

    ledger_cols = [
        "ts",
        "symbol",
        "candidate",
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

    events[ledger_cols].to_csv(
        ledger_path,
        index=False
    )

    # Asset summary
    asset_rows = []

    for symbol, group in events.groupby("symbol"):
        vals = group["observation_return"].astype(float).to_numpy()

        lo, hi = bootstrap_ci(vals)

        asset_rows.append({
            "symbol": symbol,
            "N": len(vals),
            "mean_return": float(np.mean(vals)),
            "median_return": float(np.median(vals)),
            "hit_rate": float(np.mean(vals > 0)),
            "ci_low": lo,
            "ci_high": hi,
        })

    asset_path = out / "prospective_by_asset.csv"
    pd.DataFrame(asset_rows).to_csv(asset_path, index=False)

    summary = {
        "version": VERSION,
        "candidate": CANDIDATE,
        "mode": "RESEARCH_ONLY",
        "c0_frozen": True,
        "orders_allowed": False,
        "paper_execution": False,
        "live_execution": False,
        "holdout_start": str(
            pd.Timestamp(args.holdout_start, tz="UTC")
        ),
        "btc_total_bars": int(len(btc)),
        "eth_total_bars": int(len(eth)),
        "btc_holdout_bars": int(len(btc_holdout)),
        "eth_holdout_bars": int(len(eth_holdout)),
        "holdout_n": int(n),
        "mean_return": mean_return,
        "median_return": median_return,
        "hit_rate": hit_rate,
        "bootstrap_ci_low": ci_low,
        "bootstrap_ci_high": ci_high,
        "evidence_status": evidence,
        "interpretation": interpretation,
        "candidate_parameters": {
            "atr_threshold": ATR_THRESHOLD,
            "ema_span": EMA_SPAN,
            "bar2_rule": ">= 0",
        },
    }

    json_path = out / "prospective_holdout_summary.json"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str)

    print()
    print("FILES")
    print("-" * 78)
    print(f"Decision ledger       : {ledger_path}")
    print(f"Asset summary         : {asset_path}")
    print(f"JSON summary          : {json_path}")

    print()
    print("=" * 78)
    print("FINAL PROSPECTIVE HOLDOUT STATUS")
    print("=" * 78)
    print(f"{evidence}")
    print("RESEARCH ONLY — NO ORDERS")
    print("=" * 78)


if __name__ == "__main__":
    main()