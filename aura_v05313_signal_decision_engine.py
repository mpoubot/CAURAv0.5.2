#!/usr/bin/env python3
"""
AURA v0.5.3.13 — Signal Decision Engine

LOCKED ARCHITECTURE
-------------------
Consumes ONLY the canonical market-state JSON produced by
AURA v0.5.3.12 — Market State Engine.

This layer deliberately does NOT:
- fetch market data;
- calculate indicators;
- calculate ATR;
- calculate EMA;
- resample candles;
- change thresholds;
- optimize parameters;
- place orders;
- perform paper execution;
- perform live execution.

Its sole responsibility is to turn the validated canonical market state into
a deterministic signal decision for the frozen research candidate.

Frozen candidate:
    BEAR x LOW ATR x POSITIVE bar-2

Decision policy:
    1. Canonical market state must be valid.
    2. The v0.5.3.12 state hash must verify against its canonical payload.
    3. The frozen configuration must match the locked research parameters.
    4. Each valid symbol is evaluated from the state supplied by v0.5.3.12.
    5. No indicator is recalculated here.
    6. If a symbol's frozen candidate match is True, that symbol receives
       SIGNAL_CANDIDATE.
    7. If the market state is valid but no symbol matches, the overall result
       is NO_SIGNAL.
    8. If the market state is invalid or its provenance/configuration cannot
       be verified, the result is BLOCKED.
    9. Any unexpected engine error fails closed.

Default input:
    regime_output/market_state/market_state_snapshot.json

Default output:
    regime_output/signal_decision/signal_decision.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any


VERSION = "AURA v0.5.3.13"

DEFAULT_INPUT = Path(
    r"regime_output\market_state\market_state_snapshot.json"
)
DEFAULT_OUTPUT = Path(
    r"regime_output\signal_decision\signal_decision.json"
)

SYMBOLS = ("BTC/USD", "ETH/USD")

# Locked research configuration. These values must agree with v0.5.3.12.
FROZEN_CANDIDATE = "BEAR x LOW ATR x POSITIVE bar-2"
FROZEN_ATR_THRESHOLD_PCT = 0.596
FROZEN_EMA_PERIOD_4H = 50
FROZEN_ATR_PERIOD_1H = 14
FROZEN_BAR_2_LAG_HOURS = 2


def stable_json(obj: Any) -> str:
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_snapshot(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Market-state input not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    if not isinstance(payload, dict):
        raise ValueError("Market-state input is not a JSON object.")

    return payload


def base_decision(input_path: Path) -> dict[str, Any]:
    return {
        "agent_version": VERSION,
        "engine": "SIGNAL_DECISION_ENGINE",
        "decision_status": "BLOCKED",
        "overall_decision": "BLOCKED",
        "generated_from": str(input_path.resolve()),
        "input_state_id": None,
        "input_state_hash": None,
        "input_state_hash_verified": False,
        "frozen_configuration_verified": False,
        "invalid_reasons": [],
        "decisions": {},
        "guardrails": {
            "single_source_of_truth": True,
            "source_engine": "AURA v0.5.3.12",
            "indicator_recalculation": False,
            "market_data_fetch": False,
            "lookahead_allowed": False,
            "orders_allowed": False,
            "paper_execution": False,
            "live_execution": False,
            "strategy_changed": False,
            "parameters_changed": False,
            "fail_closed": True,
        },
    }


def verify_frozen_configuration(snapshot: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    cfg = snapshot.get("frozen_configuration")
    if not isinstance(cfg, dict):
        return ["MISSING_FROZEN_CONFIGURATION"]

    expected = {
        "candidate": FROZEN_CANDIDATE,
        "atr_threshold_pct": FROZEN_ATR_THRESHOLD_PCT,
        "ema_period_4h": FROZEN_EMA_PERIOD_4H,
        "atr_period_1h": FROZEN_ATR_PERIOD_1H,
        "bar_2_lag_hours": FROZEN_BAR_2_LAG_HOURS,
    }

    for key, value in expected.items():
        if cfg.get(key) != value:
            errors.append(
                f"FROZEN_CONFIGURATION_MISMATCH:{key}:"
                f"expected={value!r}:actual={cfg.get(key)!r}"
            )

    return errors


def verify_state_hash(snapshot: dict[str, Any]) -> tuple[bool, list[str]]:
    errors: list[str] = []

    canonical = snapshot.get("canonical_state")
    supplied_hash = snapshot.get("state_hash")

    if not isinstance(canonical, dict):
        return False, ["MISSING_CANONICAL_STATE"]

    if not isinstance(supplied_hash, str) or not supplied_hash:
        return False, ["MISSING_STATE_HASH"]

    try:
        calculated_hash = sha256_text(stable_json(canonical))
    except (TypeError, ValueError) as exc:
        return False, [f"CANONICAL_STATE_NOT_HASHABLE:{type(exc).__name__}"]

    if calculated_hash != supplied_hash:
        errors.append("STATE_HASH_MISMATCH")
        return False, errors

    # Verify that state_id is deterministic for the same canonical state.
    symbols = canonical.get("symbols")
    if not isinstance(symbols, dict) or not symbols:
        errors.append("CANONICAL_SYMBOLS_MISSING")
        return False, errors

    timestamps: list[str] = []
    for symbol in sorted(symbols):
        item = symbols[symbol]
        if not isinstance(item, dict):
            errors.append(f"INVALID_CANONICAL_SYMBOL_ENTRY:{symbol}")
            continue

        state = item.get("market_state")
        if not isinstance(state, dict):
            errors.append(f"MISSING_MARKET_STATE:{symbol}")
            continue

        timestamp = state.get("timestamp")
        if not isinstance(timestamp, str) or not timestamp:
            errors.append(f"MISSING_STATE_TIMESTAMP:{symbol}")
            continue

        timestamps.append(f"{symbol}:{timestamp}")

    if errors:
        return False, sorted(set(errors))

    expected_state_id = f"MS-{sha256_text('|'.join(timestamps) + '|' + supplied_hash)[:24]}"
    if snapshot.get("state_id") != expected_state_id:
        errors.append("STATE_ID_MISMATCH")

    return len(errors) == 0, sorted(set(errors))


def validate_snapshot_guardrails(snapshot: dict[str, Any]) -> list[str]:
    errors: list[str] = []

    if snapshot.get("engine") != "MARKET_STATE_ENGINE":
        errors.append("WRONG_SOURCE_ENGINE")

    if snapshot.get("data_status") != "VALID":
        errors.append("SOURCE_DATA_STATUS_NOT_VALID")

    if snapshot.get("market_state_valid") is not True:
        errors.append("SOURCE_MARKET_STATE_NOT_VALID")

    guardrails = snapshot.get("guardrails")
    if not isinstance(guardrails, dict):
        errors.append("MISSING_SOURCE_GUARDRAILS")
        return sorted(set(errors))

    required_true = (
        "single_source_of_truth",
        "no_recalculation_downstream",
        "orders_allowed",
        "paper_execution",
        "live_execution",
        "strategy_changed",
        "parameters_changed",
    )

    # The first two must be True; execution/strategy-changing flags must be
    # False. Keep the checks explicit so a malformed source cannot silently
    # pass into the decision layer.
    if guardrails.get("single_source_of_truth") is not True:
        errors.append("SOURCE_SST_NOT_ENABLED")
    if guardrails.get("no_recalculation_downstream") is not True:
        errors.append("SOURCE_DOWNSTREAM_RECALCULATION_NOT_DISABLED")

    for key in required_true[2:]:
        if guardrails.get(key) is not False:
            errors.append(f"SOURCE_GUARDRAIL_VIOLATION:{key}")

    return sorted(set(errors))


def evaluate_symbol(symbol: str, item: dict[str, Any]) -> dict[str, Any]:
    """
    Evaluate a symbol WITHOUT recalculating indicators.

    The decision is based solely on fields already calculated by v0.5.3.12.
    """
    result: dict[str, Any] = {
        "symbol": symbol,
        "decision": "BLOCKED",
        "market_state_valid": False,
        "candidate_match": False,
        "state_timestamp": None,
        "state_id": None,
        "regime_state": None,
        "reason": None,
    }

    if not isinstance(item, dict):
        result["reason"] = "INVALID_SYMBOL_ENTRY"
        return result

    if item.get("data_status") != "VALID":
        result["reason"] = "SYMBOL_DATA_INVALID"
        return result

    if item.get("market_state_valid") is not True:
        result["reason"] = "SYMBOL_MARKET_STATE_INVALID"
        return result

    state = item.get("market_state")
    if not isinstance(state, dict):
        result["reason"] = "MISSING_MARKET_STATE"
        return result

    regime_state = state.get("regime_state")
    candidate_match = state.get("frozen_candidate_match")

    result["market_state_valid"] = True
    result["candidate_match"] = candidate_match is True
    result["state_timestamp"] = state.get("timestamp")
    result["regime_state"] = regime_state

    # The state itself must agree with the frozen candidate flag. This is a
    # consistency check, not an indicator recalculation.
    expected_match = regime_state == FROZEN_CANDIDATE

    if candidate_match is not expected_match:
        result["reason"] = "CANDIDATE_MATCH_INCONSISTENT"
        result["decision"] = "BLOCKED"
        return result

    if expected_match:
        result["decision"] = "SIGNAL_CANDIDATE"
        result["reason"] = "FROZEN_CANDIDATE_MATCH"
    else:
        result["decision"] = "NO_SIGNAL"
        result["reason"] = "FROZEN_CANDIDATE_NOT_PRESENT"

    return result


def process(snapshot: dict[str, Any], input_path: Path) -> dict[str, Any]:
    decision = base_decision(input_path)

    decision["input_state_id"] = snapshot.get("state_id")
    decision["input_state_hash"] = snapshot.get("state_hash")

    source_errors = validate_snapshot_guardrails(snapshot)
    if source_errors:
        decision["invalid_reasons"] = source_errors
        return decision

    config_errors = verify_frozen_configuration(snapshot)
    if config_errors:
        decision["invalid_reasons"] = config_errors
        return decision

    config_verified = True
    decision["frozen_configuration_verified"] = config_verified

    hash_verified, hash_errors = verify_state_hash(snapshot)
    decision["input_state_hash_verified"] = hash_verified

    if not hash_verified:
        decision["invalid_reasons"] = hash_errors
        return decision

    symbols = snapshot.get("symbols")
    if not isinstance(symbols, dict):
        decision["invalid_reasons"] = ["MISSING_SYMBOL_STATES"]
        return decision

    selected = [s for s in SYMBOLS if s in symbols]
    if not selected:
        decision["invalid_reasons"] = ["NO_REQUIRED_SYMBOL_STATES"]
        return decision

    # This engine expects both required assets from the canonical state. It
    # does not silently substitute a partial market view.
    missing = [s for s in SYMBOLS if s not in symbols]
    if missing:
        decision["invalid_reasons"] = [
            "MISSING_REQUIRED_SYMBOL:" + s for s in missing
        ]
        return decision

    signal_count = 0
    blocked_count = 0

    for symbol in SYMBOLS:
        result = evaluate_symbol(symbol, symbols[symbol])
        decision["decisions"][symbol] = result

        if result["decision"] == "SIGNAL_CANDIDATE":
            signal_count += 1
        elif result["decision"] == "BLOCKED":
            blocked_count += 1

    if blocked_count:
        decision["decision_status"] = "BLOCKED"
        decision["overall_decision"] = "BLOCKED"
        decision["invalid_reasons"] = [
            "ONE_OR_MORE_SYMBOL_DECISIONS_BLOCKED"
        ]
        return decision

    decision["decision_status"] = "DECIDED"

    if signal_count:
        decision["overall_decision"] = "SIGNAL_CANDIDATE"
    else:
        decision["overall_decision"] = "NO_SIGNAL"

    return decision


def print_report(decision: dict[str, Any], output_path: Path) -> None:
    print("=" * 96)
    print(f"{VERSION} — SIGNAL DECISION ENGINE")
    print("=" * 96)
    print()
    print("MODE                 : RESEARCH ONLY")
    print("ORDERS               : DISABLED")
    print("PAPER EXECUTION      : DISABLED")
    print("LIVE EXECUTION       : DISABLED")
    print()
    print(f"FROZEN CANDIDATE     : {FROZEN_CANDIDATE}")
    print(f"ATR THRESHOLD        : {FROZEN_ATR_THRESHOLD_PCT:.3f}%")
    print(f"4H EMA               : EMA{FROZEN_EMA_PERIOD_4H}")
    print(f"BAR-2                : {FROZEN_BAR_2_LAG_HOURS}H CLOSE-TO-CLOSE")
    print()
    print("SOURCE               : v0.5.3.12 MARKET STATE ENGINE")
    print("INDICATOR RECALC     : DISABLED")
    print("SINGLE SOURCE OF TRUTH: ENFORCED")
    print("FAIL-CLOSED POLICY   : ENABLED")
    print()

    print("DECISION")
    print("-" * 96)
    print(f"STATUS               : {decision['decision_status']}")
    print(f"OVERALL DECISION     : {decision['overall_decision']}")
    print(f"STATE ID             : {decision['input_state_id']}")
    print(f"STATE HASH VERIFIED  : {decision['input_state_hash_verified']}")
    print(f"CONFIG VERIFIED      : {decision['frozen_configuration_verified']}")
    print()

    for symbol in SYMBOLS:
        item = decision["decisions"].get(symbol)
        print(symbol)
        if not item:
            print("  DECISION           : BLOCKED")
            print("  REASON             : MISSING_DECISION")
            print()
            continue

        print(f"  DECISION           : {item['decision']}")
        print(f"  MARKET STATE VALID : {item['market_state_valid']}")
        print(f"  CANDIDATE MATCH    : {item['candidate_match']}")
        print(f"  TIMESTAMP          : {item['state_timestamp']}")
        print(f"  REGIME STATE       : {item['regime_state']}")
        print(f"  REASON             : {item['reason']}")
        print()

    if decision["invalid_reasons"]:
        print("FAIL-CLOSED REASONS")
        print("-" * 96)
        for reason in decision["invalid_reasons"]:
            print(f"  - {reason}")
        print()

    print(f"OUTPUT               : {output_path}")
    print("=" * 96)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AURA v0.5.3.13 canonical Signal Decision Engine"
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help="v0.5.3.12 canonical market-state JSON",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Signal-decision JSON output",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one deterministic decision cycle and exit",
    )
    args = parser.parse_args()

    # The flag is intentionally explicit for orchestration/smoke testing.
    _ = args.once

    try:
        snapshot = load_snapshot(args.input)
        decision = process(snapshot, args.input)

        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(
                decision,
                indent=2,
                ensure_ascii=False,
                allow_nan=False,
            ),
            encoding="utf-8",
        )

        print_report(decision, args.output)

        return 0 if decision["decision_status"] == "DECIDED" else 2

    except Exception as exc:
        print()
        print("ENGINE ERROR")
        print("-" * 96)
        print(f"{type(exc).__name__}: {exc}")
        print("FAIL-CLOSED: no signal decision was published as actionable.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
