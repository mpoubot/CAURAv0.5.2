#!/usr/bin/env python3
"""
AURA v0.5.3.17 — Execution & Decision Ledger

Schema-grounded to the actual AURA v0.5.3.12-v0.5.3.16 artifacts.

This module is AUDIT ONLY. It does not calculate market state, create signals,
make risk decisions, manage positions, or execute orders.

Actual upstream files:
  v0.5.3.12 -> regime_output/market_state/market_state_snapshot.json
  v0.5.3.13 -> regime_output/signal_decision/signal_decision.json
  v0.5.3.14 -> regime_output/risk_gate/risk_gate.json
  v0.5.3.15 -> regime_output/position_state/position_state.json
  v0.5.3.16 -> regime_output/paper_execution/paper_execution.json

The ledger records the supplied upstream artifacts and their declared hashes.
It does not invent a new hash algorithm for upstream modules.

Safety:
  real orders       = disabled
  live execution    = disabled
  paper execution   = record-only
  risk decision     = disabled
  market data       = disabled
  fail closed       = enabled
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


VERSION = "AURA v0.5.3.17"
ENGINE = "EXECUTION_DECISION_LEDGER"

EXPECTED_VERSIONS = {
    "market_state": "AURA v0.5.3.12",
    "signal_decision": "AURA v0.5.3.13",
    "risk_gate": "AURA v0.5.3.14",
    "position_state": "AURA v0.5.3.15",
    "paper_execution": "AURA v0.5.3.16",
}

DEFAULT_MARKET_STATE = Path(r"regime_output\market_state\market_state_snapshot.json")
DEFAULT_SIGNAL_DECISION = Path(r"regime_output\signal_decision\signal_decision.json")
DEFAULT_RISK_GATE = Path(r"regime_output\risk_gate\risk_gate.json")
DEFAULT_POSITION_STATE = Path(r"regime_output\position_state\position_state.json")
DEFAULT_PAPER_EXECUTION = Path(r"regime_output\paper_execution\paper_execution.json")
DEFAULT_OUTPUT = Path(r"regime_output\ledger\execution_decision_ledger.json")

SYMBOLS = ("BTC/USD", "ETH/USD")


def utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"Top-level JSON object required: {path}")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )


def declared_version(payload: dict[str, Any]) -> str | None:
    value = payload.get("agent_version")
    return value if isinstance(value, str) else None


def symbol_map(payload: dict[str, Any], container: str) -> dict[str, Any]:
    value = payload.get(container, {})
    return value if isinstance(value, dict) else {}


def upstream_snapshot(
    market: dict[str, Any],
    signal: dict[str, Any],
    risk: dict[str, Any],
    position: dict[str, Any],
    paper: dict[str, Any],
) -> dict[str, Any]:
    """
    Capture the actual schema-bearing fields. Full upstream objects are retained
    under raw_upstream so the ledger never loses audit context.
    """
    return {
        "market_state": {
            "agent_version": market.get("agent_version"),
            "engine": market.get("engine"),
            "state_id": market.get("state_id"),
            "state_hash": market.get("state_hash"),
            "generated_at": market.get("generated_at"),
            "data_status": market.get("data_status"),
            "market_state_valid": market.get("market_state_valid"),
            "guardrails": market.get("guardrails", {}),
        },
        "signal_decision": {
            "agent_version": signal.get("agent_version"),
            "engine": signal.get("engine"),
            "decision_status": signal.get("decision_status"),
            "overall_decision": signal.get("overall_decision"),
            "generated_from": signal.get("generated_from"),
            "input_state_id": signal.get("input_state_id"),
            "input_state_hash": signal.get("input_state_hash"),
            "input_state_hash_verified": signal.get("input_state_hash_verified"),
            "frozen_configuration_verified": signal.get(
                "frozen_configuration_verified"
            ),
            "invalid_reasons": signal.get("invalid_reasons", []),
            "guardrails": signal.get("guardrails", {}),
        },
        "risk_gate": {
            "agent_version": risk.get("agent_version"),
            "engine": risk.get("engine"),
            "decision_status": risk.get("decision_status"),
            "overall_risk_decision": risk.get("overall_risk_decision"),
            "risk_authorized": risk.get("risk_authorized"),
            "execution_permitted": risk.get("execution_permitted"),
            "generated_from": risk.get("generated_from"),
            "input_decision_hash": risk.get("input_decision_hash"),
            "input_decision_hash_verified": risk.get("input_decision_hash_verified"),
            "upstream_state_id": risk.get("upstream_state_id"),
            "upstream_state_hash_verified": risk.get("upstream_state_hash_verified"),
            "frozen_configuration_verified": risk.get(
                "frozen_configuration_verified"
            ),
            "blocked_reasons": risk.get("blocked_reasons", []),
            "guardrails": risk.get("guardrails", {}),
        },
        "position_state": {
            "agent_version": position.get("agent_version"),
            "engine": position.get("engine"),
            "state_version": position.get("state_version"),
            "generated_at": position.get("generated_at"),
            "decision_status": position.get("decision_status"),
            "overall_action": position.get("overall_action"),
            "execution_permitted": position.get("execution_permitted"),
            "orders_allowed": position.get("orders_allowed"),
            "paper_execution": position.get("paper_execution"),
            "live_execution": position.get("live_execution"),
            "source_engine": position.get("source_engine"),
            "source_input": position.get("source_input"),
            "input_risk_gate_hash": position.get("input_risk_gate_hash"),
            "state_hash": position.get("state_hash"),
            "idempotency": position.get("idempotency", {}),
            "guardrails": position.get("guardrails", {}),
        },
        "paper_execution": {
            "agent_version": paper.get("agent_version"),
            "engine": paper.get("engine"),
            "state_version": paper.get("state_version"),
            "generated_at": paper.get("generated_at"),
            "decision_status": paper.get("decision_status"),
            "overall_action": paper.get("overall_action"),
            "paper_execution": paper.get("paper_execution"),
            "orders_allowed": paper.get("orders_allowed"),
            "real_order_submission": paper.get("real_order_submission"),
            "live_execution": paper.get("live_execution"),
            "source_engine": paper.get("source_engine"),
            "source_input": paper.get("source_input"),
            "input_state_hash": paper.get("input_state_hash"),
            "execution_model": paper.get("execution_model", {}),
            "result_hash": paper.get("result_hash"),
            "guardrails": paper.get("guardrails", {}),
        },
    }


def chain_checks(
    market: dict[str, Any],
    signal: dict[str, Any],
    risk: dict[str, Any],
    position: dict[str, Any],
    paper: dict[str, Any],
) -> list[str]:
    errors: list[str] = []

    artifacts = {
        "market_state": market,
        "signal_decision": signal,
        "risk_gate": risk,
        "position_state": position,
        "paper_execution": paper,
    }

    for name, payload in artifacts.items():
        expected = EXPECTED_VERSIONS[name]
        if declared_version(payload) != expected:
            errors.append(
                f"{name.upper()}_VERSION_MISMATCH:{declared_version(payload)}"
            )

    # Explicit cross-artifact identity relationships from the real schemas.
    if signal.get("input_state_id") != market.get("state_id"):
        errors.append("SIGNAL_MARKET_STATE_ID_MISMATCH")

    if signal.get("input_state_hash") != market.get("state_hash"):
        errors.append("SIGNAL_MARKET_STATE_HASH_MISMATCH")

    # v0.5.3.14 records the v0.5.3.12 state ID as its upstream state identity.
    if risk.get("upstream_state_id") != market.get("state_id"):
        errors.append("RISK_MARKET_STATE_ID_MISMATCH")

    # The existing upstream schema stores source_engine as the upstream
    # AURA VERSION string, not the upstream engine identifier.
    if position.get("source_engine") != EXPECTED_VERSIONS["risk_gate"]:
        errors.append("POSITION_RISK_SOURCE_VERSION_MISMATCH")

    if paper.get("source_engine") != EXPECTED_VERSIONS["position_state"]:
        errors.append("PAPER_POSITION_SOURCE_VERSION_MISMATCH")

    # Execution safety invariants.
    if position.get("orders_allowed") is not False:
        errors.append("POSITION_ORDERS_NOT_DISABLED")
    if position.get("live_execution") is not False:
        errors.append("POSITION_LIVE_EXECUTION_NOT_DISABLED")
    if paper.get("paper_execution") is not True:
        errors.append("PAPER_EXECUTION_FLAG_NOT_TRUE")
    if paper.get("real_order_submission") is not False:
        errors.append("PAPER_REAL_ORDER_SUBMISSION_NOT_DISABLED")
    if paper.get("live_execution") is not False:
        errors.append("PAPER_LIVE_EXECUTION_NOT_DISABLED")

    # IMPORTANT: upstream BLOCKED/integrity-failure states are legitimate
    # audit events and must still be recorded. We therefore do NOT turn the
    # upstream verification flags into a second risk gate here. Their exact
    # values are captured in upstream_declarations and raw_upstream.
    return sorted(set(errors))


def build_record(
    symbol: str,
    sequence: int,
    timestamp: str,
    previous_record_hash: str | None,
    market: dict[str, Any],
    signal: dict[str, Any],
    risk: dict[str, Any],
    position: dict[str, Any],
    paper: dict[str, Any],
) -> dict[str, Any]:
    market_item = symbol_map(market, "symbols").get(symbol, {})
    signal_item = symbol_map(signal, "decisions").get(symbol, {})
    risk_item = symbol_map(risk, "decisions").get(symbol, {})
    position_item = symbol_map(position, "positions").get(symbol, {})
    position_decision = symbol_map(position, "decisions").get(symbol, {})

    record = {
        "event_id": None,
        "sequence": sequence,
        "timestamp": timestamp,
        "agent_version": VERSION,
        "symbol": symbol,
        "outcome": {
            "signal_decision": signal_item.get("decision"),
            "risk_decision": risk_item.get("risk_decision"),
            "risk_authorized": risk_item.get("risk_authorized"),
            "position_state": position_item.get("position_state"),
            "position_action": position_decision.get("action"),
            "paper_execution_status": paper.get("decision_status"),
            "paper_execution_action": paper.get("overall_action"),
            "paper_orders": [
                item for item in paper.get("orders", [])
                if isinstance(item, dict) and item.get("symbol") == symbol
            ],
            "execution_events": [
                item for item in paper.get("execution_events", [])
                if isinstance(item, dict) and item.get("symbol") == symbol
            ],
        },
        "decision_context": {
            "market_state": market_item,
            "signal_decision": signal_item,
            "risk_gate": risk_item,
            "position": position_item,
            "position_decision": position_decision,
        },
        "upstream_declarations": upstream_snapshot(
            market, signal, risk, position, paper
        ),
        "raw_upstream": {
            "market_state": market,
            "signal_decision": signal,
            "risk_gate": risk,
            "position_state": position,
            "paper_execution": paper,
        },
        "guardrails": {
            "real_orders": False,
            "live_execution": False,
            "paper_execution": "record_only",
            "risk_decision": False,
            "market_data_fetch": False,
            "fail_closed": True,
        },
        "previous_record_hash": previous_record_hash,
    }

    identity = {
        "sequence": sequence,
        "timestamp": timestamp,
        "symbol": symbol,
        "market_state_hash": market.get("state_hash"),
        "signal_input_hash": signal.get("input_state_hash"),
        "risk_input_hash": risk.get("input_decision_hash"),
        "position_state_hash": position.get("state_hash"),
        "paper_result_hash": paper.get("result_hash"),
        "previous_record_hash": previous_record_hash,
    }
    record["event_id"] = "LEDGER-" + sha256(identity)[:24]

    record_copy = dict(record)
    record_copy["record_hash"] = None
    record["record_hash"] = sha256(record_copy)
    return record


def new_ledger() -> dict[str, Any]:
    return {
        "agent_version": VERSION,
        "engine": ENGINE,
        "ledger_version": 1,
        "generated_at": utc_now_iso(),
        "append_only": True,
        "records": [],
        "last_record_hash": None,
        "ledger_hash": None,
        "guardrails": {
            "real_orders": False,
            "live_execution": False,
            "paper_execution": "record_only",
            "risk_decision": False,
            "market_data_fetch": False,
            "fail_closed": True,
        },
    }


def load_ledger(path: Path) -> dict[str, Any]:
    if not path.exists():
        return new_ledger()
    value = load_json(path)
    if value.get("engine") not in (None, ENGINE):
        raise ValueError("INCOMPATIBLE_LEDGER_ENGINE")
    if not isinstance(value.get("records"), list):
        raise ValueError("INVALID_LEDGER_RECORDS")
    return value


def save_ledger(path: Path, ledger: dict[str, Any]) -> None:
    ledger_copy = dict(ledger)
    ledger_copy["ledger_hash"] = None
    ledger["ledger_hash"] = sha256(ledger_copy)
    write_json(path, ledger)


def run(
    market: dict[str, Any],
    signal: dict[str, Any],
    risk: dict[str, Any],
    position: dict[str, Any],
    paper: dict[str, Any],
    ledger: dict[str, Any],
    timestamp: str,
    selected_symbol: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    errors = chain_checks(market, signal, risk, position, paper)

    if errors:
        return {
            "agent_version": VERSION,
            "engine": ENGINE,
            "decision_status": "BLOCKED",
            "overall_action": "NO_ACTION",
            "records_appended": 0,
            "blocked_reasons": errors,
            "ledger_hash": ledger.get("ledger_hash"),
            "guardrails": ledger["guardrails"],
        }, ledger

    symbols = [selected_symbol] if selected_symbol else list(SYMBOLS)

    previous = ledger.get("last_record_hash")
    start_sequence = len(ledger.get("records", [])) + 1
    new_records = []

    for offset, symbol in enumerate(symbols):
        record = build_record(
            symbol=symbol,
            sequence=start_sequence + offset,
            timestamp=timestamp,
            previous_record_hash=previous,
            market=market,
            signal=signal,
            risk=risk,
            position=position,
            paper=paper,
        )
        new_records.append(record)
        previous = record["record_hash"]

    existing_ids = {
        item.get("event_id")
        for item in ledger["records"]
        if isinstance(item, dict)
    }

    appended = 0
    for record in new_records:
        if record["event_id"] in existing_ids:
            continue
        ledger["records"].append(record)
        existing_ids.add(record["event_id"])
        appended += 1

    ledger["last_record_hash"] = (
        ledger["records"][-1]["record_hash"]
        if ledger["records"]
        else None
    )
    save_ledger(DEFAULT_OUTPUT if False else Path("regime_output/ledger/execution_decision_ledger.json"), ledger)

    return {
        "agent_version": VERSION,
        "engine": ENGINE,
        "decision_status": "RECORDED",
        "overall_action": (
            "PAPER_FILL"
            if any(r["outcome"]["paper_execution_action"] == "PAPER_FILL"
                   for r in new_records)
            else "NO_ACTION"
        ),
        "records_appended": appended,
        "blocked_reasons": [],
        "ledger_hash": ledger.get("ledger_hash"),
        "guardrails": ledger["guardrails"],
    }, ledger


def print_report(result: dict[str, Any], ledger: dict[str, Any], output: Path) -> None:
    print("=" * 100)
    print(f"{VERSION} — EXECUTION & DECISION LEDGER")
    print("=" * 100)
    print()
    print("MODE                 : AUDIT / LEDGER ONLY")
    print("REAL ORDERS          : DISABLED")
    print("LIVE EXECUTION       : DISABLED")
    print("PAPER EXECUTION      : RECORD ONLY")
    print("RISK DECISION        : DISABLED")
    print("MARKET DATA FETCH    : DISABLED")
    print("FAIL-CLOSED POLICY   : ENABLED")
    print()
    print("SOURCE CHAIN")
    print("-" * 100)
    for key, version in EXPECTED_VERSIONS.items():
        print(f"{key.upper():21}: {version}")
    print()
    print("LEDGER")
    print("-" * 100)
    print(f"STATUS               : {result['decision_status']}")
    print(f"OVERALL ACTION       : {result['overall_action']}")
    print(f"RECORDS APPENDED     : {result['records_appended']}")
    print(f"TOTAL RECORDS        : {len(ledger['records'])}")
    print(f"LEDGER HASH          : {ledger.get('ledger_hash')}")
    if result["blocked_reasons"]:
        print()
        print("BLOCKED REASONS")
        print("-" * 100)
        for reason in result["blocked_reasons"]:
            print(f"  - {reason}")
    print()
    print(f"OUTPUT               : {output}")
    print("=" * 100)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--market-state", type=Path, default=DEFAULT_MARKET_STATE)
    parser.add_argument("--signal-decision", type=Path, default=DEFAULT_SIGNAL_DECISION)
    parser.add_argument("--risk-gate", type=Path, default=DEFAULT_RISK_GATE)
    parser.add_argument("--position-state", type=Path, default=DEFAULT_POSITION_STATE)
    parser.add_argument("--paper-execution", type=Path, default=DEFAULT_PAPER_EXECUTION)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--symbol", choices=SYMBOLS, default=None)
    parser.add_argument("--timestamp", default=None)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    try:
        timestamp = args.timestamp or utc_now_iso()
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("TIMESTAMP_MUST_BE_TIMEZONE_AWARE")
        timestamp = (
            dt.astimezone(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )

        market = load_json(args.market_state)
        signal = load_json(args.signal_decision)
        risk = load_json(args.risk_gate)
        position = load_json(args.position_state)
        paper = load_json(args.paper_execution)

        # Do not append to an existing ledger containing records produced by an
        # earlier incompatible implementation. This avoids corrupting the
        # append-only audit history.
        ledger = load_ledger(args.output)

        if ledger.get("records"):
            first = ledger["records"][0]
            if "upstream_declarations" not in first:
                print("ENGINE ERROR")
                print("-" * 100)
                print("EXISTING LEDGER FORMAT IS FROM AN EARLIER BUILD")
                print("FAIL-CLOSED: existing ledger was not modified.")
                return 1

        errors = chain_checks(market, signal, risk, position, paper)
        if errors:
            result = {
                "agent_version": VERSION,
                "engine": ENGINE,
                "decision_status": "BLOCKED",
                "overall_action": "NO_ACTION",
                "records_appended": 0,
                "blocked_reasons": errors,
                "ledger_hash": ledger.get("ledger_hash"),
                "guardrails": ledger["guardrails"],
            }
        else:
            symbols = [args.symbol] if args.symbol else list(SYMBOLS)
            previous = ledger.get("last_record_hash")
            start_sequence = len(ledger["records"]) + 1
            records = []
            for offset, symbol in enumerate(symbols):
                record = build_record(
                    symbol=symbol,
                    sequence=start_sequence + offset,
                    timestamp=timestamp,
                    previous_record_hash=previous,
                    market=market,
                    signal=signal,
                    risk=risk,
                    position=position,
                    paper=paper,
                )
                records.append(record)
                previous = record["record_hash"]

            existing_ids = {
                item.get("event_id")
                for item in ledger["records"]
                if isinstance(item, dict)
            }
            appended = 0
            for record in records:
                if record["event_id"] not in existing_ids:
                    ledger["records"].append(record)
                    existing_ids.add(record["event_id"])
                    appended += 1

            ledger["last_record_hash"] = (
                ledger["records"][-1]["record_hash"]
                if ledger["records"] else None
            )
            save_ledger(args.output, ledger)

            result = {
                "agent_version": VERSION,
                "engine": ENGINE,
                "decision_status": "RECORDED",
                "overall_action": (
                    "PAPER_FILL"
                    if any(
                        r["outcome"]["paper_execution_action"] == "PAPER_FILL"
                        for r in records
                    )
                    else "NO_ACTION"
                ),
                "records_appended": appended,
                "blocked_reasons": [],
                "ledger_hash": ledger.get("ledger_hash"),
                "guardrails": ledger["guardrails"],
            }

        print_report(result, ledger, args.output)
        return 0

    except Exception as exc:
        print()
        print("ENGINE ERROR")
        print("-" * 100)
        print(f"{type(exc).__name__}: {exc}")
        print("FAIL-CLOSED: no ledger record was published.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
