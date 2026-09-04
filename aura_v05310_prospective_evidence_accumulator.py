#!/usr/bin/env python3
"""
AURA v0.5.3.10 — Prospective Evidence Accumulator

Purpose
-------
Read the prospective observation ledger produced by AURA v0.5.3.8,
ignore incomplete observations, calculate descriptive evidence statistics,
and publish a machine-readable evidence state.

Research-only:
- No orders
- No Alpaca trading API calls
- No parameter optimization
- No strategy modification
- No backfilling of outcomes

Frozen candidate:
    BEAR x LOW ATR x POSITIVE bar-2
Frozen ATR threshold:
    0.596%
Frozen 4H EMA:
    EMA50
Forward horizon:
    4 hours

The accumulator is intentionally conservative:
- A candidate is counted only when a complete 4H forward return exists.
- Incomplete/pending observations are reported but never counted in N.
- Bootstrap uses a deterministic seed for reproducibility.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


VERSION = "AURA v0.5.3.10"

DEFAULT_LEDGER = Path(
    r"regime_output\prospective_holdout\prospective_candidate_observations.csv"
)
DEFAULT_OUT_DIR = Path(r"regime_output\prospective_evidence")

FROZEN_CANDIDATE = "BEAR x LOW ATR x POSITIVE bar-2"
FROZEN_ATR_THRESHOLD = 0.596
FROZEN_4H_EMA = "EMA50"
FORWARD_HOURS = 4

BOOTSTRAP_SEED = 53010
BOOTSTRAP_SAMPLES = 10000


def utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, pd.Timestamp):
        if pd.isna(value):
            return None
        return value.isoformat()
    return str(value)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_output_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def empty_ledger() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "symbol",
            "timestamp",
            "close",
            "atr14_pct",
            "atr_threshold_pct",
            "bar_2_close_return_pct",
            "btc_4h_regime",
            "btc_4h_close",
            "btc_4h_ema50",
            "candidate",
            "forward_1h_return_pct",
            "forward_4h_return_pct",
            "forward_hours_available",
            "note",
        ]
    )


def load_ledger(path: Path) -> pd.DataFrame:
    if not path.exists():
        return empty_ledger()

    df = pd.read_csv(path)

    required = {
        "symbol",
        "timestamp",
        "forward_4h_return_pct",
    }
    missing = sorted(required - set(df.columns))
    if missing:
        raise RuntimeError(
            "Ledger is missing required columns: " + ", ".join(missing)
        )

    df["timestamp"] = pd.to_datetime(
        df["timestamp"], utc=True, errors="coerce"
    )

    if "forward_hours_available" in df.columns:
        df["forward_hours_available"] = pd.to_numeric(
            df["forward_hours_available"], errors="coerce"
        )
    else:
        df["forward_hours_available"] = pd.NA

    df["forward_4h_return_pct"] = pd.to_numeric(
        df["forward_4h_return_pct"], errors="coerce"
    )

    if "forward_1h_return_pct" in df.columns:
        df["forward_1h_return_pct"] = pd.to_numeric(
            df["forward_1h_return_pct"], errors="coerce"
        )

    if "candidate" in df.columns:
        # Keep original text, but normalize a working boolean.
        raw = df["candidate"].astype(str).str.strip().str.lower()
        df["_candidate_bool"] = raw.isin(
            {"true", "1", "yes", "y", "candidate", "c0_signal_triggered"}
        )
    else:
        # If the column is absent, the ledger itself is not sufficiently
        # explicit to establish candidate membership.
        df["_candidate_bool"] = False

    return df


def bootstrap_mean_ci(
    values: pd.Series,
    samples: int = BOOTSTRAP_SAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float | None, float | None]:
    x = pd.to_numeric(values, errors="coerce").dropna().to_numpy(dtype=float)

    if len(x) == 0:
        return None, None
    if len(x) == 1:
        # A one-observation "CI" is deliberately reported as a point value.
        # It is not presented as strong statistical evidence.
        v = float(x[0])
        return v, v

    import numpy as np

    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(x), size=(samples, len(x)))
    means = x[idx].mean(axis=1)

    lo, hi = np.quantile(means, [0.025, 0.975])
    return float(lo), float(hi)


def evidence_status(n: int) -> str:
    if n < 10:
        return "INSUFFICIENT_EVIDENCE"
    if n < 20:
        return "PRELIMINARY_INCONCLUSIVE"
    return "FORMAL_HOLDOUT_EVALUATION"


def classify_observations(df: pd.DataFrame) -> dict[str, Any]:
    candidate_rows = df[df["_candidate_bool"]].copy()

    # Completed means the 4H outcome exists and the observation horizon
    # is actually available. This is the ONLY population used for N.
    completed = candidate_rows[
        candidate_rows["forward_4h_return_pct"].notna()
        & (
            candidate_rows["forward_hours_available"].isna()
            | (candidate_rows["forward_hours_available"] >= FORWARD_HOURS)
        )
    ].copy()

    pending = candidate_rows[
        candidate_rows["forward_4h_return_pct"].isna()
        | (
            candidate_rows["forward_hours_available"].notna()
            & (candidate_rows["forward_hours_available"] < FORWARD_HOURS)
        )
    ].copy()

    return {
        "candidate_rows": candidate_rows,
        "completed": completed,
        "pending": pending,
    }


def numeric_stats(values: pd.Series) -> dict[str, Any]:
    x = pd.to_numeric(values, errors="coerce").dropna()

    if len(x) == 0:
        return {
            "n": 0,
            "mean": None,
            "median": None,
            "std_sample": None,
            "min": None,
            "max": None,
            "positive_count": 0,
            "negative_count": 0,
            "zero_count": 0,
            "hit_rate_pct": None,
            "bootstrap_95_ci_pct": None,
        }

    ci_lo, ci_hi = bootstrap_mean_ci(x)

    return {
        "n": int(len(x)),
        "mean": float(x.mean()),
        "median": float(x.median()),
        "std_sample": float(x.std(ddof=1)) if len(x) > 1 else None,
        "min": float(x.min()),
        "max": float(x.max()),
        "positive_count": int((x > 0).sum()),
        "negative_count": int((x < 0).sum()),
        "zero_count": int((x == 0).sum()),
        "hit_rate_pct": float((x > 0).mean() * 100.0),
        "bootstrap_95_ci_pct": (
            {"lower": ci_lo, "upper": ci_hi}
            if ci_lo is not None
            else None
        ),
    }


def symbol_stats(completed: pd.DataFrame) -> dict[str, Any]:
    result: dict[str, Any] = {}

    if completed.empty:
        return result

    for symbol, g in completed.groupby("symbol", dropna=False):
        key = str(symbol)
        result[key] = numeric_stats(g["forward_4h_return_pct"])

    return result


def parameter_consistency(df: pd.DataFrame) -> dict[str, Any]:
    errors: list[str] = []

    if "atr_threshold_pct" in df.columns:
        vals = pd.to_numeric(df["atr_threshold_pct"], errors="coerce").dropna()
        if len(vals):
            bad = vals[(vals - FROZEN_ATR_THRESHOLD).abs() > 1e-9]
            if len(bad):
                errors.append(
                    f"atr_threshold_pct contains {len(bad)} value(s) "
                    f"different from frozen {FROZEN_ATR_THRESHOLD}%"
                )

    if "btc_4h_ema50" not in df.columns and len(df):
        # Missing is not automatically an error because older ledgers may
        # not carry this descriptive field.
        pass

    return {
        "parameters_changed": bool(errors),
        "errors": errors,
        "frozen_candidate": FROZEN_CANDIDATE,
        "frozen_atr_threshold_pct": FROZEN_ATR_THRESHOLD,
        "frozen_4h_ema": FROZEN_4H_EMA,
        "forward_horizon_hours": FORWARD_HOURS,
    }


def chronology_check(df: pd.DataFrame) -> dict[str, Any]:
    errors: list[str] = []

    if df.empty:
        return {"errors": 0, "details": errors}

    for symbol, g in df.groupby("symbol", dropna=False):
        g = g.dropna(subset=["timestamp"]).sort_values("timestamp")
        if g["timestamp"].duplicated().any():
            # Duplicate timestamps are not automatically an integrity
            # failure here; v0.5.3.9 owns duplicate-key auditing.
            continue

        if not g["timestamp"].is_monotonic_increasing:
            errors.append(f"{symbol}: timestamps are not monotonic")

    return {"errors": len(errors), "details": errors}


def build_state(df: pd.DataFrame, ledger_path: Path) -> dict[str, Any]:
    classified = classify_observations(df)
    completed = classified["completed"]
    pending = classified["pending"]

    stats = numeric_stats(completed["forward_4h_return_pct"])
    status = evidence_status(stats["n"])

    latest_completed = None
    if not completed.empty:
        latest_completed = completed["timestamp"].max().isoformat()

    earliest_completed = None
    if not completed.empty:
        earliest_completed = completed["timestamp"].min().isoformat()

    chronology = chronology_check(df)
    params = parameter_consistency(df)

    return {
        "agent_version": VERSION,
        "generated_at": utc_now_iso(),
        "mode": "RESEARCH_ONLY",
        "orders_allowed": False,
        "paper_execution": False,
        "live_execution": False,

        "frozen_strategy": {
            "candidate": FROZEN_CANDIDATE,
            "atr_threshold_pct": FROZEN_ATR_THRESHOLD,
            "four_hour_ema": FROZEN_4H_EMA,
            "forward_horizon_hours": FORWARD_HOURS,
        },

        "ledger": {
            "file": str(ledger_path.resolve()),
            "rows_total": int(len(df)),
            "sha256": sha256_file(ledger_path) if ledger_path.exists() else None,
            "candidate_rows": int(len(classified["candidate_rows"])),
            "pending_observations": int(len(pending)),
            "completed_observations": int(len(completed)),
            "earliest_completed_timestamp": earliest_completed,
            "latest_completed_timestamp": latest_completed,
        },

        "evidence": {
            "valid_holdout_n": int(stats["n"]),
            "evidence_status": status,
            "mean_forward_return_pct": stats["mean"],
            "median_forward_return_pct": stats["median"],
            "std_sample_pct": stats["std_sample"],
            "min_forward_return_pct": stats["min"],
            "max_forward_return_pct": stats["max"],
            "positive_count": stats["positive_count"],
            "negative_count": stats["negative_count"],
            "zero_count": stats["zero_count"],
            "hit_rate_pct": stats["hit_rate_pct"],
            "bootstrap_95_ci_pct": stats["bootstrap_95_ci_pct"],
        },

        "by_symbol": symbol_stats(completed),

        "integrity": {
            "chronology_errors": chronology["errors"],
            "parameter_errors": len(params["errors"]),
            "parameters_changed": params["parameters_changed"],
            "parameter_error_details": params["errors"],
            "pending_observations_are_not_failures": True,
            "integrity_status": (
                "PASS"
                if chronology["errors"] == 0 and not params["errors"]
                else "FAIL"
            ),
        },

        "guardrails": {
            "strategy_changed": False,
            "parameters_changed": params["parameters_changed"],
            "orders_allowed": False,
            "paper_execution": False,
            "live_execution": False,
            "promotion_allowed": False,
        },

        "interpretation": (
            "No completed prospective observations yet."
            if stats["n"] == 0
            else (
                "Evidence remains below the formal threshold."
                if stats["n"] < 20
                else "Formal holdout evaluation threshold reached; "
                     "this program still does not authorize trading."
            )
        ),
    }


def print_report(state: dict[str, Any], out_dir: Path) -> None:
    e = state["evidence"]
    l = state["ledger"]
    i = state["integrity"]

    print("=" * 92)
    print(f"{VERSION} — PROSPECTIVE EVIDENCE ACCUMULATOR")
    print("=" * 92)
    print()
    print("MODE                 : RESEARCH ONLY")
    print("ORDERS               : DISABLED")
    print("PAPER EXECUTION      : DISABLED")
    print("LIVE EXECUTION       : DISABLED")
    print()
    print(f"FROZEN CANDIDATE     : {FROZEN_CANDIDATE}")
    print(f"FROZEN ATR THRESHOLD : {FROZEN_ATR_THRESHOLD:.3f}%")
    print(f"FROZEN 4H EMA        : {FROZEN_4H_EMA}")
    print(f"FORWARD HORIZON      : {FORWARD_HOURS}H")
    print()
    print("LEDGER")
    print("-" * 92)
    print(f"TOTAL ROWS           : {l['rows_total']}")
    print(f"CANDIDATE ROWS       : {l['candidate_rows']}")
    print(f"PENDING OBSERVATIONS : {l['pending_observations']}")
    print(f"COMPLETED OBSERVATIONS: {l['completed_observations']}")
    print()
    print("EVIDENCE")
    print("-" * 92)
    print(f"VALID HOLDOUT N      : {e['valid_holdout_n']}")
    print(f"MEAN FORWARD RETURN  : {fmt(e['mean_forward_return_pct'], '%')}")
    print(f"MEDIAN FORWARD RETURN: {fmt(e['median_forward_return_pct'], '%')}")
    print(f"HIT RATE             : {fmt(e['hit_rate_pct'], '%')}")
    print(f"BOOTSTRAP 95% CI     : {fmt_ci(e['bootstrap_95_ci_pct'])}")
    print(f"EVIDENCE STATUS      : {e['evidence_status']}")
    print()
    print("BY SYMBOL")
    print("-" * 92)

    if state["by_symbol"]:
        for symbol, s in state["by_symbol"].items():
            print(
                f"{symbol:<12} N={s['n']:<4} "
                f"mean={fmt(s['mean'], '%'):<12} "
                f"hit={fmt(s['hit_rate_pct'], '%')}"
            )
    else:
        print("NONE")

    print()
    print("INTEGRITY")
    print("-" * 92)
    print(f"CHRONOLOGY ERRORS   : {i['chronology_errors']}")
    print(f"PARAMETER ERRORS    : {i['parameter_errors']}")
    print(f"PARAMETERS CHANGED  : {i['parameters_changed']}")
    print(f"INTEGRITY STATUS    : {i['integrity_status']}")
    print()
    print("GUARDRAILS")
    print("-" * 92)
    print("STRATEGY CHANGED    : False")
    print("PROMOTION ALLOWED   : False")
    print("ORDERS ALLOWED      : False")
    print("PAPER EXECUTION     : False")
    print("LIVE EXECUTION      : False")
    print()
    print("OUTPUT")
    print("-" * 92)
    print(f"STATE JSON          : {out_dir / 'prospective_evidence_state.json'}")
    print(f"SUMMARY CSV         : {out_dir / 'prospective_evidence_summary.csv'}")
    print(f"REPORT JSON         : {out_dir / 'prospective_evidence_report.json'}")
    print("=" * 92)


def fmt(value: Any, suffix: str = "") -> str:
    if value is None:
        return "N/A"
    return f"{float(value):+.4f}{suffix}"


def fmt_ci(ci: Any) -> str:
    if not ci:
        return "N/A"
    return f"[{ci['lower']:+.4f}%, {ci['upper']:+.4f}%]"


def write_outputs(state: dict[str, Any], out_dir: Path) -> None:
    ensure_output_dir(out_dir)

    state_path = out_dir / "prospective_evidence_state.json"
    report_path = out_dir / "prospective_evidence_report.json"
    summary_path = out_dir / "prospective_evidence_summary.csv"

    state_path.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    report_path.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    e = state["evidence"]
    l = state["ledger"]

    summary = pd.DataFrame(
        [
            {
                "generated_at": state["generated_at"],
                "agent_version": state["agent_version"],
                "valid_holdout_n": e["valid_holdout_n"],
                "evidence_status": e["evidence_status"],
                "mean_forward_return_pct": e["mean_forward_return_pct"],
                "median_forward_return_pct": e["median_forward_return_pct"],
                "hit_rate_pct": e["hit_rate_pct"],
                "bootstrap_ci_lower_pct": (
                    e["bootstrap_95_ci_pct"]["lower"]
                    if e["bootstrap_95_ci_pct"]
                    else None
                ),
                "bootstrap_ci_upper_pct": (
                    e["bootstrap_95_ci_pct"]["upper"]
                    if e["bootstrap_95_ci_pct"]
                    else None
                ),
                "candidate_rows": l["candidate_rows"],
                "pending_observations": l["pending_observations"],
                "completed_observations": l["completed_observations"],
                "strategy_changed": False,
                "parameters_changed": state["guardrails"]["parameters_changed"],
                "orders_allowed": False,
                "paper_execution": False,
                "live_execution": False,
                "promotion_allowed": False,
            }
        ]
    )

    summary.to_csv(summary_path, index=False)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AURA v0.5.3.10 prospective evidence accumulator"
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        default=DEFAULT_LEDGER,
        help="Prospective candidate observation CSV",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help="Evidence output directory",
    )
    args = parser.parse_args()

    print(f"{VERSION} starting...")
    print("Research-only. Orders are permanently disabled in this program.")
    print()

    try:
        df = load_ledger(args.ledger)
        state = build_state(df, args.ledger)
        write_outputs(state, args.out_dir)
        print_report(state, args.out_dir)
        return 0

    except Exception as exc:
        print()
        print(f"{VERSION} ERROR")
        print("-" * 92)
        print(f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
