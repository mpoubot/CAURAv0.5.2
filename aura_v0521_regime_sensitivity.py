#!/usr/bin/env python3
"""
AURA v0.5.2.1 â€” Regime Stability & Sensitivity Engine

RESEARCH ONLY.
C0 remains frozen. No strategy changes. No orders.

Purpose:
  Stress-test the v0.5.2 regime definition against:
    1) ATR threshold shifts
    2) BTC 4H EMA shifts
    3) bar-2 return threshold shifts
    4) BTC vs ETH consistency
    5) regime persistence after signal
    6) alternative time splits

The engine reruns the full supplied trade ledger for each perturbation.
It does NOT optimize parameters or select a trading strategy.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from datetime import timedelta

import numpy as np
import pandas as pd


VERSION = "AURA v0.5.2.1 â€” REGIME STABILITY & SENSITIVITY ENGINE"
CANDIDATE = ("BEAR", "LOW", "POSITIVE")
MIN_SAMPLE = 10
BOOTSTRAP_N = 5000
RNG_SEED = 52021


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------

def die(msg: str):
    raise RuntimeError(msg)


def first_existing(df, names, required=True):
    for n in names:
        if n in df.columns:
            return n
    if required:
        die(f"Missing required column. Tried: {names}")
    return None


def pct(x):
    if pd.isna(x):
        return "NA"
    return f"{x * 100:+.3f}%"


def numeric(series):
    return pd.to_numeric(series, errors="coerce")


def normalize_timestamp(s):
    return pd.to_datetime(s, utc=True, errors="coerce")


def bootstrap_ci(values, n=BOOTSTRAP_N, seed=RNG_SEED):
    x = np.asarray(values, dtype=float)
    x = x[np.isfinite(x)]
    if len(x) == 0:
        return np.nan, np.nan
    if len(x) == 1:
        return x[0], x[0]
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(x), size=(n, len(x)))
    means = x[idx].mean(axis=1)
    return float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))


def stats(values):
    x = np.asarray(values, dtype=float)
    x = x[np.isfinite(x)]
    if len(x) == 0:
        return dict(n=0, mean=np.nan, median=np.nan, hit_rate=np.nan,
                    ci_low=np.nan, ci_high=np.nan, status="NO_DATA")
    mean = float(np.mean(x))
    median = float(np.median(x))
    hit = float(np.mean(x > 0))
    lo, hi = bootstrap_ci(x)
    if len(x) < MIN_SAMPLE:
        status = "SMALL_SAMPLE"
    elif lo > 0:
        status = "POSITIVE_CI"
    elif hi < 0:
        status = "NEGATIVE_CI"
    else:
        status = "UNCERTAIN"
    return dict(n=len(x), mean=mean, median=median, hit_rate=hit,
                ci_low=lo, ci_high=hi, status=status)


def classify_stability(base, variants):
    """
    This is deliberately descriptive rather than an optimizer.

    ROBUST:
      majority of valid perturbations retain positive mean and no more than
      25% of valid variants become negative.

    FRAGILE:
      >=50% valid variants become negative OR the base effect changes sign
      under a small perturbation.

    INCONCLUSIVE:
      too little data / too many unavailable variants.
    """
    valid = [v for v in variants if v.get("n", 0) > 0]
    if not valid or base.get("n", 0) == 0:
        return "INCONCLUSIVE"

    neg = sum(v["mean"] < 0 for v in valid)
    pos = sum(v["mean"] > 0 for v in valid)

    if neg >= math.ceil(len(valid) * 0.50):
        return "FRAGILE"
    if pos >= math.ceil(len(valid) * 0.60) and neg <= max(1, len(valid) // 4):
        return "ROBUST"
    return "INCONCLUSIVE"


# ---------------------------------------------------------------------------
# Ledger loading / schema awareness
# ---------------------------------------------------------------------------

def load_ledger(path: Path) -> pd.DataFrame:
    if not path.exists():
        die(f"Ledger not found: {path}")

    df = pd.read_csv(path)

    required = ["trade_id", "symbol", "signal_timestamp",
                "bar_2_close_return_before_costs", "net_return",
                "period", "assignment_status", "regime_cell"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        die(f"Ledger schema mismatch. Missing: {', '.join(missing)}")

    df["signal_timestamp"] = normalize_timestamp(df["signal_timestamp"])
    df["net_return"] = numeric(df["net_return"])
    df["bar_2_close_return_before_costs"] = numeric(
        df["bar_2_close_return_before_costs"]
    )

    # Prefer explicit asset ATR percentage if already present.
    atr_col = first_existing(
        df,
        ["asset_atr_pct", "atr_pct", "atr_percent", "atr_ratio"],
        required=False,
    )
    if atr_col:
        df["_atr_pct"] = numeric(df[atr_col])
    else:
        df["_atr_pct"] = np.nan

    df["_symbol"] = df["symbol"].astype(str).str.upper()
    df["_bar2"] = df["bar_2_close_return_before_costs"]

    # Normalize known regime strings.
    df["_btc_regime"] = df["btc_4h_regime"].astype(str).str.upper() \
        if "btc_4h_regime" in df.columns else "UNKNOWN"
    df["_bar2_regime"] = df["bar2_regime"].astype(str).str.upper() \
        if "bar2_regime" in df.columns else np.where(df["_bar2"] >= 0, "POSITIVE", "NEGATIVE")

    # Keep only actual successful assignments for sensitivity analysis.
    df["_eligible"] = (
        df["assignment_status"].astype(str).str.upper().str.strip().isin(["PASS","ASSIGNED"])
        & df["signal_timestamp"].notna()
        & df["net_return"].notna()
    )

    return df


# ---------------------------------------------------------------------------
# Candidate-cell calculations
# ---------------------------------------------------------------------------

def candidate_mask(df, atr_threshold=None, ema_regime=None,
                   bar2_threshold=0.0, btc_regime_series=None):
    out = df["_eligible"].copy()

    btc = btc_regime_series if btc_regime_series is not None else df["_btc_regime"]
    out &= btc.astype(str).str.upper().eq("BEAR")

    if atr_threshold is not None and df["_atr_pct"].notna().any():
        out &= df["_atr_pct"] < float(atr_threshold)

    if bar2_threshold >= 0:
        out &= df["_bar2"] >= float(bar2_threshold)
    else:
        # For negative thresholds, POSITIVE still means above the boundary.
        out &= df["_bar2"] >= float(bar2_threshold)

    return out


def candidate_stats(df, mask):
    return stats(df.loc[mask, "net_return"].to_numpy())


def asset_split(df, mask):
    rows = []
    for asset in ["BTC_USDT", "ETH_USDT"]:
        m = mask & df["_symbol"].eq(asset)
        s = candidate_stats(df, m)
        s["asset"] = asset
        rows.append(s)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# ATR sensitivity
# ---------------------------------------------------------------------------

def run_atr_sensitivity(df, base_threshold):
    thresholds = [0.0050, 0.0055, 0.00575, base_threshold,
                  0.00625, 0.0065, 0.0070]
    rows = []
    for t in thresholds:
        m = candidate_mask(df, atr_threshold=t, bar2_threshold=0.0)
        s = candidate_stats(df, m)
        s.update(
            parameter="ATR_THRESHOLD",
            value=t,
            value_label=f"{t*100:.3f}%",
            is_base=abs(t - base_threshold) < 1e-12,
        )
        rows.append(s)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# EMA sensitivity
# ---------------------------------------------------------------------------

def load_1h_bars(path: Path) -> pd.DataFrame:
    if not path.exists():
        die(f"Market-data file not found: {path}")

    df = pd.read_csv(path)

    # BTC and ETH files have slightly different schemas in v0.5.2.
    ts_col = first_existing(
        df, ["open_time", "timestamp", "datetime", "time"], required=False
    )

    if ts_col:
        # open_time in BTC is ISO text; ETH timestamp is ISO text.
        ts = pd.to_datetime(df[ts_col], utc=True, errors="coerce")
    elif "open_time_ms" in df.columns:
        ts = pd.to_datetime(numeric(df["open_time_ms"]), unit="ms",
                            utc=True, errors="coerce")
    else:
        die(f"Cannot find timestamp column in {path}")

    df = df.copy()
    df["_ts"] = ts
    df["_close"] = numeric(df[first_existing(df, ["close"])])
    df["_high"] = numeric(df[first_existing(df, ["high"])])
    df["_low"] = numeric(df[first_existing(df, ["low"])])

    df = df.dropna(subset=["_ts", "_close", "_high", "_low"]).sort_values("_ts")
    return df.reset_index(drop=True)


def build_btc_4h_regime_map(btc_1h, ema_span):
    """
    Build completed 4H bars from 1H BTC data and assign:
      BULL if close >= EMA
      BEAR if close < EMA

    Only completed 4H bars are eligible. Resampling is anchored to UTC.
    """
    x = btc_1h.set_index("_ts")[["_close"]].resample("4h").agg(
        open=(" _close".strip(), "first"),
        high=(" _close".strip(), "max"),
        low=(" _close".strip(), "min"),
        close=(" _close".strip(), "last"),
    ).dropna()

    x["ema"] = x["close"].ewm(span=ema_span, adjust=False).mean()
    x["regime"] = np.where(x["close"] >= x["ema"], "BULL", "BEAR")

    # Map each signal to the latest completed 4H bar at/before signal time.
    return x


def map_btc_regime(df, btc_1h, ema_span):
    bars4 = build_btc_4h_regime_map(btc_1h, ema_span)

    # merge_asof requires sorted timestamps.
    left = df[["signal_timestamp"]].copy().sort_values("signal_timestamp")
    right = bars4.reset_index().rename(columns={"_ts": "bar_time"})
    right = right.rename(columns={right.columns[0]: "bar_time"})
    right = right[["bar_time", "regime", "ema", "close"]].sort_values("bar_time")

    merged = pd.merge_asof(
        left,
        right,
        left_on="signal_timestamp",
        right_on="bar_time",
        direction="backward",
    )

    result = pd.Series(merged["regime"].to_numpy(), index=left.index)
    return result.reindex(df.index)


def run_ema_sensitivity(df, btc_1h, base_ema=50):
    rows = []
    for ema in [40, 50, 60]:
        btc_regime = map_btc_regime(df, btc_1h, ema)
        m = candidate_mask(
            df,
            atr_threshold=None,
            bar2_threshold=0.0,
            btc_regime_series=btc_regime,
        )
        # If ATR data is available, preserve the base LOW ATR condition.
        if df["_atr_pct"].notna().any():
            # Base threshold inferred from ledger metadata if available.
            base_atr = float(df["_atr_pct"].median())
            # v0.5.2 used 0.596%; median is a fallback only.
            base_atr = 0.00596 if 0.004 <= base_atr <= 0.008 else base_atr
            m &= df["_atr_pct"] < base_atr

        s = candidate_stats(df, m)
        s.update(parameter="BTC_4H_EMA", value=ema,
                 value_label=f"EMA{ema}", is_base=ema == base_ema)
        rows.append(s)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Bar-2 sensitivity
# ---------------------------------------------------------------------------

def run_bar2_sensitivity(df):
    thresholds = [-0.0010, -0.0005, 0.0, 0.0005, 0.0010]
    rows = []
    base_atr = 0.00596

    for t in thresholds:
        m = candidate_mask(df, atr_threshold=base_atr,
                           bar2_threshold=t)
        s = candidate_stats(df, m)
        s.update(parameter="BAR2_THRESHOLD", value=t,
                 value_label=f"{t*100:+.2f}%",
                 is_base=abs(t) < 1e-12)
        rows.append(s)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# BTC / ETH consistency
# ---------------------------------------------------------------------------

def run_asset_consistency(df):
    m = candidate_mask(df, atr_threshold=0.00596, bar2_threshold=0.0)
    all_s = candidate_stats(df, m)
    all_s["asset"] = "ALL"

    out = [all_s]
    for asset in ["BTC_USDT", "ETH_USDT"]:
        s = candidate_stats(df, m & df["_symbol"].eq(asset))
        s["asset"] = asset
        out.append(s)

    return pd.DataFrame(out)


# ---------------------------------------------------------------------------
# Regime persistence
# ---------------------------------------------------------------------------

def atr_pct_at_time(asset_bars, t, window=14):
    prior = asset_bars.loc[asset_bars["_ts"] <= t].tail(window + 1)
    if len(prior) < window + 1:
        return np.nan
    prev_close = prior["_close"].shift(1)
    tr = pd.concat(
        [
            prior["_high"] - prior["_low"],
            (prior["_high"] - prev_close).abs(),
            (prior["_low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(window).mean().iloc[-1]
    close = prior["_close"].iloc[-1]
    if not np.isfinite(atr) or close == 0:
        return np.nan
    return float(atr / close)


def btc_regime_at_time(btc_1h, t, ema_span=50):
    x = (
        btc_1h.loc[btc_1h["_ts"] <= t]
        .set_index("_ts")["_close"]
        .resample("4h")
        .last()
        .dropna()
    )

    if len(x) < ema_span:
        return "UNAVAILABLE"

    ema = x.ewm(span=ema_span, adjust=False).mean().iloc[-1]

    return "BULL" if x.iloc[-1] >= ema else "BEAR"


def run_persistence(df, btc_1h, eth_1h, horizons=(1, 2, 3, 4, 6, 8)):
    # Only base candidate assignments are followed.
    m = candidate_mask(df, atr_threshold=0.00596, bar2_threshold=0.0)
    base = df.loc[m].copy()

    rows = []
    for h in horizons:
        same = 0
        observed = 0
        btc_same = 0
        atr_same = 0

        for _, r in base.iterrows():
            t = r["signal_timestamp"] + pd.Timedelta(hours=h)
            asset_bars = btc_1h if "BTC" in r["_symbol"] else eth_1h

            br = btc_regime_at_time(btc_1h, t, 50)
            ar = atr_pct_at_time(asset_bars, t, 14)

            if br != "UNAVAILABLE" and np.isfinite(ar):
                observed += 1
                if br == "BEAR":
                    btc_same += 1
                if ar < 0.00596:
                    atr_same += 1
                if br == "BEAR" and ar < 0.00596:
                    same += 1

        rows.append({
            "horizon_hours": h,
            "base_candidate_n": len(base),
            "observed_n": observed,
            "btc_bear_pct": btc_same / observed if observed else np.nan,
            "low_atr_pct": atr_same / observed if observed else np.nan,
            "full_regime_persistence_pct": same / observed if observed else np.nan,
        })

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Alternative time splits
# ---------------------------------------------------------------------------

def infer_split_boundaries(df):
    """
    Infer EARLY / MIDDLE / LATE boundaries directly from
    chronological eligible assignment timestamps.

    The ledger's period column may be blank/NaN because the
    regime builder does not persist period labels. Therefore
    period classification must be derived from signal_timestamp.

    This function deliberately uses the eligible assignments only.
    It does not use net returns or regime outcomes to determine
    the boundaries.
    """

    if "signal_timestamp" not in df.columns:
        die("Cannot infer time splits: signal_timestamp column not present.")

    work = df.copy()

    work["_split_ts"] = pd.to_datetime(
        work["signal_timestamp"],
        errors="coerce",
        utc=True
    )

    work = work.loc[
        work["_eligible"] & work["_split_ts"].notna()
    ].copy()

    if work.empty:
        die("Cannot infer time splits: no eligible assignments with valid timestamps.")

    work = work.sort_values("_split_ts").reset_index(drop=True)

    n = len(work)

    if n < 3:
        die(f"Cannot infer time splits: only {n} eligible assignments.")

    # Preserve chronological ordering while creating three periods.
    # Use approximately equal observation counts.
    q1 = n // 3
    q2 = (2 * n) // 3

    if q1 <= 0 or q2 <= q1 or q2 >= n:
        die(f"Cannot infer time splits: insufficient observations ({n}).")

    early_end = work.loc[q1 - 1, "_split_ts"]
    middle_end = work.loc[q2 - 1, "_split_ts"]

    b1 = early_end
    b2 = middle_end

    return b1, b2


def assign_shifted_periods(df, delta_days, b1, b2):
    x = df["signal_timestamp"]
    b1s = b1 + pd.Timedelta(days=delta_days)
    b2s = b2 + pd.Timedelta(days=delta_days)

    return np.select(
        [x < b1s, x < b2s],
        ["EARLY", "MIDDLE"],
        default="LATE",
    )


def run_time_split_sensitivity(df):
    b1, b2 = infer_split_boundaries(df)
    rows = []

    # Base plus symmetric shifts.
    for d in [-14, -7, 0, 7, 14]:
        p = assign_shifted_periods(df, d, b1, b2)
        temp = df.copy()
        temp["_split"] = p

        # Train = EARLY+MIDDLE, OOS = LATE.
        train = temp["_eligible"] & temp["_split"].isin(["EARLY", "MIDDLE"])
        oos = temp["_eligible"] & temp["_split"].eq("LATE")

        # Candidate remains exactly BEAR/LOW/POSITIVE.
        cand = candidate_mask(temp, atr_threshold=0.00596, bar2_threshold=0.0)

        train_s = candidate_stats(temp, cand & train)
        oos_s = candidate_stats(temp, cand & oos)

        rows.append({
            "shift_days": d,
            "boundary_1": str(b1 + pd.Timedelta(days=d)),
            "boundary_2": str(b2 + pd.Timedelta(days=d)),
            "train_n": train_s["n"],
            "train_mean": train_s["mean"],
            "train_ci_low": train_s["ci_low"],
            "train_ci_high": train_s["ci_high"],
            "oos_n": oos_s["n"],
            "oos_mean": oos_s["mean"],
            "oos_ci_low": oos_s["ci_low"],
            "oos_ci_high": oos_s["ci_high"],
            "oos_status": oos_s["status"],
        })

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def make_html(outdir, summary, atr, ema, bar2, asset, persistence, splits):
    def table(df):
        return df.to_html(index=False, float_format=lambda x: f"{x:.6f}")

    html = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{VERSION}</title>
<style>
body {{ font-family: Arial, sans-serif; margin: 32px; }}
table {{ border-collapse: collapse; margin-bottom: 28px; }}
th, td {{ border: 1px solid #aaa; padding: 5px 8px; }}
h1, h2 {{ margin-top: 28px; }}
</style>
</head>
<body>
<h1>{VERSION}</h1>
<p><b>RESEARCH ONLY â€” NO ORDERS â€” C0 FROZEN</b></p>
<h2>Overall summary</h2>
{table(summary)}
<h2>ATR sensitivity</h2>
{table(atr)}
<h2>EMA sensitivity</h2>
{table(ema)}
<h2>Bar-2 sensitivity</h2>
{table(bar2)}
<h2>Asset consistency</h2>
{table(asset)}
<h2>Regime persistence</h2>
{table(persistence)}
<h2>Alternative time splits</h2>
{table(splits)}
</body>
</html>"""
    (outdir / "regime_sensitivity_report.html").write_text(html, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=VERSION)
    ap.add_argument("--ledger", required=True)
    ap.add_argument("--bars-dir", required=True)
    ap.add_argument("--output", required=True)
    args = ap.parse_args()

    ledger_path = Path(args.ledger)
    bars_dir = Path(args.bars_dir)
    outdir = Path(args.output)
    outdir.mkdir(parents=True, exist_ok=True)

    print("=" * 78)
    print(VERSION)
    print("=" * 78)
    print("MODE                  : RESEARCH ONLY â€” NO ORDERS")
    print("CONTROL               : C0 FROZEN")
    print("BASE CANDIDATE        : BEAR Ã— LOW ATR Ã— POSITIVE bar-2")
    print("PERTURBATIONS         : ATR / EMA / BAR-2 / ASSET / PERSISTENCE / TIME")
    print(f"LEDGER                : {ledger_path}")
    print(f"BARS                  : {bars_dir}")
    print(f"OUTPUT                : {outdir}")
    print()

    df = load_ledger(ledger_path)

    btc_path = bars_dir / "BTCUSDT_1h_raw.csv"
    eth_path = bars_dir / "ETHUSDT_1h_raw.csv"

    # Also accept Excel-extension files only if they are genuinely CSV text.
    if not btc_path.exists():
        die(f"BTC file not found: {btc_path}")
    if not eth_path.exists():
        die(f"ETH file not found: {eth_path}")

    btc = load_1h_bars(btc_path)
    eth = load_1h_bars(eth_path)

    base_mask = candidate_mask(df, atr_threshold=0.00596, bar2_threshold=0.0)
    base = candidate_stats(df, base_mask)

    print(f"Eligible assignments   : {int(df['_eligible'].sum())}")
    print(f"Base candidate N        : {base['n']}")
    print(f"Base candidate mean     : {pct(base['mean'])}")
    print(f"Base candidate CI       : {pct(base['ci_low'])} to {pct(base['ci_high'])}")
    print()

    atr = run_atr_sensitivity(df, 0.00596)
    ema = run_ema_sensitivity(df, btc, 50)
    bar2 = run_bar2_sensitivity(df)
    asset = run_asset_consistency(df)
    persistence = run_persistence(df, btc, eth)
    splits = run_time_split_sensitivity(df)

    atr_stability = classify_stability(
        atr.loc[atr["is_base"]].iloc[0].to_dict(),
        [r for _, r in atr.iterrows() if not r["is_base"]],
    )
    ema_stability = classify_stability(
        ema.loc[ema["is_base"]].iloc[0].to_dict(),
        [r for _, r in ema.iterrows() if not r["is_base"]],
    )
    bar2_stability = classify_stability(
        bar2.loc[bar2["is_base"]].iloc[0].to_dict(),
        [r for _, r in bar2.iterrows() if not r["is_base"]],
    )

    summary = pd.DataFrame([{
        "version": VERSION,
        "candidate": "BEAR Ã— LOW Ã— POSITIVE",
        "base_n": base["n"],
        "base_mean": base["mean"],
        "base_median": base["median"],
        "base_hit_rate": base["hit_rate"],
        "base_ci_low": base["ci_low"],
        "base_ci_high": base["ci_high"],
        "atr_stability": atr_stability,
        "ema_stability": ema_stability,
        "bar2_stability": bar2_stability,
        "orders_allowed": False,
        "strategy_filter_selected": False,
    }])

    # Save all artifacts.
    summary.to_csv(outdir / "regime_sensitivity_summary.csv", index=False)
    atr.to_csv(outdir / "atr_sensitivity.csv", index=False)
    ema.to_csv(outdir / "ema_sensitivity.csv", index=False)
    bar2.to_csv(outdir / "bar2_sensitivity.csv", index=False)
    asset.to_csv(outdir / "asset_consistency.csv", index=False)
    persistence.to_csv(outdir / "regime_persistence.csv", index=False)
    splits.to_csv(outdir / "alternative_time_splits.csv", index=False)

    manifest = {
        "version": VERSION,
        "mode": "RESEARCH ONLY â€” NO ORDERS",
        "control": "C0 FROZEN",
        "candidate": "BEAR Ã— LOW Ã— POSITIVE",
        "base_atr_threshold": 0.00596,
        "base_ema": 50,
        "base_bar2_threshold": 0.0,
        "min_sample": MIN_SAMPLE,
        "bootstrap_n": BOOTSTRAP_N,
        "rng_seed": RNG_SEED,
        "inputs": {
            "ledger": str(ledger_path),
            "btc_bars": str(btc_path),
            "eth_bars": str(eth_path),
        },
        "stability": {
            "atr": atr_stability,
            "ema": ema_stability,
            "bar2": bar2_stability,
        },
    }

    (outdir / "regime_sensitivity_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    make_html(outdir, summary, atr, ema, bar2, asset, persistence, splits)

    print("=" * 78)
    print("SENSITIVITY RESULTS")
    print("=" * 78)
    print(f"ATR stability           : {atr_stability}")
    print(f"EMA stability           : {ema_stability}")
    print(f"Bar-2 stability         : {bar2_stability}")
    print()
    print("ASSET CONSISTENCY")
    print(asset[["asset", "n", "mean", "hit_rate", "ci_low", "ci_high", "status"]]
          .to_string(index=False))
    print()
    print("REGIME PERSISTENCE")
    print(persistence.to_string(index=False))
    print()
    print("ALTERNATIVE TIME SPLITS")
    print(splits.to_string(index=False))
    print()
    print("=" * 78)
    print("FINAL v0.5.2.1 GATE")
    print("=" * 78)
    print("Strategy filter selected : NO")
    print("Orders allowed           : NO")
    print("Interpretation           : RESEARCH DIAGNOSTIC ONLY")
    print()
    print("FILES")
    for p in sorted(outdir.iterdir()):
        print(f"{p.name:32s}: {p}")
    print()
    print("AURA v0.5.2.1 completed.")


if __name__ == "__main__":
    main()

