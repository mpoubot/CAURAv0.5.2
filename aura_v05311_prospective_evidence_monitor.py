#!/usr/bin/env python3
"""
AURA v0.5.3.11 — Prospective Evidence Monitor

Purpose
-------
Monitor the frozen prospective experiment without changing it.

This layer sits after:
    v0.5.3.8 Prospective Observation Daemon
    v0.5.3.9 Prospective Evidence Integrity Auditor
    v0.5.3.10 Prospective Evidence Accumulator

It reads the prospective observation ledger and the v0.5.3.10 evidence state,
then produces a conservative evidence-progress state.

Research-only:
- No orders
- No paper execution
- No live execution
- No strategy changes
- No parameter optimization
- No candidate changes
- No backfilling
- No automatic promotion

Evidence gates:
    N < 10       -> INSUFFICIENT_EVIDENCE
    10 <= N < 20 -> PRELIMINARY_INCONCLUSIVE
    N >= 20      -> FORMAL_HOLDOUT_EVALUATION

IMPORTANT:
Reaching N=20 is NOT a trading authorization. Promotion remains false and
human review is required.
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


VERSION = "AURA v0.5.3.11"

DEFAULT_LEDGER = Path(
    r"regime_output\prospective_holdout\prospective_candidate_observations.csv"
)
DEFAULT_ACCUMULATOR_STATE = Path(
    r"regime_output\prospective_evidence\prospective_evidence_state.json"
)
DEFAULT_OUT_DIR = Path(r"regime_output\prospective_monitor")

FROZEN_CANDIDATE = "BEAR x LOW ATR x POSITIVE bar-2"
FROZEN_ATR_THRESHOLD = 0.596
FROZEN_4H_EMA = "EMA50"
FORWARD_HOURS = 4

GATE_PRELIMINARY_N = 10
GATE_FORMAL_N = 20

REQUIRED_LEDGER_COLUMNS = {
    "symbol",
    "timestamp",
    "forward_4h_return_pct",
}


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


def sha256_file(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_dir(path: Path) -> None:
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
        df = empty_ledger()
        df["_candidate_bool"] = pd.Series(dtype=bool)
        return df

    df = pd.read_csv(path)

    missing = sorted(REQUIRED_LEDGER_COLUMNS - set(df.columns))
    if missing:
        raise RuntimeError(
            "Ledger is missing required columns: " + ", ".join(missing)
        )

    df["timestamp"] = pd.to_datetime(
        df["timestamp"], utc=True, errors="coerce"
    )
    df["forward_4h_return_pct"] = pd.to_numeric(
        df["forward_4h_return_pct"], errors="coerce"
    )

    if "forward_hours_available" in df.columns:
        df["forward_hours_available"] = pd.to_numeric(
            df["forward_hours_available"], errors="coerce"
        )
    else:
        df["forward_hours_available"] = pd.NA

    raw = df["candidate"].astype(str).str.strip().str.lower()
    df["_candidate_bool"] = raw.isin(
        {"true", "1", "yes", "y", "candidate", "c0_signal_triggered"}
    )

    return df


def classify(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    candidates = df[df["_candidate_bool"]].copy()

    completed = candidates[
        candidates["forward_4h_return_pct"].notna()
        & (
            candidates["forward_hours_available"].isna()
            | (candidates["forward_hours_available"] >= FORWARD_HOURS)
        )
    ].copy()

    pending = candidates[
        candidates["forward_4h_return_pct"].isna()
        | (
            candidates["forward_hours_available"].notna()
            & (candidates["forward_hours_available"] < FORWARD_HOURS)
        )
    ].copy()

    return completed, pending


def load_accumulator_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def gate_for_n(n: int) -> str:
    if n < GATE_PRELIMINARY_N:
        return "INSUFFICIENT_EVIDENCE"
    if n < GATE_FORMAL_N:
        return "PRELIMINARY_INCONCLUSIVE"
    return "FORMAL_HOLDOUT_EVALUATION"


def next_gate(n: int) -> dict[str, Any]:
    if n < GATE_PRELIMINARY_N:
        return {
            "name": "PRELIMINARY_INCONCLUSIVE",
            "target_n": GATE_PRELIMINARY_N,
            "additional_completed_observations_required": GATE_PRELIMINARY_N - n,
        }
    if n < GATE_FORMAL_N:
        return {
            "name": "FORMAL_HOLDOUT_EVALUATION",
            "target_n": GATE_FORMAL_N,
            "additional_completed_observations_required": GATE_FORMAL_N - n,
        }
    return {
        "name": "HUMAN_REVIEW",
        "target_n": GATE_FORMAL_N,
        "additional_completed_observations_required": 0,
    }


def calculate_stats(completed: pd.DataFrame) -> dict[str, Any]:
    x = pd.to_numeric(
        completed["forward_4h_return_pct"], errors="coerce"
    ).dropna()

    if len(x) == 0:
        return {
            "n": 0,
            "mean_forward_return_pct": None,
            "median_forward_return_pct": None,
            "hit_rate_pct": None,
            "positive_count": 0,
            "negative_count": 0,
            "zero_count": 0,
        }

    return {
        "n": int(len(x)),
        "mean_forward_return_pct": float(x.mean()),
        "median_forward_return_pct": float(x.median()),
        "hit_rate_pct": float((x > 0).mean() * 100.0),
        "positive_count": int((x > 0).sum()),
        "negative_count": int((x < 0).sum()),
        "zero_count": int((x == 0).sum()),
    }


def symbol_progress(completed: pd.DataFrame) -> dict[str, Any]:
    result: dict[str, Any] = {}

    if completed.empty:
        return result

    for symbol, g in completed.groupby("symbol", dropna=False):
        stats = calculate_stats(g)
        result[str(symbol)] = {
            "n": stats["n"],
            "mean_forward_return_pct": stats["mean_forward_return_pct"],
            "hit_rate_pct": stats["hit_rate_pct"],
        }

    return result


def chronology_errors(df: pd.DataFrame) -> list[str]:
    errors: list[str] = []

    if df.empty:
        return errors

    for symbol, g in df.groupby("symbol", dropna=False):
        g = g.dropna(subset=["timestamp"]).sort_values("timestamp")
        if not g["timestamp"].is_monotonic_increasing:
            errors.append(f"{symbol}: timestamps are not monotonic")

    return errors


def parameter_errors(df: pd.DataFrame) -> list[str]:
    errors: list[str] = []

    if "atr_threshold_pct" in df.columns:
        vals = pd.to_numeric(
            df["atr_threshold_pct"], errors="coerce"
        ).dropna()
        bad = vals[(vals - FROZEN_ATR_THRESHOLD).abs() > 1e-9]
        if len(bad):
            errors.append(
                f"atr_threshold_pct contains {len(bad)} value(s) "
                f"different from frozen {FROZEN_ATR_THRESHOLD}%"
            )

    if "candidate" in df.columns:
        # Candidate membership is monitored, not rewritten.
        pass

    return errors


def build_monitor_state(
    df: pd.DataFrame,
    ledger_path: Path,
    accumulator_state: dict[str, Any] | None,
) -> dict[str, Any]:

    completed, pending = classify(df)
    stats = calculate_stats(completed)
    n = stats["n"]

    chrono = chronology_errors(df)
    params = parameter_errors(df)

    accumulator_n = None
    accumulator_status = None
    if accumulator_state:
        ev = accumulator_state.get("evidence", {})
        accumulator_n = ev.get("valid_holdout_n")
        accumulator_status = ev.get("evidence_status")

    # Cross-check the accumulator, but never silently replace the ledger-derived N.
    accumulator_match = (
        accumulator_n is None or int(accumulator_n) == n
    )

    integrity_status = (
        "PASS"
        if not chrono and not params and accumulator_match
        else "FAIL"
    )

    gate = gate_for_n(n)
    nxt = next_gate(n)

    return {
        "agent_version": VERSION,
        "generated_at": utc_now_iso(),
        "mode": "RESEARCH_ONLY",
        "orders_allowed": False,
        "paper_execution": False,
        "live_execution": False,

        "frozen_experiment": {
            "candidate": FROZEN_CANDIDATE,
            "atr_threshold_pct": FROZEN_ATR_THRESHOLD,
            "four_hour_ema": FROZEN_4H_EMA,
            "forward_horizon_hours": FORWARD_HOURS,
        },

        "ledger": {
            "file": str(ledger_path.resolve()),
            "rows_total": int(len(df)),
            "candidate_rows": int(len(df[df["_candidate_bool"]])),
            "pending_observations": int(len(pending)),
            "completed_observations": int(len(completed)),
            "sha256": sha256_file(ledger_path),
        },

        "evidence_progress": {
            "valid_holdout_n": n,
            "gate": gate,
            "next_gate": nxt,
            "completion_pct_to_formal_gate": round(
                min(100.0, (n / GATE_FORMAL_N) * 100.0), 2
            ),
            "mean_forward_return_pct": stats["mean_forward_return_pct"],
            "median_forward_return_pct": stats["median_forward_return_pct"],
            "hit_rate_pct": stats["hit_rate_pct"],
            "positive_count": stats["positive_count"],
            "negative_count": stats["negative_count"],
            "zero_count": stats["zero_count"],
        },

        "by_symbol": symbol_progress(completed),

        "accumulator_crosscheck": {
            "state_file": str(DEFAULT_ACCUMULATOR_STATE.resolve()),
            "state_available": accumulator_state is not None,
            "accumulator_valid_holdout_n": accumulator_n,
            "accumulator_evidence_status": accumulator_status,
            "ledger_n_matches_accumulator_n": accumulator_match,
        },

        "integrity": {
            "chronology_errors": len(chrono),
            "chronology_error_details": chrono,
            "parameter_errors": len(params),
            "parameter_error_details": params,
            "integrity_status": integrity_status,
        },

        "promotion_gate": {
            "promotion_allowed": False,
            "automatic_promotion": False,
            "human_review_required": True,
            "reason": (
                "Formal evidence gate reached; human review is required."
                if n >= GATE_FORMAL_N
                else "Formal evidence gate has not been reached."
            ),
        },

        "guardrails": {
            "strategy_changed": False,
            "parameters_changed": False,
            "candidate_changed": False,
            "orders_allowed": False,
            "paper_execution": False,
            "live_execution": False,
            "promotion_allowed": False,
        },

        "interpretation": (
            "No completed prospective observations yet."
            if n == 0
            else (
                f"{n} completed prospective observation(s); "
                f"current evidence gate is {gate}."
            )
        ),
    }


def write_outputs(state: dict[str, Any], out_dir: Path) -> None:
    ensure_dir(out_dir)

    state_path = out_dir / "prospective_monitor_state.json"
    report_path = out_dir / "prospective_monitor_report.json"
    summary_path = out_dir / "prospective_monitor_summary.csv"

    state_path.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    report_path.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    e = state["evidence_progress"]
    l = state["ledger"]
    p = state["promotion_gate"]

    pd.DataFrame([{
        "generated_at": state["generated_at"],
        "agent_version": state["agent_version"],
        "valid_holdout_n": e["valid_holdout_n"],
        "gate": e["gate"],
        "next_gate": e["next_gate"]["name"],
        "additional_n_required": e["next_gate"][
            "additional_completed_observations_required"
        ],
        "completion_pct_to_formal_gate": e["completion_pct_to_formal_gate"],
        "mean_forward_return_pct": e["mean_forward_return_pct"],
        "median_forward_return_pct": e["median_forward_return_pct"],
        "hit_rate_pct": e["hit_rate_pct"],
        "candidate_rows": l["candidate_rows"],
        "pending_observations": l["pending_observations"],
        "completed_observations": l["completed_observations"],
        "integrity_status": state["integrity"]["integrity_status"],
        "promotion_allowed": p["promotion_allowed"],
        "human_review_required": p["human_review_required"],
    }]).to_csv(summary_path, index=False)


def print_report(state: dict[str, Any], out_dir: Path) -> None:
    l = state["ledger"]
    e = state["evidence_progress"]
    i = state["integrity"]
    a = state["accumulator_crosscheck"]
    p = state["promotion_gate"]

    print("=" * 96)
    print(f"{VERSION} — PROSPECTIVE EVIDENCE MONITOR")
    print("=" * 96)
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
    print("EVIDENCE PROGRESS")
    print("-" * 96)
    print(f"VALID HOLDOUT N      : {e['valid_holdout_n']}")
    print(f"CURRENT GATE         : {e['gate']}")
    print(f"NEXT GATE            : {e['next_gate']['name']}")
    print(
        "ADDITIONAL N NEEDED  : "
        f"{e['next_gate']['additional_completed_observations_required']}"
    )
    print(
        "PROGRESS TO N=20     : "
        f"{e['completion_pct_to_formal_gate']:.2f}%"
    )
    print(f"MEAN 4H RETURN       : {fmt(e['mean_forward_return_pct'])}")
    print(f"MEDIAN 4H RETURN     : {fmt(e['median_forward_return_pct'])}")
    print(f"HIT RATE             : {fmt(e['hit_rate_pct'], '%')}")
    print()
    print("LEDGER")
    print("-" * 96)
    print(f"TOTAL ROWS           : {l['rows_total']}")
    print(f"CANDIDATE ROWS       : {l['candidate_rows']}")
    print(f"PENDING              : {l['pending_observations']}")
    print(f"COMPLETED            : {l['completed_observations']}")
    print()
    print("ACCUMULATOR CROSSCHECK")
    print("-" * 96)
    print(f"STATE AVAILABLE      : {a['state_available']}")
    print(f"ACCUMULATOR N        : {a['accumulator_valid_holdout_n']}")
    print(f"N MATCH              : {a['ledger_n_matches_accumulator_n']}")
    print()
    print("INTEGRITY")
    print("-" * 96)
    print(f"CHRONOLOGY ERRORS    : {i['chronology_errors']}")
    print(f"PARAMETER ERRORS     : {i['parameter_errors']}")
    print(f"INTEGRITY STATUS     : {i['integrity_status']}")
    print()
    print("PROMOTION")
    print("-" * 96)
    print(f"PROMOTION ALLOWED    : {p['promotion_allowed']}")
    print(f"AUTOMATIC PROMOTION  : {p['automatic_promotion']}")
    print(f"HUMAN REVIEW         : {p['human_review_required']}")
    print()
    print("OUTPUT")
    print("-" * 96)
    print(f"STATE JSON           : {out_dir / 'prospective_monitor_state.json'}")
    print(f"SUMMARY CSV          : {out_dir / 'prospective_monitor_summary.csv'}")
    print(f"REPORT JSON          : {out_dir / 'prospective_monitor_report.json'}")
    print("=" * 96)


def fmt(value: Any, suffix: str = "") -> str:
    if value is None:
        return "N/A"
    return f"{float(value):+.4f}{suffix}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AURA v0.5.3.11 prospective evidence monitor"
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        default=DEFAULT_LEDGER,
        help="Prospective candidate observation CSV",
    )
    parser.add_argument(
        "--accumulator-state",
        type=Path,
        default=DEFAULT_ACCUMULATOR_STATE,
        help="v0.5.3.10 accumulator state JSON",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help="Monitor output directory",
    )
    args = parser.parse_args()

    print(f"{VERSION} starting...")
    print("Research-only. Orders are permanently disabled in this program.")
    print()

    try:
        df = load_ledger(args.ledger)
        accumulator = load_accumulator_state(args.accumulator_state)
        state = build_monitor_state(df, args.ledger, accumulator)
        write_outputs(state, args.out_dir)
        print_report(state, args.out_dir)
        return 0
    except Exception as exc:
        print()
        print(f"{VERSION} ERROR")
        print("-" * 96)
        print(f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
