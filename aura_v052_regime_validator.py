#!/usr/bin/env python3
"""
AURA v0.5.2 — Schema-Aware Regime Validator
Research-only validation of the frozen C0 regime assignments.

Purpose
-------
Validate whether the regime differences observed by the v0.5.2 regime
builder are strong enough to justify further robustness / out-of-sample
research.

IMPORTANT:
    - No strategy settings are changed.
    - C0 remains frozen.
    - No orders are placed.
    - No regime is promoted to a trading filter by this script.
    - This script is descriptive/diagnostic validation, not optimization.

Inputs
------
Primary input:
    regime_assignment_ledger.csv

Optional:
    regime_matrix.csv
    regime_period_summary.csv

The ledger is authoritative for trade-level validation. The script
recomputes the statistics from the ledger rather than trusting summary
values, which makes the validation auditable.

Outputs
-------
<output>/
    regime_validation_summary.csv
    regime_validation_by_asset.csv
    regime_validation_by_period.csv
    regime_validation_trade_diagnostics.csv
    regime_validation_report.json
    regime_validation_report.html

Typical use
-----------
python aura_v052_regime_validator.py \
  --ledger "C:\\CAURA\\v0.5.2\\regime_output\\regime_assignment_ledger.csv" \
  --output "C:\\CAURA\\v0.5.2\\regime_output\\validation"

Or, if run from C:\\CAURA\\v0.5.2:
python aura_v052_regime_validator.py \
  --ledger ".\\regime_output\\regime_assignment_ledger.csv" \
  --output ".\\regime_output\\validation"
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

import numpy as np
import pandas as pd


VERSION = "AURA v0.5.2 — REGIME VALIDATOR — PERIOD FIXED"
MIN_NORMAL_N = 10
MIN_STRONG_N = 20
ALPHA = 0.05

REQUIRED_COLUMNS = {
    "trade_id",
    "symbol",
    "signal_timestamp",
    "entry_timestamp",
    "net_return",
    "period",
    "btc_4h_regime",
    "bar2_regime",
    "assignment_status",
    "regime_cell",
}

OPTIONAL_COLUMNS = {
    "period_source",
    "bar_2_close_return_before_costs",
    "asset_atr_pct",
    "btc_4h_lookback_count",
    "failure_reason",
}


def pct(x):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "NA"
    return f"{x * 100:.3f}%"


def pct1(x):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "NA"
    return f"{x * 100:.1f}%"


def fmt(x, digits=4):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "NA"
    return f"{x:.{digits}f}"


def normal_ci_mean(values, z=1.959963984540054):
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    n = len(values)
    if n < 2:
        return (np.nan, np.nan)
    mean = float(np.mean(values))
    se = float(np.std(values, ddof=1) / math.sqrt(n))
    return mean - z * se, mean + z * se


def bootstrap_ci_mean(values, seed=5202, iterations=5000):
    """
    Non-parametric bootstrap 95% CI for the mean.
    Used as a robustness diagnostic, not as a model-selection criterion.
    """
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    n = len(values)
    if n < 2:
        return (np.nan, np.nan)

    rng = np.random.default_rng(seed)
    samples = rng.choice(values, size=(iterations, n), replace=True)
    means = samples.mean(axis=1)
    return float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))


def wilson_ci(k, n, z=1.959963984540054):
    if n <= 0:
        return (np.nan, np.nan)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (
        z
        * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
        / denom
    )
    return max(0.0, center - half), min(1.0, center + half)


def permutation_pvalue_two_group(a, b, seed=5202, iterations=10000):
    """
    Two-sided permutation test on mean difference.
    H0: the two groups are exchangeable.

    This is deliberately used as a diagnostic only. No multiple-testing
    correction is used to select a trading rule; the report explicitly
    warns about the exploratory nature of the 8-cell matrix.
    """
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    a = a[np.isfinite(a)]
    b = b[np.isfinite(b)]
    if len(a) < 2 or len(b) < 2:
        return np.nan

    observed = float(a.mean() - b.mean())
    combined = np.concatenate([a, b])
    n_a = len(a)
    rng = np.random.default_rng(seed)

    count = 0
    for _ in range(iterations):
        perm = rng.permutation(combined)
        diff = float(perm[:n_a].mean() - perm[n_a:].mean())
        if abs(diff) >= abs(observed):
            count += 1
    return (count + 1) / (iterations + 1)


def max_drawdown_from_returns(values):
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if len(values) == 0:
        return np.nan
    equity = np.cumprod(1.0 + values)
    peaks = np.maximum.accumulate(equity)
    dd = equity / peaks - 1.0
    return float(dd.min())


def safe_json_value(v):
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return None if not np.isfinite(v) else float(v)
    if isinstance(v, float):
        return None if not np.isfinite(v) else v
    if isinstance(v, (pd.Timestamp, datetime)):
        return v.isoformat()
    return v


def dataframe_records(df):
    records = []
    for row in df.to_dict(orient="records"):
        records.append({k: safe_json_value(v) for k, v in row.items()})
    return records


def load_ledger(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Ledger file not found: {path}")

    df = pd.read_csv(path)

    missing = sorted(REQUIRED_COLUMNS - set(df.columns))
    if missing:
        raise ValueError(
            "Ledger schema mismatch. Missing required columns: "
            + ", ".join(missing)
        )

    # Keep original strings where useful, but parse timestamps for ordering.
    for col in ("signal_timestamp", "entry_timestamp"):
        df[col] = pd.to_datetime(df[col], utc=True, errors="coerce")

    df["net_return"] = pd.to_numeric(df["net_return"], errors="coerce")

    if "bar_2_close_return_before_costs" in df.columns:
        df["bar_2_close_return_before_costs"] = pd.to_numeric(
            df["bar_2_close_return_before_costs"], errors="coerce"
        )

    if "asset_atr_pct" in df.columns:
        df["asset_atr_pct"] = pd.to_numeric(
            df["asset_atr_pct"], errors="coerce"
        )

    # Normalize categorical fields without changing their meaning.
    # v0.5.2 builder schema does not store a standalone atr_regime column.
    # The complete assigned regime is stored in regime_cell as:
    #   btc_4h_regime__atr_regime__bar2_regime
    for col in (
        "symbol",
        "period",
        "btc_4h_regime",
        "bar2_regime",
        "assignment_status",
        "regime_cell",
    ):
        df[col] = df[col].astype("string").str.strip()

    # PERIOD FIX — v0.5.2 canonical diagnostic buckets.
    #
    # The builder defines period from signal_timestamp using fixed,
    # chronological buckets:
    #   EARLY  = 2026-03-02 through 2026-04-30
    #   MIDDLE = 2026-05-01 through 2026-06-30
    #   LATE   = 2026-07-01 through 2026-08-26
    #
    # The current ledger contains a blank period field, so the validator
    # must derive the period from the authoritative signal_timestamp.
    # This does not alter the ledger; it only reconstructs the builder's
    # existing descriptive period classification for validation.
    period_cuts = [
        ("EARLY", pd.Timestamp("2026-03-02T00:00:00Z"),
                  pd.Timestamp("2026-04-30T23:59:59Z")),
        ("MIDDLE", pd.Timestamp("2026-05-01T00:00:00Z"),
                   pd.Timestamp("2026-06-30T23:59:59Z")),
        ("LATE", pd.Timestamp("2026-07-01T00:00:00Z"),
                 pd.Timestamp("2026-08-26T23:59:59Z")),
    ]

    def canonical_period(ts):
        if pd.isna(ts):
            return "OUTSIDE"
        for name, start, end in period_cuts:
            if start <= ts <= end:
                return name
        return "OUTSIDE"

    derived_period = df["signal_timestamp"].map(canonical_period).astype("string")

    # Prefer the canonical timestamp-derived period. This repairs blank
    # ledger period values while remaining deterministic and auditable.
    df["period_source"] = np.where(
        df["period"].isin(["EARLY", "MIDDLE", "LATE", "OUTSIDE"]),
        "LEDGER",
        "SIGNAL_TIMESTAMP_DERIVED",
    )
    df["period"] = derived_period

    # Schema-aware derivation: extract ATR regime from the builder's
    # composite regime_cell. No new threshold is invented and the ledger
    # itself remains untouched. Failed/UNASSIGNED rows remain NA.
    if "atr_regime" not in df.columns:
        parts = df["regime_cell"].fillna("").astype(str).str.split("__", n=2, expand=True)
        df["atr_regime"] = pd.Series(pd.NA, index=df.index, dtype="string")
        if parts.shape[1] >= 3:
            passed_mask = df["assignment_status"].eq("PASS")
            df.loc[passed_mask, "atr_regime"] = parts.loc[passed_mask, 1].astype("string")
    else:
        df["atr_regime"] = df["atr_regime"].astype("string").str.strip()

    return df


def validate_integrity(df: pd.DataFrame):
    checks = []

    def add(name, passed, detail):
        checks.append(
            {
                "check": name,
                "status": "PASS" if passed else "FAIL",
                "detail": detail,
            }
        )

    add(
        "row_structure",
        len(df) > 0,
        f"{len(df)} ledger rows loaded",
    )

    add(
        "unique_trade_ids",
        df["trade_id"].nunique(dropna=True) == len(df),
        f"{df['trade_id'].nunique(dropna=True)} unique trade IDs / {len(df)} rows",
    )

    add(
        "valid_timestamps",
        df["signal_timestamp"].notna().all()
        and df["entry_timestamp"].notna().all(),
        f"invalid signal timestamps={df['signal_timestamp'].isna().sum()}, "
        f"invalid entry timestamps={df['entry_timestamp'].isna().sum()}",
    )

    add(
        "valid_returns",
        df["net_return"].notna().all()
        and np.isfinite(df["net_return"]).all(),
        f"invalid net returns={df['net_return'].isna().sum()}",
    )

    valid_status = {"PASS", "FAIL_EMA_WARMUP", "FAIL_MISSING_ASSET_BARS"}
    unknown_status = sorted(
        set(df["assignment_status"].dropna().unique()) - valid_status
    )
    add(
        "known_assignment_status",
        len(unknown_status) == 0,
        "unknown statuses=" + (",".join(unknown_status) if unknown_status else "none"),
    )

    passed = df[df["assignment_status"].eq("PASS")].copy()
    add(
        "passed_assignments_present",
        len(passed) > 0,
        f"{len(passed)} passed assignments",
    )

    # Confirm the derived ATR regime agrees with the composite regime_cell.
    mismatches = 0
    for _, row in passed.iterrows():
        parts = str(row.get("regime_cell", "")).split("|")
        if len(parts) == 3 and str(row.get("atr_regime", "")).strip() != parts[1].strip():
            mismatches += 1
    add(
        "regime_cell_atr_consistency",
        mismatches == 0,
        f"ATR component mismatches={mismatches}",
    )

    # No duplicate trade IDs among passed rows.
    add(
        "passed_trade_ids_unique",
        passed["trade_id"].nunique(dropna=True) == len(passed),
        f"{passed['trade_id'].nunique(dropna=True)} unique passed trade IDs / {len(passed)} rows",
    )

    return checks


def classify_status(n, ci_low, ci_high, mean_value):
    if n < MIN_NORMAL_N:
        return "SMALL_SAMPLE"
    if np.isfinite(ci_low) and np.isfinite(ci_high) and ci_low > 0:
        return "POSITIVE_CI"
    if np.isfinite(ci_low) and np.isfinite(ci_high) and ci_high < 0:
        return "NEGATIVE_CI"
    if abs(mean_value) < 0.001:
        return "NEAR_ZERO"
    return "UNCERTAIN"


def build_regime_stats(passed: pd.DataFrame):
    group_cols = ["btc_4h_regime", "atr_regime", "bar2_regime"]
    rows = []

    for keys, g in passed.groupby(group_cols, dropna=False, sort=True):
        btc_trend, atr_regime, bar2_regime = keys
        vals = g["net_return"].dropna().to_numpy(dtype=float)
        n = len(vals)

        mean = float(np.mean(vals)) if n else np.nan
        median = float(np.median(vals)) if n else np.nan
        hit = float(np.mean(vals > 0)) if n else np.nan
        gross_positive = float(vals[vals > 0].sum()) if np.any(vals > 0) else 0.0
        gross_negative = float(-vals[vals < 0].sum()) if np.any(vals < 0) else 0.0
        profit_factor = (
            gross_positive / gross_negative
            if gross_negative > 0
            else (np.inf if gross_positive > 0 else np.nan)
        )

        ci_low, ci_high = normal_ci_mean(vals)
        boot_low, boot_high = bootstrap_ci_mean(vals)

        hit_low, hit_high = wilson_ci(int(np.sum(vals > 0)), n)

        rows.append(
            {
                "btc_trend": btc_trend,
                "atr_regime": atr_regime,
                "bar2_regime": bar2_regime,
                "regime_cell": f"{btc_trend}__{atr_regime}__{bar2_regime}",
                "n": n,
                "mean_net_return": mean,
                "median_net_return": median,
                "hit_rate": hit,
                "ci95_mean_low_normal": ci_low,
                "ci95_mean_high_normal": ci_high,
                "ci95_mean_low_bootstrap": boot_low,
                "ci95_mean_high_bootstrap": boot_high,
                "ci95_hit_low_wilson": hit_low,
                "ci95_hit_high_wilson": hit_high,
                "profit_factor": profit_factor,
                "max_drawdown_trade_order": max_drawdown_from_returns(vals),
                "status": classify_status(n, boot_low, boot_high, mean),
            }
        )

    out = pd.DataFrame(rows)

    # Add a rank purely for descriptive reporting. It is NOT a selection.
    if not out.empty:
        out["descriptive_mean_rank"] = (
            out["mean_net_return"].rank(ascending=False, method="min").astype(int)
        )

    return out


def build_asset_stats(passed: pd.DataFrame):
    rows = []

    for symbol, g in passed.groupby("symbol", dropna=False, sort=True):
        vals = g["net_return"].dropna().to_numpy(dtype=float)
        n = len(vals)
        mean = float(vals.mean()) if n else np.nan
        median = float(np.median(vals)) if n else np.nan
        hit = float(np.mean(vals > 0)) if n else np.nan
        low, high = bootstrap_ci_mean(vals)

        rows.append(
            {
                "symbol": symbol,
                "n": n,
                "mean_net_return": mean,
                "median_net_return": median,
                "hit_rate": hit,
                "ci95_mean_low_bootstrap": low,
                "ci95_mean_high_bootstrap": high,
                "max_drawdown_trade_order": max_drawdown_from_returns(vals),
            }
        )

    return pd.DataFrame(rows)


def build_period_stats(passed: pd.DataFrame):
    rows = []

    for period, g in passed.groupby("period", dropna=False, sort=True):
        vals = g["net_return"].dropna().to_numpy(dtype=float)
        n = len(vals)
        mean = float(vals.mean()) if n else np.nan
        median = float(np.median(vals)) if n else np.nan
        hit = float(np.mean(vals > 0)) if n else np.nan
        low, high = bootstrap_ci_mean(vals)

        rows.append(
            {
                "period": period,
                "n": n,
                "mean_net_return": mean,
                "median_net_return": median,
                "hit_rate": hit,
                "ci95_mean_low_bootstrap": low,
                "ci95_mean_high_bootstrap": high,
            }
        )

    return pd.DataFrame(rows)


def build_trade_diagnostics(passed: pd.DataFrame):
    out = passed.copy()

    # Convert timestamps to strings for CSV friendliness.
    out["signal_timestamp"] = out["signal_timestamp"].dt.strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    out["entry_timestamp"] = out["entry_timestamp"].dt.strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    # Descriptive flags only.
    out["positive_trade"] = out["net_return"] > 0
    out["large_positive_trade"] = out["net_return"] >= 0.01
    out["large_negative_trade"] = out["net_return"] <= -0.01

    return out


def build_comparisons(passed: pd.DataFrame, regime_stats: pd.DataFrame):
    """
    Descriptive pairwise diagnostics.

    The main comparison is POSITIVE vs NEGATIVE bar2 within each BTC trend /
    ATR regime where both sides have observations.

    A second comparison evaluates BULL vs BEAR within each ATR/bar2 cell.

    These tests are deliberately exploratory. They are not used to select
    a trading filter.
    """
    rows = []

    # Bar-2 positive vs negative.
    for (btc, atr), g in passed.groupby(
        ["btc_4h_regime", "atr_regime"], dropna=False
    ):
        pos = g[g["bar2_regime"].eq("POSITIVE")]["net_return"].dropna().to_numpy()
        neg = g[g["bar2_regime"].eq("NEGATIVE")]["net_return"].dropna().to_numpy()

        if len(pos) >= 2 and len(neg) >= 2:
            p = permutation_pvalue_two_group(pos, neg)
        else:
            p = np.nan

        rows.append(
            {
                "comparison": "BAR2_POSITIVE_vs_NEGATIVE",
                "context": f"{btc}__{atr}",
                "group_a": "POSITIVE",
                "group_b": "NEGATIVE",
                "n_a": len(pos),
                "n_b": len(neg),
                "mean_a": float(pos.mean()) if len(pos) else np.nan,
                "mean_b": float(neg.mean()) if len(neg) else np.nan,
                "mean_difference_a_minus_b": (
                    float(pos.mean() - neg.mean())
                    if len(pos) and len(neg)
                    else np.nan
                ),
                "permutation_p_two_sided": p,
            }
        )

    # BTC trend comparison.
    for (atr, bar2), g in passed.groupby(
        ["atr_regime", "bar2_regime"], dropna=False
    ):
        bull = g[g["btc_4h_regime"].eq("BULL")]["net_return"].dropna().to_numpy()
        bear = g[g["btc_4h_regime"].eq("BEAR")]["net_return"].dropna().to_numpy()

        if len(bull) >= 2 and len(bear) >= 2:
            p = permutation_pvalue_two_group(bull, bear, seed=5203)
        else:
            p = np.nan

        rows.append(
            {
                "comparison": "BULL_vs_BEAR",
                "context": f"{atr}__{bar2}",
                "group_a": "BULL",
                "group_b": "BEAR",
                "n_a": len(bull),
                "n_b": len(bear),
                "mean_a": float(bull.mean()) if len(bull) else np.nan,
                "mean_b": float(bear.mean()) if len(bear) else np.nan,
                "mean_difference_a_minus_b": (
                    float(bull.mean() - bear.mean())
                    if len(bull) and len(bear)
                    else np.nan
                ),
                "permutation_p_two_sided": p,
            }
        )

    return pd.DataFrame(rows)


def build_robustness_summary(passed, regime_stats, asset_stats, period_stats, comparisons):
    """
    Create a concise research verdict.

    Conservative rules:
      - A regime is NOT called robust merely because its sample mean is positive.
      - At least 10 observations are required before a cell is considered
        normally diagnosable.
      - A positive bootstrap CI is interesting but still requires later
        time-split / OOS confirmation.
      - No automatic strategy filter is selected.
    """
    total = len(passed)

    if total:
        overall_vals = passed["net_return"].dropna().to_numpy(dtype=float)
        overall_mean = float(overall_vals.mean())
        overall_median = float(np.median(overall_vals))
        overall_hit = float(np.mean(overall_vals > 0))
        overall_low, overall_high = bootstrap_ci_mean(overall_vals)
        overall_dd = max_drawdown_from_returns(overall_vals)
    else:
        overall_mean = overall_median = overall_hit = overall_low = overall_high = overall_dd = np.nan

    positive_ci_cells = (
        int(
            (
                (regime_stats["n"] >= MIN_NORMAL_N)
                & (regime_stats["ci95_mean_low_bootstrap"] > 0)
            ).sum()
        )
        if not regime_stats.empty
        else 0
    )

    negative_ci_cells = (
        int(
            (
                (regime_stats["n"] >= MIN_NORMAL_N)
                & (regime_stats["ci95_mean_high_bootstrap"] < 0)
            ).sum()
        )
        if not regime_stats.empty
        else 0
    )

    exploratory_p05 = (
        int((comparisons["permutation_p_two_sided"] < 0.05).sum())
        if not comparisons.empty
        else 0
    )

    return {
        "validation_status": "RESEARCH_VALIDATION_COMPLETE",
        "strategy_filter_selected": False,
        "strategy_changed": False,
        "orders_allowed": False,
        "c0_frozen": True,
        "passed_assignments": total,
        "regime_cells_observed": int(len(regime_stats)),
        "positive_bootstrap_ci_cells_n_ge_10": positive_ci_cells,
        "negative_bootstrap_ci_cells_n_ge_10": negative_ci_cells,
        "exploratory_pairwise_p_values_below_0_05": exploratory_p05,
        "overall_mean_net_return": overall_mean,
        "overall_median_net_return": overall_median,
        "overall_hit_rate": overall_hit,
        "overall_ci95_mean_low_bootstrap": overall_low,
        "overall_ci95_mean_high_bootstrap": overall_high,
        "overall_trade_order_max_drawdown": overall_dd,
        "assets_observed": int(len(asset_stats)),
        "periods_observed": int(len(period_stats)),
        "minimum_cell_n_for_normal_diagnostic": MIN_NORMAL_N,
        "strong_sample_n_reference": MIN_STRONG_N,
        "interpretation": (
            "Descriptive regime differences are now validated at the "
            "trade-ledger level, but no regime is promoted to a trading "
            "filter. Further time-split, asset-split and out-of-sample "
            "validation is required before any strategy change."
        ),
    }


def html_escape(x):
    import html
    return html.escape(str(x))


def df_to_html(df, max_rows=100):
    if df is None or df.empty:
        return "<p><em>No rows.</em></p>"
    shown = df.head(max_rows).copy()

    # Human-readable percentage columns.
    for col in shown.columns:
        if (
            "return" in col
            or "hit_rate" in col
            or col.endswith("_low")
            or col.endswith("_high")
            or "drawdown" in col
        ):
            if pd.api.types.is_numeric_dtype(shown[col]):
                shown[col] = shown[col].map(
                    lambda x: "NA" if pd.isna(x) else f"{x * 100:.3f}%"
                )
        elif "p_two_sided" in col and pd.api.types.is_numeric_dtype(shown[col]):
            shown[col] = shown[col].map(
                lambda x: "NA" if pd.isna(x) else f"{x:.4f}"
            )

    return shown.to_html(
        index=False,
        border=0,
        classes="data",
        na_rep="NA",
        justify="left",
    )


def write_html_report(
    path,
    meta,
    integrity_checks,
    robustness,
    regime_stats,
    asset_stats,
    period_stats,
    comparisons,
):
    status_rows = []
    for check in integrity_checks:
        status_rows.append(
            f"<tr><td>{html_escape(check['check'])}</td>"
            f"<td>{html_escape(check['status'])}</td>"
            f"<td>{html_escape(check['detail'])}</td></tr>"
        )

    overall = [
        ("Passed assignments", robustness["passed_assignments"]),
        ("Observed regime cells", robustness["regime_cells_observed"]),
        (
            "Positive bootstrap-CI cells (N≥10)",
            robustness["positive_bootstrap_ci_cells_n_ge_10"],
        ),
        (
            "Negative bootstrap-CI cells (N≥10)",
            robustness["negative_bootstrap_ci_cells_n_ge_10"],
        ),
        ("Overall mean net return", pct(robustness["overall_mean_net_return"])),
        ("Overall median net return", pct(robustness["overall_median_net_return"])),
        ("Overall hit rate", pct1(robustness["overall_hit_rate"])),
        (
            "Overall bootstrap 95% CI",
            f"{pct(robustness['overall_ci95_mean_low_bootstrap'])} to "
            f"{pct(robustness['overall_ci95_mean_high_bootstrap'])}",
        ),
        (
            "Trade-order max drawdown",
            pct(robustness["overall_trade_order_max_drawdown"]),
        ),
        ("Strategy filter selected", "NO"),
        ("Orders allowed", "NO"),
    ]

    overall_html = "".join(
        f"<tr><td>{html_escape(k)}</td><td>{html_escape(v)}</td></tr>"
        for k, v in overall
    )

    html = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{html_escape(VERSION)}</title>
<style>
body {{
    font-family: Arial, sans-serif;
    margin: 32px;
    line-height: 1.45;
}}
h1, h2 {{ margin-top: 28px; }}
.banner {{
    padding: 14px;
    border: 2px solid #444;
    background: #f4f4f4;
    font-weight: bold;
}}
table {{
    border-collapse: collapse;
    width: 100%;
    margin: 12px 0 24px 0;
}}
th, td {{
    border: 1px solid #ccc;
    padding: 6px 8px;
    vertical-align: top;
    text-align: left;
}}
th {{ background: #eee; }}
.small {{ color: #555; font-size: 0.9em; }}
</style>
</head>
<body>
<h1>{html_escape(VERSION)}</h1>

<div class="banner">
RESEARCH ONLY — C0 FROZEN — NO STRATEGY CHANGE — NO ORDERS
</div>

<p>{html_escape(robustness["interpretation"])}</p>

<h2>Run metadata</h2>
<table>
<tr><td>Ledger</td><td>{html_escape(meta["ledger"])}</td></tr>
<tr><td>Generated UTC</td><td>{html_escape(meta["generated_utc"])}</td></tr>
<tr><td>Validator version</td><td>{html_escape(meta["version"])}</td></tr>
</table>

<h2>Overall validation</h2>
<table>
<tr><th>Metric</th><th>Value</th></tr>
{overall_html}
</table>

<h2>Integrity checks</h2>
<table>
<tr><th>Check</th><th>Status</th><th>Detail</th></tr>
{"".join(status_rows)}
</table>

<h2>Regime cells</h2>
{df_to_html(regime_stats)}

<h2>By asset</h2>
{df_to_html(asset_stats)}

<h2>By period</h2>
{df_to_html(period_stats)}

<h2>Exploratory pairwise comparisons</h2>
{df_to_html(comparisons)}

<p class="small">
Important: bootstrap confidence intervals and permutation p-values are
diagnostic statistics only. The eight-cell regime matrix was explored after
the fact, so these results are not sufficient to establish a deployable
edge. The next research stage must use time-split / walk-forward and
out-of-sample validation.
</p>
</body>
</html>
"""
    path.write_text(html, encoding="utf-8")


