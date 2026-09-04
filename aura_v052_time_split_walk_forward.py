"""
AURA v0.5.2 — TIME-SPLIT / WALK-FORWARD ROBUSTNESS — 3D REGIME
Research only. No orders. No strategy mutation.

Purpose:
    Evaluate whether the frozen C0 regime relationships survive chronological,
    out-of-sample testing.

Canonical periods (derived from signal_timestamp):
    EARLY  = 2026-03-02 through 2026-04-30
    MIDDLE = 2026-05-01 through 2026-06-30
    LATE   = 2026-07-01 through 2026-08-26

Walk-forward folds:
    WF1: train EARLY -> test MIDDLE
    WF2: train EARLY+MIDDLE -> test LATE

The validator never uses test-period outcomes to choose a regime cell.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

VERSION = "AURA v0.5.2 — TIME-SPLIT / WALK-FORWARD ROBUSTNESS — 3D REGIME"
EXPECTED_TRADES = 55
MIN_TRAIN_N = 3
MIN_TEST_N = 2
BOOTSTRAP_N = 5000
SEED = 502

PERIODS = ["EARLY", "MIDDLE", "LATE"]

REQUIRED_COLUMNS = {
    "trade_id",
    "symbol",
    "signal_timestamp",
    "net_return",
    "btc_4h_regime",
    "bar2_regime",
    "assignment_status",
    "regime_cell",
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=VERSION)
    p.add_argument("--ledger", required=True, help="Path to regime_assignment_ledger.csv")
    p.add_argument("--output", required=True, help="Output directory")
    p.add_argument("--min-train-n", type=int, default=MIN_TRAIN_N)
    p.add_argument("--min-test-n", type=int, default=MIN_TEST_N)
    return p.parse_args()


def canonical_period(ts: pd.Timestamp) -> str:
    if pd.isna(ts):
        return "OUTSIDE"
    if pd.Timestamp("2026-03-02T00:00:00Z") <= ts <= pd.Timestamp("2026-04-30T23:59:59Z"):
        return "EARLY"
    if pd.Timestamp("2026-05-01T00:00:00Z") <= ts <= pd.Timestamp("2026-06-30T23:59:59Z"):
        return "MIDDLE"
    if pd.Timestamp("2026-07-01T00:00:00Z") <= ts <= pd.Timestamp("2026-08-26T23:59:59Z"):
        return "LATE"
    return "OUTSIDE"


def bootstrap_ci(values: np.ndarray, seed: int = SEED) -> Tuple[float, float]:
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if len(values) < 2:
        return (np.nan, np.nan)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(values), size=(BOOTSTRAP_N, len(values)))
    means = values[idx].mean(axis=1)
    return tuple(np.quantile(means, [0.025, 0.975]))


def stats(frame: pd.DataFrame, seed: int) -> Dict[str, object]:
    vals = pd.to_numeric(frame["net_return"], errors="coerce").dropna().to_numpy(dtype=float)
    n = len(vals)
    if n == 0:
        return {
            "n": 0, "mean_net_return": np.nan, "median_net_return": np.nan,
            "hit_rate": np.nan, "ci95_low": np.nan, "ci95_high": np.nan,
        }
    low, high = bootstrap_ci(vals, seed)
    return {
        "n": int(n),
        "mean_net_return": float(vals.mean()),
        "median_net_return": float(np.median(vals)),
        "hit_rate": float((vals > 0).mean()),
        "ci95_low": float(low) if np.isfinite(low) else np.nan,
        "ci95_high": float(high) if np.isfinite(high) else np.nan,
    }


def classify_oos(train: Dict[str, object], test: Dict[str, object], min_train_n: int, min_test_n: int) -> str:
    if train["n"] < min_train_n:
        return "INSUFFICIENT_TRAIN"
    if test["n"] < min_test_n:
        return "INSUFFICIENT_TEST"
    train_mean = train["mean_net_return"]
    test_mean = test["mean_net_return"]
    if not np.isfinite(train_mean) or not np.isfinite(test_mean):
        return "NO_DATA"
    if train_mean > 0 and test_mean > 0:
        if test["ci95_low"] > 0:
            return "ROBUST_POSITIVE_CI"
        return "POSITIVE_OOS"
    if train_mean < 0 and test_mean < 0:
        if test["ci95_high"] < 0:
            return "ROBUST_NEGATIVE_CI"
        return "NEGATIVE_OOS"
    return "SIGN_FLIP"


def load_ledger(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    missing = sorted(REQUIRED_COLUMNS - set(df.columns))
    if missing:
        raise ValueError(f"Ledger schema mismatch. Missing required columns: {', '.join(missing)}")

    df["signal_timestamp"] = pd.to_datetime(df["signal_timestamp"], utc=True, errors="coerce")
    df["net_return"] = pd.to_numeric(df["net_return"], errors="coerce")
    df["assignment_status"] = df["assignment_status"].astype("string").str.strip()
    df["btc_4h_regime"] = df["btc_4h_regime"].astype("string").str.strip()
    df["bar2_regime"] = df["bar2_regime"].astype("string").str.strip()

    # Only PASS assignments enter the robustness test.
    passed = df[df["assignment_status"] == "PASS"].copy()
    passed["period"] = passed["signal_timestamp"].map(canonical_period)

    # Preserve the builder's authoritative 3-dimensional regime cell:
    # BTC trend × ATR regime × bar-2 regime. Do NOT reconstruct this from
    # only btc_4h_regime/bar2_regime, because that would silently discard ATR.
    passed["regime_cell"] = passed["regime_cell"].astype("string").str.strip()
    parts = passed["regime_cell"].fillna("").str.split("__", n=2, expand=True)
    if parts.shape[1] != 3:
        raise ValueError("regime_cell schema mismatch: expected BTC__ATR__BAR2 composite cells")
    passed["atr_regime"] = parts[1].astype("string").str.strip()
    passed = passed.sort_values(["signal_timestamp", "trade_id"]).reset_index(drop=True)
    return df, passed


def build_fold_rows(passed: pd.DataFrame, min_train_n: int, min_test_n: int) -> pd.DataFrame:
    folds = [
        ("WF1", ["EARLY"], "MIDDLE"),
        ("WF2", ["EARLY", "MIDDLE"], "LATE"),
    ]
    cells = sorted(c for c in passed["regime_cell"].dropna().unique() if "<NA>" not in str(c))
    rows: List[Dict[str, object]] = []

    for fold_id, train_periods, test_period in folds:
        train = passed[passed["period"].isin(train_periods)]
        test = passed[passed["period"] == test_period]
        for cell in cells:
            tr = train[train["regime_cell"] == cell]
            te = test[test["regime_cell"] == cell]
            a = stats(tr, SEED + len(rows) + 1)
            b = stats(te, SEED + len(rows) + 1001)
            status = classify_oos(a, b, min_train_n, min_test_n)
            rows.append({
                "fold": fold_id,
                "train_periods": "+".join(train_periods),
                "test_period": test_period,
                "regime_cell": cell,
                "train_n": a["n"],
                "train_mean_net_return": a["mean_net_return"],
                "train_hit_rate": a["hit_rate"],
                "test_n": b["n"],
                "test_mean_net_return": b["mean_net_return"],
                "test_median_net_return": b["median_net_return"],
                "test_hit_rate": b["hit_rate"],
                "test_ci95_low": b["ci95_low"],
                "test_ci95_high": b["ci95_high"],
                "classification": status,
            })
    return pd.DataFrame(rows)


def build_period_rows(passed: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for period in PERIODS:
        f = passed[passed["period"] == period]
        s = stats(f, SEED + PERIODS.index(period) + 3000)
        rows.append({"period": period, **s})
    return pd.DataFrame(rows)


def build_oos_summary(folds: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for fold in ["WF1", "WF2"]:
        f = folds[folds["fold"] == fold]
        # OOS benchmark uses every passed trade in the test period, independent
        # of cell selection. This is a diagnostic baseline, not a strategy filter.
        rows.append({
            "fold": fold,
            "train_periods": f["train_periods"].iloc[0],
            "test_period": f["test_period"].iloc[0],
            "cells_with_train_support": int((f["train_n"] >= MIN_TRAIN_N).sum()),
            "cells_positive_oos": int((f["test_mean_net_return"] > 0).fillna(False).sum()),
            "cells_robust_positive_ci": int((f["classification"] == "ROBUST_POSITIVE_CI").sum()),
            "cells_sign_flip": int((f["classification"] == "SIGN_FLIP").sum()),
        })
    return pd.DataFrame(rows)


def write_json(path: Path, payload: Dict[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=2, default=lambda x: None), encoding="utf-8")


def main() -> int:
    args = parse_args()
    ledger_path = Path(args.ledger).expanduser().resolve()
    out = Path(args.output).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)

    print("=" * 78)
    print(VERSION)
    print("=" * 78)
    print("MODE                 : RESEARCH ONLY — NO ORDERS")
    print("CONTROL              : C0 FROZEN")
    print("METHOD               : EXPANDING WALK-FORWARD")
    print("FOLDS                : WF1 EARLY→MIDDLE | WF2 EARLY+MIDDLE→LATE")
    print(f"MIN TRAIN N          : {args.min_train_n}")
    print(f"MIN TEST N           : {args.min_test_n}")
    print(f"LEDGER               : {ledger_path}")
    print(f"OUTPUT               : {out}")
    print()

    full, passed = load_ledger(ledger_path)
    if len(full) != EXPECTED_TRADES:
        print(f"WARNING: ledger has {len(full)} rows; expected {EXPECTED_TRADES}.")

    period_counts = passed["period"].value_counts().reindex(PERIODS, fill_value=0)
    print(f"Ledger rows          : {len(full)}")
    print(f"PASS rows             : {len(passed)}")
    print(f"Excluded assignments  : {len(full) - len(passed)}")
    print("Period coverage       : " + ", ".join(f"{p}={period_counts[p]}" for p in PERIODS))

    outside = int((passed["period"] == "OUTSIDE").sum())
    if outside:
        raise ValueError(f"{outside} PASS trades fall outside the canonical v0.5.2 periods.")

    period_df = build_period_rows(passed)
    fold_df = build_fold_rows(passed, args.min_train_n, args.min_test_n)
    oos_df = build_oos_summary(fold_df)

    period_path = out / "time_split_period_summary.csv"
    fold_path = out / "walk_forward_by_regime_cell.csv"
    oos_path = out / "walk_forward_fold_summary.csv"
    report_path = out / "walk_forward_report.json"
    manifest_path = out / "walk_forward_run_manifest.json"

    period_df.to_csv(period_path, index=False)
    fold_df.to_csv(fold_path, index=False)
    oos_df.to_csv(oos_path, index=False)

    # Conservative gate: PASS means the analysis ran correctly and no data/schema
    # integrity failure occurred. It does NOT mean a trading strategy is approved.
    robust_cells = int((fold_df["classification"] == "ROBUST_POSITIVE_CI").sum())
    positive_oos = int((fold_df["classification"] == "POSITIVE_OOS").sum())
    sign_flips = int((fold_df["classification"] == "SIGN_FLIP").sum())

    overall = stats(passed, SEED + 9000)
    gate = "PASS"
    recommendation = (
        "ROBUSTNESS TEST COMPLETE — NO STRATEGY FILTER SELECTED. "
        "Proceed to deeper out-of-sample review only if the chronological results support it."
    )

    report = {
        "version": VERSION,
        "mode": "RESEARCH ONLY — NO ORDERS",
        "control": "C0 FROZEN",
        "method": "EXPANDING WALK-FORWARD",
        "folds": [
            {"id": "WF1", "train": ["EARLY"], "test": "MIDDLE"},
            {"id": "WF2", "train": ["EARLY", "MIDDLE"], "test": "LATE"},
        ],
        "regime_dimensions": ["btc_4h_regime", "atr_regime", "bar2_regime"],
        "ledger_rows": int(len(full)),
        "pass_rows": int(len(passed)),
        "overall": overall,
        "robust_positive_cell_results": robust_cells,
        "positive_oos_cell_results": positive_oos,
        "sign_flip_results": sign_flips,
        "gate": gate,
        "recommendation": recommendation,
        "outputs": {
            "period_summary": str(period_path),
            "walk_forward_by_regime_cell": str(fold_path),
            "fold_summary": str(oos_path),
            "json_report": str(report_path),
        },
    }
    write_json(report_path, report)

    manifest = {
        "version": VERSION,
        "ledger": str(ledger_path),
        "ledger_rows": int(len(full)),
        "pass_rows": int(len(passed)),
        "period_definition": {
            "EARLY": "2026-03-02 through 2026-04-30",
            "MIDDLE": "2026-05-01 through 2026-06-30",
            "LATE": "2026-07-01 through 2026-08-26",
        },
        "folds": [
            {"fold": "WF1", "train": ["EARLY"], "test": "MIDDLE"},
            {"fold": "WF2", "train": ["EARLY", "MIDDLE"], "test": "LATE"},
        ],
        "regime_dimensions": ["btc_4h_regime", "atr_regime", "bar2_regime"],
        "parameters": {
            "min_train_n": args.min_train_n,
            "min_test_n": args.min_test_n,
            "bootstrap_n": BOOTSTRAP_N,
            "seed": SEED,
        },
    }
    write_json(manifest_path, manifest)

    print()
    print("=" * 78)
    print("TIME-SPLIT / WALK-FORWARD RESULT")
    print("=" * 78)
    print(f"WF1: EARLY -> MIDDLE")
    print(f"WF2: EARLY+MIDDLE -> LATE")
    print(f"Robust positive CI cell results : {robust_cells}")
    print(f"Positive OOS cell results       : {positive_oos}")
    print(f"Sign-flip cell results           : {sign_flips}")
    print()
    print("PERIOD SUMMARY")
    print("-" * 78)
    for _, r in period_df.iterrows():
        print(
            f"{r['period']:>6} N={int(r['n']):>2} "
            f"mean={r['mean_net_return']:+.3%} "
            f"median={r['median_net_return']:+.3%} "
            f"hit={r['hit_rate']:.1%} "
            f"CI={r['ci95_low']:+.3%}..{r['ci95_high']:+.3%}"
        )

    print()
    print("TOP TRAIN-SUPPORTED / OOS RESULTS")
    print("-" * 78)
    display = fold_df[(fold_df["train_n"] >= args.min_train_n) & (fold_df["test_n"] >= args.min_test_n)].copy()
    if display.empty:
        print("No regime cells met the minimum train/test sample requirements.")
    else:
        display["score"] = display["test_mean_net_return"].fillna(-np.inf)
        for _, r in display.sort_values(["fold", "score"], ascending=[True, False]).head(12).iterrows():
            print(
                f"{r['fold']} {r['regime_cell']:<24} "
                f"train N={int(r['train_n']):>2} "
                f"test N={int(r['test_n']):>2} "
                f"train={r['train_mean_net_return']:+.3%} "
                f"test={r['test_mean_net_return']:+.3%} "
                f"CI={r['test_ci95_low']:+.3%}..{r['test_ci95_high']:+.3%} "
                f"{r['classification']}"
            )

    print()
    print("GUARDRAILS")
    print("-" * 78)
    print("Strategy filter selected : NO")
    print("Strategy changed         : NO")
    print("Orders allowed           : NO")
    print("Final robustness gate    : PASS")
    print("Interpretation           : RESEARCH RESULT ONLY")
    print()
    print("FILES")
    print("-" * 78)
    print(f"Period summary           : {period_path}")
    print(f"Walk-forward cells       : {fold_path}")
    print(f"Fold summary             : {oos_path}")
    print(f"JSON report              : {report_path}")
    print(f"Manifest                 : {manifest_path}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("=" * 78)
        print(f"{VERSION} — ERROR")
        print("=" * 78)
        print(f"{type(exc).__name__}: {exc}")
        raise SystemExit(1)
