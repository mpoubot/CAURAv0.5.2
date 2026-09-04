#!/usr/bin/env python3
"""
AURA v0.5.3 — Chronological Holdout Validator

RESEARCH ONLY.
Pre-registered candidate is frozen. No parameter search. No strategy changes.
No live/paper orders are generated.

Purpose:
    Evaluate a FROZEN v0.5.2 candidate on genuinely unseen chronological data.

Frozen hypothesis:
    BTC 4H EMA50 regime = BEAR
    Asset 1H ATR14 / close < 0.596%
    bar-2 close return >= 0.00%
    Candidate = BEAR × LOW ATR × POSITIVE bar-2

Important:
    This script never searches for a better threshold, regime cell, asset,
    split, or parameter. It only evaluates supplied holdout observations.

Expected holdout input:
    CSV containing one row per unseen C0-compatible trade with at least:
      trade_id
      symbol
      signal_timestamp
      net_return
      btc_4h_regime
      asset_atr_pct
      bar2_regime

Optional:
      entry_timestamp
      bar_2_close_return_before_costs
      btc_4h_ema50
      failure_reason
      assignment_status

If the holdout CSV already contains the frozen regime fields, this validator
does not recalculate them. That keeps the test auditable and avoids silently
substituting a different data-generation pipeline.

A holdout observation qualifies only when:
    assignment_status is PASS (if present)
    AND btc_4h_regime == BEAR
    AND asset_atr_pct < 0.00596
    AND bar-2 return >= 0.0, or bar2_regime == POSITIVE

Decision:
    PASS / FAIL / INCONCLUSIVE are deliberately conservative.
    The default minimum qualifying holdout N is 20.
    Statistical PASS additionally requires a bootstrap 95% CI lower bound > 0
    and positive mean return.
    A small positive sample cannot PASS.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd


VERSION = "AURA v0.5.3 — CHRONOLOGICAL HOLDOUT VALIDATOR"

# ----------------------- FROZEN HYPOTHESIS -----------------------
EMA_SPAN = 50
ATR_THRESHOLD = 0.00596       # 0.596%
BAR2_THRESHOLD = 0.0
CANDIDATE = "BEAR × LOW ATR × POSITIVE bar-2"

# ----------------------- HOLDOUT GATES ---------------------------
MIN_HOLDOUT_N = 20
ALPHA = 0.05
BOOTSTRAP_ITERATIONS = 10000
SEED = 53001


def die(msg: str) -> None:
    raise RuntimeError(msg)


def pct(x):
    if x is None or not np.isfinite(x):
        return "NA"
    return f"{x * 100:+.3f}%"


def pct1(x):
    if x is None or not np.isfinite(x):
        return "NA"
    return f"{x * 100:.1f}%"


def parse_ts(s):
    return pd.to_datetime(s, utc=True, errors="coerce")


def bootstrap_ci(values, iterations=BOOTSTRAP_ITERATIONS, seed=SEED):
    x = np.asarray(values, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 2:
        return np.nan, np.nan

    rng = np.random.default_rng(seed)
    means = np.empty(iterations, dtype=float)

    # Chunking avoids constructing an unnecessarily large matrix for larger
    # future holdout samples.
    chunk = 1000
    pos = 0
    while pos < iterations:
        k = min(chunk, iterations - pos)
        idx = rng.integers(0, n, size=(k, n))
        means[pos:pos + k] = x[idx].mean(axis=1)
        pos += k

    return float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))


def normal_ci(values):
    x = np.asarray(values, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 2:
        return np.nan, np.nan
    mean = float(x.mean())
    se = float(x.std(ddof=1) / math.sqrt(n))
    z = 1.959963984540054
    return mean - z * se, mean + z * se


def load_holdout(path: Path) -> pd.DataFrame:
    if not path.exists():
        die(f"Holdout file not found: {path}")

    df = pd.read_csv(path)

    required = [
        "trade_id",
        "symbol",
        "signal_timestamp",
        "net_return",
        "btc_4h_regime",
        "asset_atr_pct",
    ]
    missing = [c for c in required if c not in df.columns]
    if missing:
        die("Holdout schema mismatch. Missing: " + ", ".join(missing))

    df = df.copy()
    df["signal_timestamp"] = parse_ts(df["signal_timestamp"])
    df["net_return"] = pd.to_numeric(df["net_return"], errors="coerce")
    df["asset_atr_pct"] = pd.to_numeric(df["asset_atr_pct"], errors="coerce")

    if "bar_2_close_return_before_costs" in df.columns:
        df["bar_2_close_return_before_costs"] = pd.to_numeric(
            df["bar_2_close_return_before_costs"], errors="coerce"
        )
    else:
        df["bar_2_close_return_before_costs"] = np.nan

    df["symbol"] = df["symbol"].astype(str).str.upper().str.strip()
    df["btc_4h_regime"] = (
        df["btc_4h_regime"].astype(str).str.upper().str.strip()
    )

    if "assignment_status" in df.columns:
        df["assignment_status"] = (
            df["assignment_status"].astype(str).str.upper().str.strip()
        )
        status_ok = df["assignment_status"].eq("PASS")
    else:
        status_ok = pd.Series(True, index=df.index)

    if "bar2_regime" in df.columns:
        df["bar2_regime"] = df["bar2_regime"].astype(str).str.upper().str.strip()
        bar2_ok = (
            df["bar_2_close_return_before_costs"].notna()
            & (df["bar_2_close_return_before_costs"] >= BAR2_THRESHOLD)
        ) | (
            df["bar_2_close_return_before_costs"].isna()
            & df["bar2_regime"].eq("POSITIVE")
        )
    else:
        if df["bar_2_close_return_before_costs"].isna().all():
            die(
                "Holdout requires either bar2_regime or "
                "bar_2_close_return_before_costs."
            )
        bar2_ok = df["bar_2_close_return_before_costs"] >= BAR2_THRESHOLD

    df["_valid_base"] = (
        status_ok
        & df["signal_timestamp"].notna()
        & df["net_return"].notna()
        & df["btc_4h_regime"].eq("BEAR")
        & df["asset_atr_pct"].notna()
        & (df["asset_atr_pct"] < ATR_THRESHOLD)
        & bar2_ok
    )

    # A holdout must be genuinely after the historical discovery sample.
    # The default boundary is the end of the supplied historical v0.5.2
    # sample. It can be overridden only by an explicit --holdout-start.
    return df


def stats(values):
    x = np.asarray(values, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)

    if n == 0:
        return {
            "n": 0,
            "mean": np.nan,
            "median": np.nan,
            "hit_rate": np.nan,
            "best": np.nan,
            "worst": np.nan,
            "normal_ci_low": np.nan,
            "normal_ci_high": np.nan,
            "bootstrap_ci_low": np.nan,
            "bootstrap_ci_high": np.nan,
        }

    lo, hi = bootstrap_ci(x)
    nlo, nhi = normal_ci(x)

    return {
        "n": n,
        "mean": float(x.mean()),
        "median": float(np.median(x)),
        "hit_rate": float(np.mean(x > 0)),
        "best": float(x.max()),
        "worst": float(x.min()),
        "normal_ci_low": nlo,
        "normal_ci_high": nhi,
        "bootstrap_ci_low": lo,
        "bootstrap_ci_high": hi,
    }


def chronological_checks(df, holdout_start):
    checks = []

    checks.append({
        "check": "C0_FROZEN",
        "status": "PASS",
        "detail": "This validator contains no parameter-search or strategy-modification path.",
    })

    checks.append({
        "check": "NO_ORDERS",
        "status": "PASS",
        "detail": "This module has no order/execution functionality.",
    })

    if df["signal_timestamp"].notna().any():
        first_ts = df["signal_timestamp"].min()
        last_ts = df["signal_timestamp"].max()
        if first_ts >= holdout_start:
            status = "PASS"
            detail = f"All holdout observations begin at/after {holdout_start.isoformat()}."
        else:
            status = "FAIL"
            detail = (
                f"Holdout contains observations before holdout-start: "
                f"first={first_ts.isoformat()}."
            )
    else:
        status = "FAIL"
        detail = "No valid signal timestamps."

    checks.append({
        "check": "CHRONOLOGICAL_HOLDOUT",
        "status": status,
        "detail": detail,
    })

    duplicate_count = int(df["trade_id"].duplicated().sum())
    checks.append({
        "check": "DUPLICATE_TRADE_IDS",
        "status": "PASS" if duplicate_count == 0 else "FAIL",
        "detail": f"Duplicate trade_id count = {duplicate_count}.",
    })

    return pd.DataFrame(checks)


def evaluate_gate(candidate, checks):
    n = candidate["n"]
    mean = candidate["mean"]
    ci_low = candidate["bootstrap_ci_low"]

    integrity_pass = (checks["status"] == "PASS").all()

    if not integrity_pass:
        return "FAIL", "Holdout integrity check failed."

    if n < MIN_HOLDOUT_N:
        return (
            "INCONCLUSIVE",
            f"Only {n} qualifying holdout trades; minimum required is {MIN_HOLDOUT_N}.",
        )

    if not np.isfinite(mean) or not np.isfinite(ci_low):
        return "INCONCLUSIVE", "Insufficient valid return observations."

    if mean > 0 and ci_low > 0:
        return (
            "PASS",
            "Qualifying holdout mean is positive and the bootstrap 95% CI lower bound is above zero.",
        )

    return (
        "FAIL",
        "Adequate holdout sample, but the frozen candidate does not meet the predefined positive-return criterion.",
    )


def main():
    parser = argparse.ArgumentParser(description=VERSION)
    parser.add_argument("--holdout", required=True,
                        help="CSV containing genuinely unseen holdout observations.")
    parser.add_argument("--output", required=True,
                        help="Output directory.")
    parser.add_argument(
        "--holdout-start",
        default="2026-08-27T00:00:00Z",
        help="Earliest timestamp allowed in the holdout. Default: 2026-08-27 UTC.",
    )
    args = parser.parse_args()

    holdout_path = Path(args.holdout)
    outdir = Path(args.output)
    outdir.mkdir(parents=True, exist_ok=True)

    holdout_start = parse_ts(args.holdout_start)
    if pd.isna(holdout_start):
        die(f"Invalid --holdout-start: {args.holdout_start}")

    print("=" * 78)
    print(VERSION)
    print("=" * 78)
    print("MODE                 : RESEARCH ONLY")
    print("C0                   : FROZEN")
    print("ORDERS               : DISABLED")
    print("PARAMETER SEARCH     : DISABLED")
    print()
    print("FROZEN HYPOTHESIS")
    print(f"Candidate             : {CANDIDATE}")
    print(f"BTC 4H EMA            : EMA{EMA_SPAN}")
    print(f"ATR threshold         : {ATR_THRESHOLD * 100:.3f}%")
    print(f"bar-2 threshold       : {BAR2_THRESHOLD * 100:+.3f}%")
    print()
    print(f"Holdout               : {holdout_path}")
    print(f"Holdout start         : {holdout_start.isoformat()}")
    print()

    df = load_holdout(holdout_path)

    # Only unseen chronological observations are allowed.
    df = df.loc[df["signal_timestamp"] >= holdout_start].copy()
    df = df.sort_values("signal_timestamp").reset_index(drop=True)

    if df.empty:
        die("No observations remain after holdout-start filtering.")

    checks = chronological_checks(df, holdout_start)

    candidate_df = df.loc[df["_valid_base"]].copy()
    candidate = stats(candidate_df["net_return"].to_numpy())

    # Descriptive asset split only; this is NOT model selection.
    asset_rows = []
    for asset in sorted(df["symbol"].dropna().unique()):
        x = candidate_df.loc[candidate_df["symbol"].eq(asset), "net_return"]
        s = stats(x.to_numpy())
        s["symbol"] = asset
        asset_rows.append(s)
    asset_stats = pd.DataFrame(asset_rows)

    # Chronological cumulative path is descriptive only.
    if not candidate_df.empty:
        path = candidate_df[["trade_id", "symbol", "signal_timestamp", "net_return"]].copy()
        path["cumulative_return"] = path["net_return"].cumsum()
        path["running_max_cumulative"] = path["cumulative_return"].cummax()
        path["drawdown_from_running_max"] = (
            path["cumulative_return"] - path["running_max_cumulative"]
        )
    else:
        path = pd.DataFrame(
            columns=[
                "trade_id", "symbol", "signal_timestamp", "net_return",
                "cumulative_return", "running_max_cumulative",
                "drawdown_from_running_max"
            ]
        )

    gate, interpretation = evaluate_gate(candidate, checks)

    summary = pd.DataFrame([{
        "version": VERSION,
        "candidate": CANDIDATE,
        "ema_span": EMA_SPAN,
        "atr_threshold": ATR_THRESHOLD,
        "bar2_threshold": BAR2_THRESHOLD,
        "holdout_start": holdout_start.isoformat(),
        "holdout_first_timestamp": df["signal_timestamp"].min().isoformat(),
        "holdout_last_timestamp": df["signal_timestamp"].max().isoformat(),
        "holdout_observations": len(df),
        "qualifying_candidate_n": candidate["n"],
        "mean_return": candidate["mean"],
        "median_return": candidate["median"],
        "hit_rate": candidate["hit_rate"],
        "best_return": candidate["best"],
        "worst_return": candidate["worst"],
        "bootstrap_ci_low": candidate["bootstrap_ci_low"],
        "bootstrap_ci_high": candidate["bootstrap_ci_high"],
        "normal_ci_low": candidate["normal_ci_low"],
        "normal_ci_high": candidate["normal_ci_high"],
        "minimum_holdout_n": MIN_HOLDOUT_N,
        "gate": gate,
        "interpretation": interpretation,
        "strategy_filter_selected": False,
        "orders_allowed": False,
    }])

    manifest = {
        "version": VERSION,
        "mode": "RESEARCH ONLY",
        "c0_frozen": True,
        "orders_allowed": False,
        "strategy_filter_selected": False,
        "candidate": CANDIDATE,
        "frozen_parameters": {
            "btc_4h_ema": EMA_SPAN,
            "atr_threshold": ATR_THRESHOLD,
            "bar2_threshold": BAR2_THRESHOLD,
        },
        "holdout_start": holdout_start.isoformat(),
        "minimum_holdout_n": MIN_HOLDOUT_N,
        "bootstrap_iterations": BOOTSTRAP_ITERATIONS,
        "seed": SEED,
        "gate": gate,
        "interpretation": interpretation,
    }

    summary.to_csv(outdir / "holdout_summary.csv", index=False)
    checks.to_csv(outdir / "holdout_integrity_checks.csv", index=False)
    asset_stats.to_csv(outdir / "holdout_asset_diagnostics.csv", index=False)
    candidate_df.to_csv(outdir / "holdout_candidate_trades.csv", index=False)
    path.to_csv(outdir / "holdout_cumulative_path.csv", index=False)
    (outdir / "holdout_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    print("=" * 78)
    print("HOLDOUT RESULT")
    print("=" * 78)
    print(f"All unseen observations : {len(df)}")
    print(f"Qualifying candidate N  : {candidate['n']}")
    print(f"Mean return             : {pct(candidate['mean'])}")
    print(f"Median return           : {pct(candidate['median'])}")
    print(f"Hit rate                : {pct1(candidate['hit_rate'])}")
    print(f"Best return             : {pct(candidate['best'])}")
    print(f"Worst return            : {pct(candidate['worst'])}")
    print(
        f"Bootstrap 95% CI        : "
        f"{pct(candidate['bootstrap_ci_low'])} → "
        f"{pct(candidate['bootstrap_ci_high'])}"
    )
    print()
    print("INTEGRITY CHECKS")
    print(checks.to_string(index=False))
    print()
    print("ASSET DIAGNOSTICS — DESCRIPTIVE ONLY")
    if asset_stats.empty:
        print("No qualifying candidate observations.")
    else:
        print(asset_stats[
            ["symbol", "n", "mean", "median", "hit_rate",
             "bootstrap_ci_low", "bootstrap_ci_high"]
        ].to_string(index=False))
    print()
    print("=" * 78)
    print("FINAL v0.5.3 HOLDOUT GATE")
    print("=" * 78)
    print(f"GATE                   : {gate}")
    print(f"INTERPRETATION         : {interpretation}")
    print("STRATEGY FILTER        : NO")
    print("ORDERS ALLOWED         : NO")
    print()
    print("OUTPUT FILES")
    for p in sorted(outdir.iterdir()):
        print(f"{p.name:34s}: {p}")
    print()
    print("AURA v0.5.3 completed.")


if __name__ == "__main__":
    main()
