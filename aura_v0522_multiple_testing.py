#!/usr/bin/env python3
"""
AURA v0.5.2.2 — MULTIPLE-TESTING CONTROL & NULL-MODEL SIGNIFICANCE

RESEARCH ONLY.
C0 remains frozen. No strategy changes. No orders.

Purpose
-------
Test whether the observed 2x2x2 regime result could plausibly arise by chance
after searching the frozen C0 ledger across all eight regime cells.

This build deliberately separates four questions:
  1) Raw one-sided permutation p-value for each regime cell.
  2) Bonferroni-adjusted p-values across the eight cells.
  3) Benjamini-Hochberg FDR q-values across the eight cells.
  4) Selection-aware max-statistic permutation p-value for the observed
     candidate. This is the primary multiple-search diagnostic.

Two null models are reported:
  A) IID permutation: randomly reassign the observed net returns to the
     frozen regime-cell memberships.
  B) Block bootstrap null: resample chronological returns in contiguous
     blocks, then assign the resulting pseudo-returns to the frozen regime
     memberships. This preserves some local temporal dependence while
     breaking the association between returns and regime labels.

Important methodological guardrails
------------------------------------
- The eight regime memberships are frozen from the v0.5.2 ledger.
- No regime definition is optimized here.
- No asset subgroup is promoted to a confirmatory hypothesis. BTC/ETH are
  reported descriptively only.
- Primary statistic is mean net return.
- Hit rate is a secondary diagnostic, not the selection statistic.
- Monte Carlo p-values use the conservative +1 correction:
      (exceedances + 1) / (iterations + 1)
- C0 remains frozen and orders_allowed is always False.
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd


VERSION = "AURA v0.5.2.2 — MULTIPLE-TESTING CONTROL & NULL-MODEL SIGNIFICANCE"
CANDIDATE = "BEAR × LOW ATR × POSITIVE bar-2"
CANDIDATE_KEY = "BEAR|LOW|POSITIVE"
EXPECTED_TRADES = 55
EXPECTED_ELIGIBLE = 52
N_CELLS = 8
ALPHA = 0.05
ITERATIONS_DEFAULT = 50000
RNG_SEED_DEFAULT = 52202
BLOCK_LENGTH_DEFAULT = 5

REQUIRED_COLUMNS = {
    "trade_id",
    "symbol",
    "signal_timestamp",
    "net_return",
    "assignment_status",
    "regime_cell",
    "btc_4h_regime",
    "bar2_regime",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def die(msg: str) -> None:
    raise RuntimeError(msg)


def numeric(s):
    return pd.to_numeric(s, errors="coerce")


def pct(x):
    if x is None or not np.isfinite(float(x)):
        return "NA"
    return f"{float(x) * 100:+.3f}%"


def pct1(x):
    if x is None or not np.isfinite(float(x)):
        return "NA"
    return f"{float(x) * 100:.1f}%"


def pstr(x):
    if x is None or not np.isfinite(float(x)):
        return "NA"
    return f"{float(x):.6f}"


def clean_cell_value(x: object) -> str:
    s = str(x).strip().upper()
    s = s.replace(" ", "_")
    s = s.replace("×", "|")
    s = s.replace("X", "|") if "|" not in s else s
    while "||" in s:
        s = s.replace("||", "|")
    return s


def normalize_symbol(x: object) -> str:
    s = str(x).upper().strip()
    if "BTC" in s:
        return "BTC"
    if "ETH" in s:
        return "ETH"
    return s


def candidate_key_from_row(row: pd.Series) -> str:
    btc = str(row.get("btc_4h_regime", "")).strip().upper()
    atr = str(row.get("_atr_regime", "")).strip().upper()
    bar2 = str(row.get("bar2_regime", "")).strip().upper()
    return f"{btc}|{atr}|{bar2}"


def mean_of(x: np.ndarray) -> float:
    return float(np.mean(x)) if len(x) else np.nan


def hit_rate(x: np.ndarray) -> float:
    return float(np.mean(x > 0)) if len(x) else np.nan


def empirical_p(exceedances: int, iterations: int) -> float:
    return (float(exceedances) + 1.0) / (float(iterations) + 1.0)


def wilson_ci(k: int, n: int, z: float = 1.959963984540054):
    if n <= 0:
        return np.nan, np.nan
    p = k / n
    den = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / den
    half = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * n)) / n) / den
    return max(0.0, center - half), min(1.0, center + half)


def bh_adjust(p_values: dict[str, float]) -> dict[str, float]:
    """Benjamini-Hochberg q-values for a finite family of one-sided p-values."""
    keys = list(p_values.keys())
    finite = [(k, float(v)) for k, v in p_values.items() if np.isfinite(v)]
    finite.sort(key=lambda kv: kv[1])
    m = len(finite)
    q = {k: np.nan for k in keys}
    running = 1.0
    for rank in range(m, 0, -1):
        k, p = finite[rank - 1]
        value = min(running, p * m / rank)
        q[k] = value
        running = value
    return q


def bootstrap_ci(values: np.ndarray, iterations: int = 5000, seed: int = 52202):
    x = np.asarray(values, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n == 0:
        return np.nan, np.nan
    if n == 1:
        return float(x[0]), float(x[0])
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(iterations, n))
    means = x[idx].mean(axis=1)
    return float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))


# ---------------------------------------------------------------------------
# Ledger loading and frozen cell construction
# ---------------------------------------------------------------------------

def load_ledger(path: Path) -> pd.DataFrame:
    if not path.exists():
        die(f"Ledger not found: {path}")

    df = pd.read_csv(path)
    missing = sorted(REQUIRED_COLUMNS - set(df.columns))
    if missing:
        die("Ledger schema mismatch. Missing required columns: " + ", ".join(missing))

    if len(df) != EXPECTED_TRADES:
        die(f"FROZEN C0 COUNT FAILURE: ledger has {len(df)} rows; expected {EXPECTED_TRADES}.")

    df = df.copy()
    df["signal_timestamp"] = pd.to_datetime(df["signal_timestamp"], utc=True, errors="coerce")
    df["net_return"] = numeric(df["net_return"])
    df["_status"] = df["assignment_status"].astype(str).str.upper().str.strip()
    df["_symbol"] = df["symbol"].map(normalize_symbol)

    # v0.5.2 uses PASS for valid frozen assignments. ASSIGNED is accepted only
    # as a compatibility value for older ledgers.
    df["_eligible"] = (
        df["_status"].isin(["PASS", "ASSIGNED"])
        & df["signal_timestamp"].notna()
        & df["net_return"].notna()
    )

    eligible = int(df["_eligible"].sum())
    if eligible != EXPECTED_ELIGIBLE:
        die(
            f"FROZEN C0 ELIGIBILITY FAILURE: found {eligible} eligible assignments; "
            f"expected {EXPECTED_ELIGIBLE}."
        )

    # Prefer the frozen ledger's explicit regime_cell, but reconstruct a clean
    # canonical cell from its three dimensions for analysis.
    if "asset_atr_pct" in df.columns:
        atr_pct = numeric(df["asset_atr_pct"])
    elif "atr_pct" in df.columns:
        atr_pct = numeric(df["atr_pct"])
    else:
        atr_pct = pd.Series(np.nan, index=df.index)

    df["_atr_pct"] = atr_pct
    df["_atr_regime"] = np.where(
        df["_atr_pct"].notna(),
        np.where(df["_atr_pct"] < 0.00596, "LOW", "HIGH"),
        "UNKNOWN",
    )

    df["_btc_regime"] = df["btc_4h_regime"].astype(str).str.upper().str.strip()
    df["_bar2_regime"] = df["bar2_regime"].astype(str).str.upper().str.strip()

    # Use the explicit frozen regime_cell where it is well-formed, while also
    # keeping the reconstructed cell for audit comparison.
    def explicit_key(x):
        s = str(x).upper().strip()
        if s in {"UNASSIGNED", "NAN", "NONE", ""}:
            return ""
        parts = s.replace("×", "|").replace(" ", "_").split("_")
        # Common forms: BEAR_LOW_POSITIVE, BEAR|LOW|POSITIVE
        if "|" in s:
            q = s.replace(" ", "").replace("×", "|")
            return q
        if len(parts) >= 3:
            return "|".join(parts[-3:])
        return s.replace("_", "|")

    df["_explicit_cell"] = df["regime_cell"].map(explicit_key)
    df["_cell"] = np.where(
        df["_eligible"],
        df["_btc_regime"] + "|" + df["_atr_regime"] + "|" + df["_bar2_regime"],
        "UNASSIGNED",
    )

    valid_cells = {
        "BULL|HIGH|NEGATIVE", "BULL|HIGH|POSITIVE",
        "BULL|LOW|NEGATIVE", "BULL|LOW|POSITIVE",
        "BEAR|HIGH|NEGATIVE", "BEAR|HIGH|POSITIVE",
        "BEAR|LOW|NEGATIVE", "BEAR|LOW|POSITIVE",
    }
    bad = sorted(set(df.loc[df["_eligible"], "_cell"]) - valid_cells)
    if bad:
        die("Unexpected reconstructed regime cells: " + ", ".join(bad))

    return df


def get_cells() -> list[str]:
    return [
        "BULL|HIGH|NEGATIVE",
        "BULL|HIGH|POSITIVE",
        "BULL|LOW|NEGATIVE",
        "BULL|LOW|POSITIVE",
        "BEAR|HIGH|NEGATIVE",
        "BEAR|HIGH|POSITIVE",
        "BEAR|LOW|NEGATIVE",
        "BEAR|LOW|POSITIVE",
    ]


def cell_label(key: str) -> str:
    a, b, c = key.split("|")
    return f"{a} {b} {c}"


# ---------------------------------------------------------------------------
# Observed statistics
# ---------------------------------------------------------------------------

def observed_cell_stats(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for cell in get_cells():
        x = df.loc[df["_eligible"] & df["_cell"].eq(cell), "net_return"].to_numpy(dtype=float)
        lo, hi = bootstrap_ci(x)
        rows.append({
            "cell": cell,
            "cell_label": cell_label(cell),
            "n": len(x),
            "mean": mean_of(x),
            "median": float(np.median(x)) if len(x) else np.nan,
            "hit_rate": hit_rate(x),
            "ci_low": lo,
            "ci_high": hi,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# IID permutation null
# ---------------------------------------------------------------------------

def run_iid_null(df: pd.DataFrame, iterations: int, seed: int):
    """
    Randomly reassign the 52 observed returns to the 52 frozen regime labels.

    This breaks the return/regime association while preserving:
      - exact number of eligible observations,
      - exact cell sizes,
      - exact empirical return distribution.

    For each iteration we record every cell mean and the maximum cell mean.
    """
    eligible = df.loc[df["_eligible"]].copy().sort_values("signal_timestamp")
    returns = eligible["net_return"].to_numpy(dtype=float)
    labels = eligible["_cell"].to_numpy(dtype=object)
    cells = get_cells()
    masks = [labels == c for c in cells]

    observed = {c: mean_of(returns[m]) for c, m in zip(cells, masks)}
    max_observed = max(v for v in observed.values() if np.isfinite(v))

    exceed = {c: 0 for c in cells}
    max_exceed = 0
    rng = np.random.default_rng(seed)

    cell_means = np.empty((iterations, len(cells)), dtype=float)
    max_means = np.empty(iterations, dtype=float)

    for i in range(iterations):
        shuffled = rng.permutation(returns)
        vals = np.array([mean_of(shuffled[m]) for m in masks], dtype=float)
        cell_means[i, :] = vals
        mx = float(np.nanmax(vals))
        max_means[i] = mx
        max_exceed += int(mx >= max_observed)
        for j, c in enumerate(cells):
            if vals[j] >= observed[c]:
                exceed[c] += 1

    pvals = {c: empirical_p(exceed[c], iterations) for c in cells}
    max_p = empirical_p(max_exceed, iterations)
    return {
        "cell_pvalues": pvals,
        "max_pvalue": max_p,
        "max_exceedances": max_exceed,
        "cell_exceedances": exceed,
        "cell_means": cell_means,
        "max_means": max_means,
    }


# ---------------------------------------------------------------------------
# Block bootstrap null
# ---------------------------------------------------------------------------

def moving_block_sample(values: np.ndarray, n: int, block_length: int, rng) -> np.ndarray:
    """Build a length-n series from circular contiguous blocks."""
    x = np.asarray(values, dtype=float)
    N = len(x)
    if N == 0:
        return np.array([], dtype=float)
    L = max(1, min(int(block_length), N))
    out = np.empty(n, dtype=float)
    pos = 0
    starts = np.arange(N)
    while pos < n:
        start = int(rng.choice(starts))
        take = min(L, n - pos)
        idx = (start + np.arange(take)) % N
        out[pos:pos + take] = x[idx]
        pos += take
    return out


def run_block_null(df: pd.DataFrame, iterations: int, seed: int, block_length: int):
    """
    Chronological block-resampling null.

    The frozen regime labels stay attached to the original timestamps/cells;
    only the chronological return sequence is resampled in contiguous blocks.
    This preserves some local serial structure while breaking regime/outcome
    association.
    """
    eligible = df.loc[df["_eligible"]].copy().sort_values("signal_timestamp")
    returns = eligible["net_return"].to_numpy(dtype=float)
    labels = eligible["_cell"].to_numpy(dtype=object)
    cells = get_cells()
    masks = [labels == c for c in cells]

    observed = {c: mean_of(returns[m]) for c, m in zip(cells, masks)}
    max_observed = max(v for v in observed.values() if np.isfinite(v))

    exceed = {c: 0 for c in cells}
    max_exceed = 0
    rng = np.random.default_rng(seed)
    cell_means = np.empty((iterations, len(cells)), dtype=float)
    max_means = np.empty(iterations, dtype=float)

    for i in range(iterations):
        sampled = moving_block_sample(returns, len(returns), block_length, rng)
        vals = np.array([mean_of(sampled[m]) for m in masks], dtype=float)
        cell_means[i, :] = vals
        mx = float(np.nanmax(vals))
        max_means[i] = mx
        max_exceed += int(mx >= max_observed)
        for j, c in enumerate(cells):
            if vals[j] >= observed[c]:
                exceed[c] += 1

    pvals = {c: empirical_p(exceed[c], iterations) for c in cells}
    max_p = empirical_p(max_exceed, iterations)
    return {
        "cell_pvalues": pvals,
        "max_pvalue": max_p,
        "max_exceedances": max_exceed,
        "cell_exceedances": exceed,
        "cell_means": cell_means,
        "max_means": max_means,
    }


# ---------------------------------------------------------------------------
# Multiple-testing tables
# ---------------------------------------------------------------------------

def build_multiple_testing_table(observed: pd.DataFrame, iid: dict, block: dict) -> pd.DataFrame:
    cells = get_cells()
    iid_p = iid["cell_pvalues"]
    block_p = block["cell_pvalues"]
    iid_bonf = {c: min(1.0, iid_p[c] * N_CELLS) for c in cells}
    block_bonf = {c: min(1.0, block_p[c] * N_CELLS) for c in cells}
    iid_bh = bh_adjust(iid_p)
    block_bh = bh_adjust(block_p)

    obs_map = observed.set_index("cell").to_dict("index")
    rows = []
    for c in cells:
        o = obs_map[c]
        rows.append({
            "cell": c,
            "cell_label": cell_label(c),
            "n": int(o["n"]),
            "observed_mean": o["mean"],
            "observed_hit_rate": o["hit_rate"],
            "observed_ci_low": o["ci_low"],
            "observed_ci_high": o["ci_high"],
            "iid_p": iid_p[c],
            "iid_bonferroni_p": iid_bonf[c],
            "iid_bh_q": iid_bh[c],
            "block_p": block_p[c],
            "block_bonferroni_p": block_bonf[c],
            "block_bh_q": block_bh[c],
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Candidate-focused diagnostics
# ---------------------------------------------------------------------------

def candidate_summary(table: pd.DataFrame, iid: dict, block: dict) -> dict:
    row = table.loc[table["cell"].eq(CANDIDATE_KEY)].iloc[0]
    iid_max_p = float(iid["max_pvalue"])
    block_max_p = float(block["max_pvalue"])

    # Primary decision is intentionally conservative: the candidate must pass
    # the selection-aware max-statistic test in BOTH null models.
    iid_pass = iid_max_p < ALPHA
    block_pass = block_max_p < ALPHA
    gate_pass = bool(iid_pass and block_pass)

    if gate_pass:
        interpretation = (
            "SELECTION-ADJUSTED NULL TEST PASSED in both IID and block null models. "
            "This is still a research result; untouched holdout validation is required."
        )
    else:
        reasons = []
        if not iid_pass:
            reasons.append(f"IID max-statistic p={iid_max_p:.6f} >= {ALPHA:.2f}")
        if not block_pass:
            reasons.append(f"block max-statistic p={block_max_p:.6f} >= {ALPHA:.2f}")
        interpretation = "PURIFICATION FAILED: " + "; ".join(reasons) + ". Candidate remains unvalidated."

    return {
        "candidate": CANDIDATE,
        "candidate_cell": CANDIDATE_KEY,
        "n": int(row["n"]),
        "observed_mean": float(row["observed_mean"]),
        "observed_hit_rate": float(row["observed_hit_rate"]),
        "observed_ci_low": float(row["observed_ci_low"]),
        "observed_ci_high": float(row["observed_ci_high"]),
        "iid_raw_p": float(row["iid_p"]),
        "iid_bonferroni_p": float(row["iid_bonferroni_p"]),
        "iid_bh_q": float(row["iid_bh_q"]),
        "iid_max_statistic_p": iid_max_p,
        "block_raw_p": float(row["block_p"]),
        "block_bonferroni_p": float(row["block_bonferroni_p"]),
        "block_bh_q": float(row["block_bh_q"]),
        "block_max_statistic_p": block_max_p,
        "iid_max_exceedances": int(iid["max_exceedances"]),
        "block_max_exceedances": int(block["max_exceedances"]),
        "gate_pass": gate_pass,
        "interpretation": interpretation,
        "orders_allowed": False,
        "strategy_filter_selected": False,
    }


def asset_diagnostics(df: pd.DataFrame) -> pd.DataFrame:
    m = df["_eligible"] & df["_cell"].eq(CANDIDATE_KEY)
    rows = []
    for asset in ["ALL", "BTC", "ETH"]:
        mm = m if asset == "ALL" else (m & df["_symbol"].eq(asset))
        x = df.loc[mm, "net_return"].to_numpy(dtype=float)
        lo, hi = bootstrap_ci(x)
        rows.append({
            "asset": asset,
            "n": len(x),
            "mean": mean_of(x),
            "median": float(np.median(x)) if len(x) else np.nan,
            "hit_rate": hit_rate(x),
            "ci_low": lo,
            "ci_high": hi,
            "role": "PRIMARY CANDIDATE" if asset == "ALL" else "DESCRIPTIVE SUBGROUP ONLY",
        })
    return pd.DataFrame(rows)


def candidate_trade_diagnostics(df: pd.DataFrame) -> pd.DataFrame:
    m = df["_eligible"] & df["_cell"].eq(CANDIDATE_KEY)
    cols = [
        "trade_id", "symbol", "signal_timestamp", "net_return",
        "btc_4h_regime", "_atr_pct", "bar2_regime", "regime_cell",
    ]
    available = [c for c in cols if c in df.columns]
    out = df.loc[m, available].copy().sort_values("signal_timestamp")
    if "_atr_pct" in out.columns:
        out = out.rename(columns={"_atr_pct": "asset_atr_pct_reconstructed"})
    return out


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def write_html(outdir: Path, meta: dict, summary: dict, table: pd.DataFrame,
               assets: pd.DataFrame, trades: pd.DataFrame) -> None:
    def html_table(df):
        return df.to_html(index=False, float_format=lambda x: f"{x:.6f}")

    banner = "PURIFICATION GATE: PASS" if summary["gate_pass"] else "PURIFICATION GATE: FAIL"
    html = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{VERSION}</title>
<style>
body {{ font-family: Arial, sans-serif; margin: 32px; line-height: 1.45; }}
.banner {{ padding: 14px; border: 2px solid #444; background: #f4f4f4; font-weight: bold; }}
table {{ border-collapse: collapse; width: 100%; margin: 12px 0 28px 0; }}
th,td {{ border: 1px solid #ccc; padding: 6px 8px; text-align: left; vertical-align: top; }}
th {{ background: #eee; }}
.small {{ color: #555; font-size: .9em; }}
</style>
</head>
<body>
<h1>{VERSION}</h1>
<div class="banner">RESEARCH ONLY — C0 FROZEN — NO STRATEGY CHANGE — NO ORDERS<br>{banner}</div>
<h2>Candidate decision</h2>
<table>
<tr><th>Metric</th><th>Value</th></tr>
<tr><td>Candidate</td><td>{summary['candidate']}</td></tr>
<tr><td>N</td><td>{summary['n']}</td></tr>
<tr><td>Observed mean</td><td>{pct(summary['observed_mean'])}</td></tr>
<tr><td>Observed hit rate</td><td>{pct1(summary['observed_hit_rate'])}</td></tr>
<tr><td>Observed bootstrap 95% CI</td><td>{pct(summary['observed_ci_low'])} to {pct(summary['observed_ci_high'])}</td></tr>
<tr><td>IID max-statistic p</td><td>{pstr(summary['iid_max_statistic_p'])}</td></tr>
<tr><td>Block max-statistic p</td><td>{pstr(summary['block_max_statistic_p'])}</td></tr>
<tr><td>Gate</td><td>{banner}</td></tr>
</table>
<p>{summary['interpretation']}</p>
<h2>All 8 regime cells</h2>
{html_table(table)}
<h2>Candidate BTC / ETH diagnostics</h2>
{html_table(assets)}
<h2>Candidate trade list</h2>
{html_table(trades)}
<h2>Methodology</h2>
<ul>
<li>IID iterations: {meta['iterations']}</li>
<li>Block-null iterations: {meta['iterations']}</li>
<li>Block length: {meta['block_length']}</li>
<li>Random seed: {meta['seed']}</li>
<li>Primary statistic: mean net return</li>
<li>Multiple-testing family: 8 frozen regime cells</li>
<li>Bonferroni: p × 8, capped at 1</li>
<li>FDR: Benjamini-Hochberg q-values</li>
<li>Selection-aware statistic: maximum positive cell mean per null iteration</li>
<li>Monte Carlo p correction: (exceedances + 1) / (iterations + 1)</li>
</ul>
<p class="small">BTC/ETH subgroup figures are descriptive only. They are not used as an independent confirmatory test because asset selection occurred after observing the data.</p>
</body>
</html>"""
    (outdir / "multiple_testing_report.html").write_text(html, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=VERSION)
    ap.add_argument("--ledger", required=True, help="Path to regime_assignment_ledger.csv")
    ap.add_argument("--output", required=True, help="Output directory")
    ap.add_argument("--iterations", type=int, default=ITERATIONS_DEFAULT,
                    help=f"Monte Carlo iterations for each null. Default: {ITERATIONS_DEFAULT}")
    ap.add_argument("--seed", type=int, default=RNG_SEED_DEFAULT,
                    help=f"Base random seed. Default: {RNG_SEED_DEFAULT}")
    ap.add_argument("--block-length", type=int, default=BLOCK_LENGTH_DEFAULT,
                    help=f"Moving block length. Default: {BLOCK_LENGTH_DEFAULT}")
    args = ap.parse_args()

    if args.iterations < 1000:
        die("ITERATIONS must be >= 1000 for this research gate.")
    if args.block_length < 1:
        die("BLOCK LENGTH must be >= 1.")

    ledger_path = Path(args.ledger)
    outdir = Path(args.output)
    outdir.mkdir(parents=True, exist_ok=True)

    print("=" * 90)
    print(VERSION)
    print("=" * 90)
    print("MODE                         : RESEARCH ONLY — NO ORDERS")
    print("CONTROL                      : C0 FROZEN")
    print(f"BASE CANDIDATE               : {CANDIDATE}")
    print("PRIMARY STATISTIC            : MEAN NET RETURN")
    print("MULTIPLE-TEST FAMILY         : 8 FROZEN REGIME CELLS")
    print(f"IID NULL ITERATIONS          : {args.iterations:,}")
    print(f"BLOCK NULL ITERATIONS        : {args.iterations:,}")
    print(f"BLOCK LENGTH                 : {args.block_length}")
    print(f"RANDOM SEED                  : {args.seed}")
    print(f"LEDGER                       : {ledger_path}")
    print(f"OUTPUT                       : {outdir}")
    print()

    df = load_ledger(ledger_path)
    observed = observed_cell_stats(df)

    candidate_row = observed.loc[observed["cell"].eq(CANDIDATE_KEY)].iloc[0]
    if int(candidate_row["n"]) != 13:
        print(f"WARNING: candidate N is {int(candidate_row['n'])}, not the previously observed 13.")
        print("The script will not silently substitute another cell or sample.")

    print(f"Frozen C0 rows              : {len(df)}")
    print(f"Eligible assignments        : {int(df['_eligible'].sum())}")
    print(f"Regime cells observed       : {(observed['n'] > 0).sum()}")
    print(f"Candidate N                 : {int(candidate_row['n'])}")
    print(f"Candidate mean              : {pct(candidate_row['mean'])}")
    print(f"Candidate bootstrap CI      : {pct(candidate_row['ci_low'])} to {pct(candidate_row['ci_high'])}")
    print()

    print("Running IID permutation null ...")
    iid = run_iid_null(df, args.iterations, args.seed)
    print(f"IID max-statistic p         : {pstr(iid['max_pvalue'])}")
    print()

    print("Running block bootstrap null ...")
    block = run_block_null(df, args.iterations, args.seed + 1, args.block_length)
    print(f"Block max-statistic p       : {pstr(block['max_pvalue'])}")
    print()

    table = build_multiple_testing_table(observed, iid, block)
    summary = candidate_summary(table, iid, block)
    assets = asset_diagnostics(df)
    trades = candidate_trade_diagnostics(df)

    # Persist full machine-readable audit artifacts.
    table.to_csv(outdir / "multiple_testing_cell_results.csv", index=False)
    assets.to_csv(outdir / "candidate_asset_diagnostics.csv", index=False)
    trades.to_csv(outdir / "candidate_trade_diagnostics.csv", index=False)

    iid_distribution = pd.DataFrame({
        "iteration": np.arange(1, args.iterations + 1),
        "max_cell_mean": iid["max_means"],
    })
    iid_distribution.to_csv(outdir / "iid_max_stat_distribution.csv", index=False)

    block_distribution = pd.DataFrame({
        "iteration": np.arange(1, args.iterations + 1),
        "max_cell_mean": block["max_means"],
    })
    block_distribution.to_csv(outdir / "block_max_stat_distribution.csv", index=False)

    meta = {
        "version": VERSION,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "python": sys.version,
        "platform": platform.platform(),
        "mode": "RESEARCH_ONLY_NO_ORDERS",
        "control": "C0_FROZEN",
        "orders_allowed": False,
        "strategy_filter_selected": False,
        "candidate": CANDIDATE,
        "candidate_cell": CANDIDATE_KEY,
        "expected_trades": EXPECTED_TRADES,
        "eligible_assignments": int(df["_eligible"].sum()),
        "regime_cell_family_size": N_CELLS,
        "iterations": args.iterations,
        "seed": args.seed,
        "block_length": args.block_length,
        "primary_statistic": "mean_net_return",
        "iid_null": "random permutation of observed returns across frozen cell memberships",
        "block_null": "circular moving-block resampling of chronological returns across frozen cell memberships",
        "monte_carlo_p_correction": "(exceedances + 1) / (iterations + 1)",
        "bonferroni_family": N_CELLS,
        "bh_family": N_CELLS,
        "asset_subgroups_confirmatory": False,
        "raw_data_modified": False,
        "gate_pass": summary["gate_pass"],
        "interpretation": summary["interpretation"],
        "outputs": [
            "multiple_testing_summary.json",
            "multiple_testing_cell_results.csv",
            "candidate_asset_diagnostics.csv",
            "candidate_trade_diagnostics.csv",
            "iid_max_stat_distribution.csv",
            "block_max_stat_distribution.csv",
            "multiple_testing_report.html",
        ],
    }
    (outdir / "multiple_testing_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    (outdir / "multiple_testing_run_manifest.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )

    write_html(outdir, meta, summary, table, assets, trades)

    print("=" * 90)
    print("MULTIPLE-TESTING RESULTS")
    print("=" * 90)
    print(table[[
        "cell_label", "n", "observed_mean", "observed_hit_rate",
        "iid_p", "iid_bonferroni_p", "iid_bh_q",
        "block_p", "block_bonferroni_p", "block_bh_q",
    ]].to_string(index=False, formatters={
        "observed_mean": pct,
        "observed_hit_rate": pct1,
        "iid_p": pstr,
        "iid_bonferroni_p": pstr,
        "iid_bh_q": pstr,
        "block_p": pstr,
        "block_bonferroni_p": pstr,
        "block_bh_q": pstr,
    }))
    print()
    print("CANDIDATE")
    print("-" * 90)
    print(f"Observed mean               : {pct(summary['observed_mean'])}")
    print(f"Observed hit rate           : {pct1(summary['observed_hit_rate'])}")
    print(f"IID raw p                   : {pstr(summary['iid_raw_p'])}")
    print(f"IID Bonferroni p            : {pstr(summary['iid_bonferroni_p'])}")
    print(f"IID BH q                    : {pstr(summary['iid_bh_q'])}")
    print(f"IID max-statistic p         : {pstr(summary['iid_max_statistic_p'])}")
    print(f"Block raw p                 : {pstr(summary['block_raw_p'])}")
    print(f"Block Bonferroni p          : {pstr(summary['block_bonferroni_p'])}")
    print(f"Block BH q                  : {pstr(summary['block_bh_q'])}")
    print(f"Block max-statistic p       : {pstr(summary['block_max_statistic_p'])}")
    print()
    print("BTC / ETH SUBGROUPS — DESCRIPTIVE ONLY")
    print(assets[["asset", "n", "mean", "hit_rate", "ci_low", "ci_high", "role"]].to_string(
        index=False,
        formatters={"mean": pct, "hit_rate": pct1, "ci_low": pct, "ci_high": pct},
    ))
    print()
    print("=" * 90)
    print("FINAL v0.5.2.2 PURIFICATION GATE")
    print("=" * 90)
    print("Gate                       :", "PASS" if summary["gate_pass"] else "FAIL")
    print("Orders allowed             : NO")
    print("Strategy filter selected   : NO")
    print("Interpretation             :", summary["interpretation"])
    print()
    print("FILES")
    for p in sorted(outdir.iterdir()):
        print(f"{p.name:38s}: {p}")
    print()
    print("AURA v0.5.2.2 completed.")
    return 0 if summary["gate_pass"] else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted. No orders were placed.")
        raise SystemExit(130)
    except Exception as exc:
        print()
        print("=" * 90)
        print("AURA v0.5.2.2 — MULTIPLE-TESTING ENGINE ERROR")
        print("=" * 90)
        print(str(exc))
        raise SystemExit(1)
