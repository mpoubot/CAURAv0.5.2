#!/usr/bin/env python3
"""
AURA v0.5.3.14 — Risk Gate

LOCKED ARCHITECTURE
-------------------
Consumes ONLY the canonical decision output produced by
AURA v0.5.3.13 — Signal Decision Engine.

This layer deliberately does NOT:
- fetch market data;
- calculate ATR;
- calculate EMA;
- resample candles;
- recalculate bar-2;
- create or modify signals;
- choose position size;
- create positions;
- place orders;
- perform paper execution;
- perform live execution.

Its sole responsibility is to determine whether an already-produced signal
candidate is allowed to pass the risk/integrity gate.

Decision semantics:
    SIGNAL_CANDIDATE + all risk/integrity checks pass
        -> RISK_PASS

    valid state + NO_SIGNAL
        -> NO_SIGNAL

    invalid, unverified, stale/inconsistent, or blocked upstream state
        -> BLOCKED

Important:
    RISK_PASS means "permitted to continue to the Position State Manager".
    It does NOT mean "an order may be sent".
    Execution remains permanently disabled in this build.

Default input:
    regime_output/signal_decision/signal_decision.json

Default output:
    regime_output/risk_gate/risk_gate.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


VERSION = "AURA v0.5.3.14"

DEFAULT_INPUT = Path(
    r"regime_output\signal_decision\signal_decision.json"
)
DEFAULT_OUTPUT = Path(
    r"regime_output\risk_gate\risk_gate.json"
)

SYMBOLS = ("BTC/USD", "ETH/USD")

# Locked research configuration inherited from v0.5.3.12 / v0.5.3.13.
FROZEN_CANDIDATE = "BEAR x LOW ATR x POSITIVE bar-2"
FROZEN_ATR_THRESHOLD_PCT = 0.596
FROZEN_EMA_PERIOD_4H = 50
FROZEN_ATR_PERIOD_1H = 14
FROZEN_BAR_2_LAG_HOURS = 2

EXPECTED_SOURCE_ENGINE = "SIGNAL_DECISION_ENGINE"
EXPECTED_SOURCE_VERSION = "AURA v0.5.3.13"


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


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    if not isinstance(payload, dict):
        raise ValueError("Input JSON must contain an object at the top level.")

    return payload


def base_result(input_path: Path) -> dict[str, Any]:
    return {
        "agent_version": VERSION,
        "engine": "RISK_GATE",
        "decision_status": "BLOCKED",
        "overall_risk_decision": "BLOCKED",
        "risk_authorized": False,
        "execution_permitted": False,
        "generated_from": str(input_path.resolve()),
        "input_decision_hash": None,
        "input_decision_hash_verified": False,
        "upstream_state_id": None,
        "upstream_state_hash_verified": False,
        "frozen_configuration_verified": False,
        "risk_checks": {},
        "decisions": {},
        "blocked_reasons": [],
        "guardrails": {
            "single_source_of_truth": True,
            "source_engine": EXPECTED_SOURCE_VERSION,
            "indicator_recalculation": False,
            "market_data_fetch": False,
            "signal_recalculation": False,
            "position_sizing": False,
            "position_creation": False,
            "orders_allowed": False,
            "paper_execution": False,
            "live_execution": False,
            "fail_closed": True,
        },
    }


def verify_decision_integrity(
    decision: dict[str, Any],
) -> tuple[bool, list[str]]:
    """
    Verify the decision document itself.

    The decision JSON is treated as the signed-like handoff between
    v0.5.3.13 and v0.5.3.14. No market indicator is recalculated.
    """
    errors: list[str] = []

    if decision.get("engine") != EXPECTED_SOURCE_ENGINE:
        errors.append("WRONG_UPSTREAM_ENGINE")

    if decision.get("agent_version") != EXPECTED_SOURCE_VERSION:
        errors.append("WRONG_UPSTREAM_VERSION")

    if decision.get("decision_status") not in {"DECIDED", "BLOCKED"}:
        errors.append("INVALID_UPSTREAM_DECISION_STATUS")

    if decision.get("upstream_state_id") is None:
        # v0.5.3.13 calls this input_state_id.
        if decision.get("input_state_id") is None:
            errors.append("MISSING_UPSTREAM_STATE_ID")

    if not isinstance(decision.get("input_state_hash_verified"), bool):
        errors.append("MISSING_STATE_HASH_VERIFICATION_FLAG")
    elif decision.get("input_state_hash_verified") is not True:
        errors.append("UPSTREAM_STATE_HASH_NOT_VERIFIED")

    if decision.get("frozen_configuration_verified") is not True:
        errors.append("UPSTREAM_CONFIGURATION_NOT_VERIFIED")

    guardrails = decision.get("guardrails")
    if not isinstance(guardrails, dict):
        errors.append("MISSING_UPSTREAM_GUARDRAILS")
    else:
        if guardrails.get("single_source_of_truth") is not True:
            errors.append("UPSTREAM_SST_NOT_ENFORCED")
        if guardrails.get("indicator_recalculation") is not False:
            errors.append("UPSTREAM_INDICATOR_RECALCULATION_ENABLED")
        if guardrails.get("market_data_fetch") is not False:
            errors.append("UPSTREAM_MARKET_DATA_FETCH_ENABLED")
        if guardrails.get("orders_allowed") is not False:
            errors.append("UPSTREAM_ORDERS_NOT_DISABLED")
        if guardrails.get("paper_execution") is not False:
            errors.append("UPSTREAM_PAPER_EXECUTION_NOT_DISABLED")
        if guardrails.get("live_execution") is not False:
            errors.append("UPSTREAM_LIVE_EXECUTION_NOT_DISABLED")

    return len(errors) == 0, sorted(set(errors))


def canonical_decision_hash(decision: dict[str, Any]) -> str:
    """
    Deterministic integrity fingerprint of the v0.5.3.13 decision payload.

    Metadata fields that v0.5.3.14 itself creates are excluded by hashing only
    the upstream decision object supplied to this module.
    """
    return sha256_text(stable_json(decision))


def verify_symbol_set(decision: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    decisions = decision.get("decisions")

    if not isinstance(decisions, dict):
        return ["MISSING_UPSTREAM_DECISIONS"]

    for symbol in SYMBOLS:
        if symbol not in decisions:
            errors.append(f"MISSING_REQUIRED_SYMBOL:{symbol}")

    return errors


def verify_timestamp_synchronization(
    decision: dict[str, Any],
) -> tuple[bool, list[str], str | None]:
    """
    Require the two required symbol decisions to reference the same canonical
    market-state timestamp.

    This is a provenance/synchronization check only. No timestamp is created
    or transformed here.
    """
    errors: list[str] = []
    decisions = decision.get("decisions")

    if not isinstance(decisions, dict):
        return False, ["MISSING_UPSTREAM_DECISIONS"], None

    timestamps: dict[str, str] = {}

    for symbol in SYMBOLS:
        item = decisions.get(symbol)
        if not isinstance(item, dict):
            errors.append(f"INVALID_SYMBOL_DECISION:{symbol}")
            continue

        ts = item.get("state_timestamp")
        if not isinstance(ts, str) or not ts:
            errors.append(f"MISSING_STATE_TIMESTAMP:{symbol}")
        else:
            timestamps[symbol] = ts

    if errors:
        return False, sorted(set(errors)), None

    unique = set(timestamps.values())
    if len(unique) != 1:
        errors.append("SYMBOL_STATE_TIMESTAMPS_NOT_SYNCHRONIZED")
        return False, errors, None

    return True, [], next(iter(unique))


def verify_candidate_consistency(
    decision: dict[str, Any],
) -> tuple[bool, list[str]]:
    """
    Check that SIGNAL_CANDIDATE means exactly what v0.5.3.13 says it means.

    No indicator is recomputed.
    """
    errors: list[str] = []
    decisions = decision.get("decisions")

    if not isinstance(decisions, dict):
        return False, ["MISSING_UPSTREAM_DECISIONS"]

    for symbol in SYMBOLS:
        item = decisions.get(symbol)
        if not isinstance(item, dict):
            errors.append(f"INVALID_SYMBOL_DECISION:{symbol}")
            continue

        d = item.get("decision")
        match = item.get("candidate_match")
        reason = item.get("reason")

        if d == "SIGNAL_CANDIDATE":
            if match is not True:
                errors.append(
                    f"CANDIDATE_MATCH_FLAG_INCONSISTENT:{symbol}"
                )
            if reason != "FROZEN_CANDIDATE_MATCH":
                errors.append(
                    f"CANDIDATE_REASON_INCONSISTENT:{symbol}"
                )

        elif d == "NO_SIGNAL":
            if match is not False:
                errors.append(
                    f"NO_SIGNAL_MATCH_FLAG_INCONSISTENT:{symbol}"
                )
            if reason != "FROZEN_CANDIDATE_NOT_PRESENT":
                errors.append(
                    f"NO_SIGNAL_REASON_INCONSISTENT:{symbol}"
                )

        elif d == "BLOCKED":
            # A blocked upstream symbol is never promoted by the Risk Gate.
            if match is True:
                errors.append(
                    f"BLOCKED_SYMBOL_CLAIMS_MATCH:{symbol}"
                )

        else:
            errors.append(f"UNKNOWN_SYMBOL_DECISION:{symbol}:{d!r}")

    return len(errors) == 0, sorted(set(errors))


def evaluate_risk_gate(
    decision: dict[str, Any],
    input_path: Path,
) -> dict[str, Any]:
    result = base_result(input_path)

    result["input_decision_hash"] = canonical_decision_hash(decision)

    # Copy the upstream state identity without changing it.
    result["upstream_state_id"] = decision.get("input_state_id")
    result["upstream_state_hash_verified"] = (
        decision.get("input_state_hash_verified") is True
    )
    result["frozen_configuration_verified"] = (
        decision.get("frozen_configuration_verified") is True
    )

    integrity_ok, integrity_errors = verify_decision_integrity(decision)
    result["risk_checks"]["upstream_integrity"] = {
        "passed": integrity_ok,
        "reasons": integrity_errors,
    }

    symbol_errors = verify_symbol_set(decision)
    symbols_ok = not symbol_errors
    result["risk_checks"]["required_symbols"] = {
        "passed": symbols_ok,
        "reasons": symbol_errors,
    }

    sync_ok, sync_errors, canonical_timestamp = (
        verify_timestamp_synchronization(decision)
    )
    result["risk_checks"]["timestamp_synchronization"] = {
        "passed": sync_ok,
        "canonical_timestamp": canonical_timestamp,
        "reasons": sync_errors,
    }

    consistency_ok, consistency_errors = verify_candidate_consistency(
        decision
    )
    result["risk_checks"]["candidate_consistency"] = {
        "passed": consistency_ok,
        "reasons": consistency_errors,
    }

    # Explicit research-only execution guard. This must remain false even
    # when every risk check passes.
    result["risk_checks"]["execution_disabled"] = {
        "passed": True,
        "orders_allowed": False,
        "paper_execution": False,
        "live_execution": False,
    }

    blocked_reasons: list[str] = []
    blocked_reasons.extend(integrity_errors)
    blocked_reasons.extend(symbol_errors)
    blocked_reasons.extend(sync_errors)
    blocked_reasons.extend(consistency_errors)

    if blocked_reasons:
        result["blocked_reasons"] = sorted(set(blocked_reasons))

        for symbol in SYMBOLS:
            upstream_item = decision.get("decisions", {}).get(symbol)
            if isinstance(upstream_item, dict):
                result["decisions"][symbol] = {
                    "symbol": symbol,
                    "upstream_decision": upstream_item.get("decision"),
                    "risk_decision": "BLOCKED",
                    "risk_authorized": False,
                    "reason": "UPSTREAM_OR_INTEGRITY_CHECK_FAILED",
                }
            else:
                result["decisions"][symbol] = {
                    "symbol": symbol,
                    "upstream_decision": None,
                    "risk_decision": "BLOCKED",
                    "risk_authorized": False,
                    "reason": "MISSING_UPSTREAM_DECISION",
                }

        result["decision_status"] = "BLOCKED"
        result["overall_risk_decision"] = "BLOCKED"
        result["risk_authorized"] = False
        result["execution_permitted"] = False
        return result

    signal_count = 0
    blocked_count = 0

    upstream_decisions = decision["decisions"]

    for symbol in SYMBOLS:
        item = upstream_decisions[symbol]
        upstream = item.get("decision")

        if upstream == "SIGNAL_CANDIDATE":
            risk_decision = "RISK_PASS"
            authorized = True
            reason = "ALL_RISK_AND_INTEGRITY_CHECKS_PASSED"
            signal_count += 1

        elif upstream == "NO_SIGNAL":
            risk_decision = "NO_SIGNAL"
            authorized = False
            reason = "NO_UPSTREAM_SIGNAL_CANDIDATE"

        else:
            risk_decision = "BLOCKED"
            authorized = False
            reason = "UNEXPECTED_UPSTREAM_DECISION"
            blocked_count += 1

        result["decisions"][symbol] = {
            "symbol": symbol,
            "upstream_decision": upstream,
            "risk_decision": risk_decision,
            "risk_authorized": authorized,
            "reason": reason,
        }

    if blocked_count:
        result["decision_status"] = "BLOCKED"
        result["overall_risk_decision"] = "BLOCKED"
        result["risk_authorized"] = False
        result["execution_permitted"] = False
        result["blocked_reasons"] = ["UNEXPECTED_UPSTREAM_DECISION"]
        return result

    result["decision_status"] = "DECIDED"

    if signal_count:
        result["overall_risk_decision"] = "RISK_PASS"
        result["risk_authorized"] = True
    else:
        result["overall_risk_decision"] = "NO_SIGNAL"
        result["risk_authorized"] = False

    # This is intentionally hard-coded false in the research build.
    result["execution_permitted"] = False

    return result


def print_report(result: dict[str, Any], output_path: Path) -> None:
    print("=" * 96)
    print(f"{VERSION} — RISK GATE")
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
    print("SOURCE               : v0.5.3.13 SIGNAL DECISION ENGINE")
    print("INDICATOR RECALC     : DISABLED")
    print("MARKET DATA FETCH    : DISABLED")
    print("POSITION SIZING      : DISABLED")
    print("POSITION CREATION    : DISABLED")
    print("FAIL-CLOSED POLICY   : ENABLED")
    print()
    print("RISK GATE")
    print("-" * 96)
    print(f"STATUS               : {result['decision_status']}")
    print(f"OVERALL RISK DECISION: {result['overall_risk_decision']}")
    print(f"RISK AUTHORIZED      : {result['risk_authorized']}")
    print(f"EXECUTION PERMITTED  : {result['execution_permitted']}")
    print(f"UPSTREAM STATE ID    : {result['upstream_state_id']}")
    print(
        f"UPSTREAM HASH VERIFIED: "
        f"{result['upstream_state_hash_verified']}"
    )
    print(
        f"CONFIG VERIFIED      : "
        f"{result['frozen_configuration_verified']}"
    )
    print()

    for symbol in SYMBOLS:
        item = result["decisions"].get(symbol)
        print(symbol)
        if not item:
            print("  RISK DECISION      : BLOCKED")
            print("  RISK AUTHORIZED    : False")
            print("  REASON             : MISSING_DECISION")
            print()
            continue

        print(f"  UPSTREAM DECISION  : {item['upstream_decision']}")
        print(f"  RISK DECISION      : {item['risk_decision']}")
        print(f"  RISK AUTHORIZED    : {item['risk_authorized']}")
        print(f"  REASON             : {item['reason']}")
        print()

    failed_checks = [
        name
        for name, check in result["risk_checks"].items()
        if check.get("passed") is False
    ]

    if failed_checks:
        print("FAILED RISK CHECKS")
        print("-" * 96)
        for name in failed_checks:
            print(f"  - {name}")
        print()

    if result["blocked_reasons"]:
        print("FAIL-CLOSED REASONS")
        print("-" * 96)
        for reason in result["blocked_reasons"]:
            print(f"  - {reason}")
        print()

    print(f"OUTPUT               : {output_path}")
    print("=" * 96)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AURA v0.5.3.14 fail-closed Risk Gate"
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help="v0.5.3.13 signal-decision JSON",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Risk-gate JSON output",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one deterministic gate cycle and exit",
    )
    args = parser.parse_args()

    _ = args.once

    try:
        decision = load_json(args.input)
        result = evaluate_risk_gate(decision, args.input)

        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(
                result,
                indent=2,
                ensure_ascii=False,
                allow_nan=False,
            ),
            encoding="utf-8",
        )

        print_report(result, args.output)

        # A blocked gate is a valid, intentional fail-closed outcome.
        # Return 0 for both DECIDED and BLOCKED so orchestration can inspect
        # the JSON result rather than treating a safe block as a crash.
        return 0

    except Exception as exc:
        print()
        print("ENGINE ERROR")
        print("-" * 96)
        print(f"{type(exc).__name__}: {exc}")
        print("FAIL-CLOSED: no risk authorization was published.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