def parse_args():
    parser = argparse.ArgumentParser(
        description="AURA v0.5.2 research-only regime validator."
    )
    parser.add_argument(
        "--ledger",
        required=True,
        help="Path to regime_assignment_ledger.csv",
    )
    parser.add_argument(
        "--matrix",
        default=None,
        help="Optional path to regime_matrix.csv",
    )
    parser.add_argument(
        "--period-summary",
        default=None,
        help="Optional path to regime_period_summary.csv",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Output directory.",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    ledger_path = Path(args.ledger).expanduser().resolve()
    output_dir = Path(args.output).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 76)
    print(VERSION)
    print("=" * 76)
    print("MODE                  : RESEARCH ONLY — NO ORDERS")
    print("CONTROL               : C0 FROZEN")
    print("METHOD                : TRADE-LEDGER RECOMPUTATION")
    print(f"LEDGER                : {ledger_path}")
    print(f"OUTPUT                : {output_dir}")
    print()

    try:
        df = load_ledger(ledger_path)
        integrity = validate_integrity(df)

        passed = df[df["assignment_status"].eq("PASS")].copy()
        passed = passed.sort_values(
            ["signal_timestamp", "trade_id"], kind="mergesort"
        )

        regime_stats = build_regime_stats(passed)
        asset_stats = build_asset_stats(passed)
        period_stats = build_period_stats(passed)
        comparisons = build_comparisons(passed, regime_stats)
        trade_diag = build_trade_diagnostics(passed)

        robustness = build_robustness_summary(
            passed,
            regime_stats,
            asset_stats,
            period_stats,
            comparisons,
        )

        meta = {
            "version": VERSION,
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "ledger": str(ledger_path),
            "matrix_input": str(Path(args.matrix).resolve())
            if args.matrix
            else None,
            "period_summary_input": str(Path(args.period_summary).resolve())
            if args.period_summary
            else None,
            "python": sys.version,
            "pandas": pd.__version__,
            "numpy": np.__version__,
        }

        # CSV outputs.
        regime_stats.to_csv(
            output_dir / "regime_validation_summary.csv", index=False
        )
        asset_stats.to_csv(
            output_dir / "regime_validation_by_asset.csv", index=False
        )
        period_stats.to_csv(
            output_dir / "regime_validation_by_period.csv", index=False
        )
        trade_diag.to_csv(
            output_dir / "regime_validation_trade_diagnostics.csv", index=False
        )
        comparisons.to_csv(
            output_dir / "regime_validation_comparisons.csv", index=False
        )

        # JSON report.
        report = {
            "metadata": meta,
            "integrity_checks": integrity,
            "robustness_summary": robustness,
            "regime_cells": dataframe_records(regime_stats),
            "by_asset": dataframe_records(asset_stats),
            "by_period": dataframe_records(period_stats),
            "comparisons": dataframe_records(comparisons),
        }

        json_path = output_dir / "regime_validation_report.json"
        json_path.write_text(
            json.dumps(report, indent=2, allow_nan=False),
            encoding="utf-8",
        )

        html_path = output_dir / "regime_validation_report.html"
        write_html_report(
            html_path,
            meta,
            integrity,
            robustness,
            regime_stats,
            asset_stats,
            period_stats,
            comparisons,
        )

        # Console summary.
        print("=" * 76)
        print("AURA v0.5.2 — REGIME VALIDATION RESULT")
        print("=" * 76)
        print(f"Ledger rows loaded      : {len(df)}")
        print(f"PASS assignments        : {len(passed)}")
        print(
            f"Rejected assignments   : "
            f"{len(df) - len(passed)}"
        )
        print(f"Regime cells observed  : {len(regime_stats)}")
        if not period_stats.empty:
            observed_periods = ", ".join(
                str(x) for x in period_stats["period"].dropna().unique()
            )
            print(f"Periods observed       : {observed_periods}")
        derived_n = int((df["period_source"] == "SIGNAL_TIMESTAMP_DERIVED").sum())
        print(f"Periods timestamp-fixed: {derived_n}")
        print(
            "Positive bootstrap CI  : "
            f"{robustness['positive_bootstrap_ci_cells_n_ge_10']} "
            f"cells with N >= {MIN_NORMAL_N}"
        )
        print(
            "Negative bootstrap CI  : "
            f"{robustness['negative_bootstrap_ci_cells_n_ge_10']} "
            f"cells with N >= {MIN_NORMAL_N}"
        )

        print()
        print("OVERALL")
        print("-" * 76)
        print(f"Mean net return        : {pct(robustness['overall_mean_net_return'])}")
        print(f"Median net return      : {pct(robustness['overall_median_net_return'])}")
        print(f"Hit rate               : {pct1(robustness['overall_hit_rate'])}")
        print(
            "Bootstrap 95% CI       : "
            f"{pct(robustness['overall_ci95_mean_low_bootstrap'])} "
            f"to {pct(robustness['overall_ci95_mean_high_bootstrap'])}"
        )

        print()
        print("REGIME CELLS")
        print("-" * 76)
        if regime_stats.empty:
            print("No valid regime assignments.")
        else:
            for _, r in regime_stats.iterrows():
                print(
                    f"{r['btc_trend']:4} "
                    f"{r['atr_regime']:4} "
                    f"{r['bar2_regime']:8} "
                    f"N={int(r['n']):2d} "
                    f"mean={pct(r['mean_net_return']):>9} "
                    f"median={pct(r['median_net_return']):>9} "
                    f"hit={pct1(r['hit_rate']):>7} "
                    f"{r['status']}"
                )

        print()
        print("GUARDRAILS")
        print("-" * 76)
        print("Strategy filter selected : NO")
        print("Strategy changed         : NO")
        print("Orders allowed            : NO")
        print(
            "Next stage                : TIME-SPLIT / WALK-FORWARD "
            "ROBUSTNESS"
        )

        print()
        print("FILES")
        print("-" * 76)
        print(f"Summary                  : {output_dir / 'regime_validation_summary.csv'}")
        print(f"Asset                    : {output_dir / 'regime_validation_by_asset.csv'}")
        print(f"Period                   : {output_dir / 'regime_validation_by_period.csv'}")
        print(f"Trade diagnostics        : {output_dir / 'regime_validation_trade_diagnostics.csv'}")
        print(f"Comparisons              : {output_dir / 'regime_validation_comparisons.csv'}")
        print(f"JSON report              : {json_path}")
        print(f"HTML report              : {html_path}")

        failed_integrity = [x for x in integrity if x["status"] == "FAIL"]
        print()
        if failed_integrity:
            print("FINAL REGIME VALIDATION GATE: FAIL")
            print("Fix ledger integrity issues before interpreting results.")
            return 2

        print("FINAL REGIME VALIDATION GATE: PASS")
        print(
            "Research validation completed. "
            "No strategy filter has been selected."
        )
        return 0

    except Exception as exc:
        print()
        print("=" * 76)
        print("AURA v0.5.2 — REGIME VALIDATOR ERROR")
        print("=" * 76)
        print(f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
