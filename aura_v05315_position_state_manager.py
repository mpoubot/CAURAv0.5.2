#!/usr/bin/env python3
"""
AURA v0.5.3.15 — Position State Manager

LOCKED ARCHITECTURE
-------------------
Consumes the canonical output of AURA v0.5.3.14 — Risk Gate and maintains
AURA's deterministic local position state for BTC/USD and ETH/USD.

This layer deliberately does NOT:
- fetch market data;
- calculate indicators;
- create or modify signals;
- choose position size;
- place orders;
- perform paper execution;
- perform live execution;
- assume that a risk pass is an order fill.

Its sole responsibility is to manage the POSITION STATE / POSITION INTENT
boundary between the Risk Gate and the future Execution Adapter.

Important semantics:
    RISK_PASS + FLAT
        -> ENTRY_PENDING

    RISK_PASS + existing position
        -> MANAGING

    NO_SIGNAL
        -> no new entry is created

    BLOCKED / integrity failure
        -> no state promotion; fail closed

Execution remains permanently disabled in this build.

The manager supports future execution lifecycle events through an optional
JSON event input, but it never submits those events to a broker itself.

Default input:
    regime_output/risk_gate/risk_gate.json

Default persistent state / output:
    regime_output/position_state/position_state.json

Optional execution-event input:
    --event <path>

Optional broker reconciliation snapshot:
    --broker-state <path>

The broker-state file is an adapter boundary only. This module never contacts
Alpaca directly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


VERSION = "AURA v0.5.3.15"
ENGINE = "POSITION_STATE_MANAGER"

DEFAULT_INPUT = Path(r"regime_output\risk_gate\risk_gate.json")
DEFAULT_OUTPUT = Path(r"regime_output\position_state\position_state.json")
DEFAULT_STATE = DEFAULT_OUTPUT

SYMBOLS = ("BTC/USD", "ETH/USD")

EXPECTED_SOURCE_ENGINE = "RISK_GATE"
EXPECTED_SOURCE_VERSION = "AURA v0.5.3.14"

# The research architecture remains execution-disabled until the future
# paper-execution build explicitly changes the execution layer. This module
# must never change that policy.
ORDERS_ALLOWED = False
PAPER_EXECUTION = False
LIVE_EXECUTION = False

POSITION_STATES = {
    "FLAT",
    "ENTRY_PENDING",
    "PARTIALLY_FILLED",
    "OPEN",
    "MANAGING",
    "EXIT_PENDING",
    "RECONCILIATION_REQUIRED",
    "ERROR",
}

EVENT_TYPES = {
    "ORDER_SUBMITTED",
    "ORDER_ACCEPTED",
    "PARTIALLY_FILLED",
    "FILLED",
    "ORDER_REJECTED",
    "CANCELLED",
    "EXIT_REQUESTED",
    "POSITION_CLOSED",
}


def utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


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
        raise ValueError(f"JSON must contain an object at top level: {path}")
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )


def new_symbol_state(symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "position_state": "FLAT",
        "position_quantity": None,
        "average_entry_price": None,
        "entry_timestamp": None,
        "exit_timestamp": None,
        "active_order_id": None,
        "active_idempotency_key": None,
        "last_signal_timestamp": None,
        "last_risk_decision": None,
        "last_transition": "INITIALIZED",
        "last_transition_reason": "NO_PRIOR_STATE",
        "last_update_timestamp": None,
        "reconciliation_status": "NOT_AVAILABLE",
        "reconciliation_reason": "NO_BROKER_SNAPSHOT_SUPPLIED",
    }


def base_state() -> dict[str, Any]:
    return {
        "agent_version": VERSION,
        "engine": ENGINE,
        "state_version": 1,
        "generated_at": utc_now_iso(),
        "decision_status": "BLOCKED",
        "overall_action": "NO_ACTION",
        "execution_permitted": False,
        "orders_allowed": False,
        "paper_execution": False,
        "live_execution": False,
        "source_engine": EXPECTED_SOURCE_VERSION,
        "source_input": str(DEFAULT_INPUT),
        "input_risk_gate_hash": None,
        "state_hash": None,
        "idempotency": {
            "enabled": True,
            "key_format": "symbol + signal_timestamp + strategy_version",
            "duplicate_action": "NO_NEW_ENTRY",
        },
        "guardrails": {
            "single_source_of_truth_upstream": True,
            "risk_gate_required": True,
            "position_creation": False,
            "position_sizing": False,
            "order_submission": False,
            "market_data_fetch": False,
            "indicator_recalculation": False,
            "paper_execution": False,
            "live_execution": False,
            "fail_closed": True,
        },
        "positions": {symbol: new_symbol_state(symbol) for symbol in SYMBOLS},
        "decisions": {},
        "blocked_reasons": [],
        "transition_events": [],
    }


def normalize_loaded_state(payload: dict[str, Any]) -> dict[str, Any]:
    """Accept only compatible state files; missing fields are initialized."""
    state = base_state()

    if payload.get("engine") not in {None, ENGINE}:
        raise ValueError("INCOMPATIBLE_POSITION_STATE_ENGINE")

    positions = payload.get("positions")
    if positions is not None and not isinstance(positions, dict):
        raise ValueError("INVALID_POSITIONS_OBJECT")

    if isinstance(positions, dict):
        for symbol in SYMBOLS:
            item = positions.get(symbol)
            if isinstance(item, dict):
                merged = new_symbol_state(symbol)
                merged.update(item)
                state["positions"][symbol] = merged

    events = payload.get("transition_events")
    if isinstance(events, list):
        state["transition_events"] = events[-100:]

    return state


def load_previous_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return base_state()
    return normalize_loaded_state(load_json(path))


def risk_gate_integrity_ok(risk: dict[str, Any]) -> tuple[bool, list[str]]:
    errors: list[str] = []

    if risk.get("engine") != EXPECTED_SOURCE_ENGINE:
        errors.append("WRONG_UPSTREAM_ENGINE")
    if risk.get("agent_version") != EXPECTED_SOURCE_VERSION:
        errors.append("WRONG_UPSTREAM_VERSION")
    if risk.get("execution_permitted") is not False:
        errors.append("UPSTREAM_EXECUTION_NOT_DISABLED")
    if risk.get("risk_authorized") is not False and risk.get("overall_risk_decision") not in {
        "RISK_PASS"
    }:
        errors.append("INVALID_RISK_AUTHORIZATION_SEMANTICS")

    guardrails = risk.get("guardrails")
    if not isinstance(guardrails, dict):
        errors.append("MISSING_UPSTREAM_GUARDRAILS")
    else:
        if guardrails.get("orders_allowed") is not False:
            errors.append("UPSTREAM_ORDERS_NOT_DISABLED")
        if guardrails.get("paper_execution") is not False:
            errors.append("UPSTREAM_PAPER_EXECUTION_NOT_DISABLED")
        if guardrails.get("live_execution") is not False:
            errors.append("UPSTREAM_LIVE_EXECUTION_NOT_DISABLED")
        if guardrails.get("fail_closed") is not True:
            errors.append("UPSTREAM_FAIL_CLOSED_NOT_ENFORCED")

    decisions = risk.get("decisions")
    if not isinstance(decisions, dict):
        errors.append("MISSING_RISK_DECISIONS")
    else:
        for symbol in SYMBOLS:
            item = decisions.get(symbol)
            if not isinstance(item, dict):
                errors.append(f"MISSING_RISK_DECISION:{symbol}")

    return len(errors) == 0, sorted(set(errors))


def canonical_risk_hash(risk: dict[str, Any]) -> str:
    return sha256_text(stable_json(risk))


def idempotency_key(symbol: str, signal_timestamp: Any) -> str | None:
    if not isinstance(signal_timestamp, str) or not signal_timestamp:
        return None
    return f"{symbol}|{signal_timestamp}|{VERSION}"


def get_signal_timestamp(item: dict[str, Any]) -> str | None:
    for key in ("state_timestamp", "signal_timestamp", "timestamp"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def apply_broker_reconciliation(
    state_item: dict[str, Any],
    broker_item: dict[str, Any] | None,
) -> tuple[bool, str]:
    """Compare local position state with an optional broker snapshot.

    The manager never fetches broker data. The future execution adapter may
    supply a snapshot using the schema:
        {"position_quantity": number, "position_state": "FLAT"|"OPEN"|...}
    """
    if broker_item is None:
        state_item["reconciliation_status"] = "NOT_AVAILABLE"
        state_item["reconciliation_reason"] = "NO_BROKER_SNAPSHOT_SUPPLIED"
        return True, "NO_BROKER_SNAPSHOT_SUPPLIED"

    if not isinstance(broker_item, dict):
        state_item["reconciliation_status"] = "FAILED"
        state_item["reconciliation_reason"] = "INVALID_BROKER_SNAPSHOT"
        return False, "INVALID_BROKER_SNAPSHOT"

    local_state = state_item.get("position_state")
    broker_state = broker_item.get("position_state")
    local_qty = state_item.get("position_quantity")
    broker_qty = broker_item.get("position_quantity")

    # Only compare fields explicitly supplied by the adapter.
    mismatch = False
    reasons: list[str] = []

    if broker_state is not None and broker_state != local_state:
        mismatch = True
        reasons.append("POSITION_STATE_MISMATCH")

    if broker_qty is not None and local_qty is not None:
        try:
            if abs(float(broker_qty) - float(local_qty)) > 1e-12:
                mismatch = True
                reasons.append("POSITION_QUANTITY_MISMATCH")
        except (TypeError, ValueError):
            mismatch = True
            reasons.append("INVALID_BROKER_QUANTITY")

    if mismatch:
        state_item["reconciliation_status"] = "FAILED"
        state_item["reconciliation_reason"] = ";".join(reasons)
        return False, ";".join(reasons)

    state_item["reconciliation_status"] = "PASS"
    state_item["reconciliation_reason"] = "LOCAL_AND_BROKER_STATE_CONSISTENT"
    return True, "LOCAL_AND_BROKER_STATE_CONSISTENT"


def record_transition(
    state: dict[str, Any],
    symbol: str,
    previous: str,
    new: str,
    reason: str,
    key: str | None,
) -> None:
    event = {
        "timestamp": utc_now_iso(),
        "symbol": symbol,
        "from": previous,
        "to": new,
        "reason": reason,
        "idempotency_key": key,
    }
    state["transition_events"].append(event)
    state["transition_events"] = state["transition_events"][-100:]


def process_execution_event(
    state: dict[str, Any],
    symbol: str,
    event: dict[str, Any],
) -> tuple[str, str]:
    """Apply a broker/execution event without performing execution."""
    event_type = str(event.get("event_type", "")).strip().upper()
    if event_type not in EVENT_TYPES:
        raise ValueError(f"UNKNOWN_EXECUTION_EVENT:{event_type}")

    item = state["positions"][symbol]
    previous = item["position_state"]
    reason = f"EXECUTION_EVENT:{event_type}"

    if event_type in {"ORDER_SUBMITTED", "ORDER_ACCEPTED"}:
        if previous not in {"ENTRY_PENDING", "EXIT_PENDING"}:
            return previous, "EVENT_IGNORED_INVALID_STATE"
        item["active_order_id"] = event.get("order_id")
        return previous, "EVENT_RECORDED"

    if event_type == "PARTIALLY_FILLED":
        if previous not in {"ENTRY_PENDING", "OPEN"}:
            return previous, "EVENT_IGNORED_INVALID_STATE"
        item["position_state"] = "PARTIALLY_FILLED"
        if event.get("order_id") is not None:
            item["active_order_id"] = event.get("order_id")
        if event.get("position_quantity") is not None:
            item["position_quantity"] = event.get("position_quantity")
        if event.get("average_entry_price") is not None:
            item["average_entry_price"] = event.get("average_entry_price")
        record_transition(state, symbol, previous, item["position_state"], reason, item.get("active_idempotency_key"))
        return item["position_state"], "EVENT_APPLIED"

    if event_type == "FILLED":
        if previous not in {"ENTRY_PENDING", "PARTIALLY_FILLED", "EXIT_PENDING"}:
            return previous, "EVENT_IGNORED_INVALID_STATE"
        # The execution adapter owns the meaning of the fill. An entry fill
        # creates OPEN; an exit fill creates FLAT when explicitly marked exit.
        is_exit = bool(event.get("is_exit", previous == "EXIT_PENDING"))
        new_state = "FLAT" if is_exit else "OPEN"
        item["position_state"] = new_state
        item["active_order_id"] = None
        if is_exit:
            item["exit_timestamp"] = event.get("timestamp", utc_now_iso())
            item["position_quantity"] = None
            item["average_entry_price"] = None
        else:
            item["entry_timestamp"] = event.get("timestamp", utc_now_iso())
            if event.get("position_quantity") is not None:
                item["position_quantity"] = event.get("position_quantity")
            if event.get("average_entry_price") is not None:
                item["average_entry_price"] = event.get("average_entry_price")
        record_transition(state, symbol, previous, new_state, reason, item.get("active_idempotency_key"))
        return new_state, "EVENT_APPLIED"

    if event_type in {"ORDER_REJECTED", "CANCELLED"}:
        if previous in {"ENTRY_PENDING", "EXIT_PENDING"}:
            new_state = "FLAT" if previous == "ENTRY_PENDING" else "OPEN"
            item["position_state"] = new_state
            item["active_order_id"] = None
            record_transition(state, symbol, previous, new_state, reason, item.get("active_idempotency_key"))
            return new_state, "EVENT_APPLIED"
        return previous, "EVENT_IGNORED_INVALID_STATE"

    if event_type == "EXIT_REQUESTED":
        if previous not in {"OPEN", "MANAGING", "PARTIALLY_FILLED"}:
            return previous, "EVENT_IGNORED_INVALID_STATE"
        item["position_state"] = "EXIT_PENDING"
        record_transition(state, symbol, previous, "EXIT_PENDING", reason, item.get("active_idempotency_key"))
        return "EXIT_PENDING", "EVENT_APPLIED"

    if event_type == "POSITION_CLOSED":
        if previous not in {"OPEN", "MANAGING", "EXIT_PENDING", "PARTIALLY_FILLED"}:
            return previous, "EVENT_IGNORED_INVALID_STATE"
        item["position_state"] = "FLAT"
        item["position_quantity"] = None
        item["average_entry_price"] = None
        item["active_order_id"] = None
        item["exit_timestamp"] = event.get("timestamp", utc_now_iso())
        record_transition(state, symbol, previous, "FLAT", reason, item.get("active_idempotency_key"))
        return "FLAT", "EVENT_APPLIED"

    return previous, "EVENT_NOT_HANDLED"


def process_symbol(
    state: dict[str, Any],
    symbol: str,
    risk_item: dict[str, Any],
    broker_item: dict[str, Any] | None,
    execution_event: dict[str, Any] | None,
) -> dict[str, Any]:
    item = state["positions"][symbol]
    previous = item["position_state"]
    upstream = risk_item.get("risk_decision")
    signal_ts = get_signal_timestamp(risk_item)
    key = idempotency_key(symbol, signal_ts)

    item["last_risk_decision"] = upstream
    item["last_signal_timestamp"] = signal_ts
    item["last_update_timestamp"] = utc_now_iso()

    reconciliation_ok, reconciliation_reason = apply_broker_reconciliation(
        item, broker_item
    )

    if not reconciliation_ok:
        item["position_state"] = "RECONCILIATION_REQUIRED"
        item["last_transition"] = "RECONCILIATION_REQUIRED"
        item["last_transition_reason"] = reconciliation_reason
        record_transition(
            state,
            symbol,
            previous,
            "RECONCILIATION_REQUIRED",
            reconciliation_reason,
            key,
        )
        return {
            "symbol": symbol,
            "upstream_risk_decision": upstream,
            "position_state": "RECONCILIATION_REQUIRED",
            "previous_position_state": previous,
            "action": "NO_ACTION",
            "transition_allowed": False,
            "duplicate_signal": False,
            "idempotency_key": key,
            "reconciliation_status": item["reconciliation_status"],
            "reason": reconciliation_reason,
        }

    if execution_event is not None:
        try:
            new_state, event_result = process_execution_event(
                state, symbol, execution_event
            )
            if event_result == "EVENT_APPLIED":
                item["last_transition"] = new_state
                item["last_transition_reason"] = f"APPLIED:{execution_event.get('event_type')}"
            return {
                "symbol": symbol,
                "upstream_risk_decision": upstream,
                "position_state": item["position_state"],
                "previous_position_state": previous,
                "action": "EVENT_APPLIED" if event_result == "EVENT_APPLIED" else "EVENT_IGNORED",
                "transition_allowed": event_result == "EVENT_APPLIED",
                "duplicate_signal": False,
                "idempotency_key": key,
                "reconciliation_status": item["reconciliation_status"],
                "reason": event_result,
            }
        except Exception as exc:
            item["position_state"] = "ERROR"
            item["last_transition"] = "ERROR"
            item["last_transition_reason"] = str(exc)
            record_transition(state, symbol, previous, "ERROR", str(exc), key)
            return {
                "symbol": symbol,
                "upstream_risk_decision": upstream,
                "position_state": "ERROR",
                "previous_position_state": previous,
                "action": "NO_ACTION",
                "transition_allowed": False,
                "duplicate_signal": False,
                "idempotency_key": key,
                "reconciliation_status": item["reconciliation_status"],
                "reason": f"EXECUTION_EVENT_ERROR:{type(exc).__name__}",
            }

    if upstream == "RISK_PASS":
        if previous == "RECONCILIATION_REQUIRED":
            return {
                "symbol": symbol,
                "upstream_risk_decision": upstream,
                "position_state": previous,
                "previous_position_state": previous,
                "action": "NO_ACTION",
                "transition_allowed": False,
                "duplicate_signal": False,
                "idempotency_key": key,
                "reconciliation_status": item["reconciliation_status"],
                "reason": "RECONCILIATION_REQUIRED",
            }

        duplicate = key is not None and key == item.get("active_idempotency_key")

        if previous == "FLAT":
            item["position_state"] = "ENTRY_PENDING"
            item["active_idempotency_key"] = key
            item["last_transition"] = "ENTRY_PENDING"
            item["last_transition_reason"] = "RISK_PASS_NEW_ENTRY_INTENT"
            record_transition(
                state,
                symbol,
                previous,
                "ENTRY_PENDING",
                "RISK_PASS_NEW_ENTRY_INTENT",
                key,
            )
            action = "ENTRY_INTENT"
            allowed = True
            reason = "RISK_PASS_ACCEPTED;ENTRY_INTENT_CREATED;NO_ORDER_SUBMITTED"
        elif duplicate:
            action = "NO_NEW_ENTRY"
            allowed = False
            reason = "DUPLICATE_SIGNAL_IDEMPOTENCY_KEY"
        elif previous in {"ENTRY_PENDING", "PARTIALLY_FILLED", "OPEN", "MANAGING"}:
            if previous in {"OPEN", "MANAGING"}:
                item["position_state"] = "MANAGING"
                if previous != "MANAGING":
                    record_transition(
                        state,
                        symbol,
                        previous,
                        "MANAGING",
                        "RISK_PASS_WITH_EXISTING_POSITION",
                        key,
                    )
                action = "MANAGE_EXISTING"
                allowed = False
                reason = "EXISTING_POSITION_PRESENT"
            else:
                action = "NO_NEW_ENTRY"
                allowed = False
                reason = "ENTRY_ALREADY_PENDING_OR_PARTIALLY_FILLED"
        else:
            action = "NO_ACTION"
            allowed = False
            reason = f"UNSAFE_POSITION_STATE:{previous}"

        item["last_update_timestamp"] = utc_now_iso()
        return {
            "symbol": symbol,
            "upstream_risk_decision": upstream,
            "position_state": item["position_state"],
            "previous_position_state": previous,
            "action": action,
            "transition_allowed": allowed,
            "duplicate_signal": duplicate,
            "idempotency_key": key,
            "reconciliation_status": item["reconciliation_status"],
            "reason": reason,
        }

    if upstream == "NO_SIGNAL":
        return {
            "symbol": symbol,
            "upstream_risk_decision": upstream,
            "position_state": item["position_state"],
            "previous_position_state": previous,
            "action": "NO_ACTION",
            "transition_allowed": False,
            "duplicate_signal": False,
            "idempotency_key": key,
            "reconciliation_status": item["reconciliation_status"],
            "reason": "NO_UPSTREAM_SIGNAL_CANDIDATE",
        }

    # BLOCKED or unknown risk decision: never promote position state.
    return {
        "symbol": symbol,
        "upstream_risk_decision": upstream,
        "position_state": item["position_state"],
        "previous_position_state": previous,
        "action": "NO_ACTION",
        "transition_allowed": False,
        "duplicate_signal": False,
        "idempotency_key": key,
        "reconciliation_status": item["reconciliation_status"],
        "reason": "UPSTREAM_RISK_BLOCKED_OR_UNEXPECTED",
    }


def finalize_state_hash(state: dict[str, Any]) -> None:
    state_for_hash = dict(state)
    state_for_hash["state_hash"] = None
    state["state_hash"] = sha256_text(stable_json(state_for_hash))


def evaluate(
    risk: dict[str, Any],
    state: dict[str, Any],
    input_path: Path,
    broker_state: dict[str, Any] | None,
    execution_event: dict[str, Any] | None,
) -> dict[str, Any]:
    state["generated_at"] = utc_now_iso()
    state["source_input"] = str(input_path.resolve())
    state["input_risk_gate_hash"] = canonical_risk_hash(risk)
    state["source_engine"] = EXPECTED_SOURCE_VERSION
    state["decisions"] = {}
    state["blocked_reasons"] = []

    integrity_ok, integrity_errors = risk_gate_integrity_ok(risk)
    if not integrity_ok:
        state["decision_status"] = "BLOCKED"
        state["overall_action"] = "NO_ACTION"
        state["blocked_reasons"] = integrity_errors
        for symbol in SYMBOLS:
            item = state["positions"][symbol]
            state["decisions"][symbol] = {
                "symbol": symbol,
                "upstream_risk_decision": None,
                "position_state": item["position_state"],
                "previous_position_state": item["position_state"],
                "action": "NO_ACTION",
                "transition_allowed": False,
                "duplicate_signal": False,
                "idempotency_key": None,
                "reconciliation_status": item["reconciliation_status"],
                "reason": "UPSTREAM_INTEGRITY_CHECK_FAILED",
            }
        finalize_state_hash(state)
        return state

    broker_positions = {}
    if broker_state is not None:
        broker_positions = broker_state.get("positions", {})
        if not isinstance(broker_positions, dict):
            state["decision_status"] = "BLOCKED"
            state["overall_action"] = "NO_ACTION"
            state["blocked_reasons"] = ["INVALID_BROKER_STATE_POSITIONS"]
            finalize_state_hash(state)
            return state

    execution_symbol = None
    if execution_event is not None:
        execution_symbol = execution_event.get("symbol")
        if execution_symbol not in SYMBOLS:
            state["decision_status"] = "BLOCKED"
            state["overall_action"] = "NO_ACTION"
            state["blocked_reasons"] = ["INVALID_EXECUTION_EVENT_SYMBOL"]
            finalize_state_hash(state)
            return state

    for symbol in SYMBOLS:
        risk_item = risk["decisions"][symbol]
        broker_item = broker_positions.get(symbol) if broker_state is not None else None
        event = execution_event if execution_symbol == symbol else None
        state["decisions"][symbol] = process_symbol(
            state, symbol, risk_item, broker_item, event
        )

    states = [state["positions"][s]["position_state"] for s in SYMBOLS]
    actions = [state["decisions"][s]["action"] for s in SYMBOLS]

    if any(s in {"RECONCILIATION_REQUIRED", "ERROR"} for s in states):
        state["decision_status"] = "BLOCKED"
        state["overall_action"] = "NO_ACTION"
        state["blocked_reasons"] = [
            f"{symbol}:{state['positions'][symbol]['position_state']}"
            for symbol in SYMBOLS
            if state["positions"][symbol]["position_state"] in {"RECONCILIATION_REQUIRED", "ERROR"}
        ]
    else:
        state["decision_status"] = "DECIDED"
        if "ENTRY_INTENT" in actions:
            state["overall_action"] = "ENTRY_INTENT"
        elif "MANAGE_EXISTING" in actions:
            state["overall_action"] = "MANAGE_EXISTING"
        elif "NO_NEW_ENTRY" in actions:
            state["overall_action"] = "NO_NEW_ENTRY"
        else:
            state["overall_action"] = "NO_ACTION"

    finalize_state_hash(state)
    return state


def print_report(state: dict[str, Any], output_path: Path) -> None:
    print("=" * 100)
    print(f"{VERSION} — POSITION STATE MANAGER")
    print("=" * 100)
    print()
    print("MODE                 : RESEARCH / INFRASTRUCTURE ONLY")
    print("ORDERS               : DISABLED")
    print("PAPER EXECUTION      : DISABLED")
    print("LIVE EXECUTION       : DISABLED")
    print("POSITION SIZING      : DISABLED")
    print("FAIL-CLOSED POLICY   : ENABLED")
    print()
    print(f"SOURCE               : {EXPECTED_SOURCE_VERSION} RISK GATE")
    print("MARKET DATA FETCH    : DISABLED")
    print("INDICATOR RECALC     : DISABLED")
    print("ORDER SUBMISSION     : DISABLED")
    print("POSITION CREATION   : INTENT ONLY — NO BROKER ACTION")
    print()
    print("POSITION STATE")
    print("-" * 100)
    print(f"STATUS               : {state['decision_status']}")
    print(f"OVERALL ACTION       : {state['overall_action']}")
    print(f"EXECUTION PERMITTED  : {state['execution_permitted']}")
    print()

    for symbol in SYMBOLS:
        p = state["positions"][symbol]
        d = state["decisions"].get(symbol, {})
        print(symbol)
        print(f"  POSITION STATE     : {p['position_state']}")
        print(f"  PREVIOUS STATE     : {d.get('previous_position_state')}")
        print(f"  RISK DECISION      : {d.get('upstream_risk_decision')}")
        print(f"  ACTION             : {d.get('action')}")
        print(f"  TRANSITION ALLOWED : {d.get('transition_allowed')}")
        print(f"  DUPLICATE SIGNAL   : {d.get('duplicate_signal')}")
        print(f"  IDEMPOTENCY KEY    : {d.get('idempotency_key')}")
        print(f"  RECONCILIATION     : {p['reconciliation_status']}")
        print(f"  REASON             : {d.get('reason')}")
        print()

    if state["blocked_reasons"]:
        print("FAIL-CLOSED REASONS")
        print("-" * 100)
        for reason in state["blocked_reasons"]:
            print(f"  - {reason}")
        print()

    print(f"STATE HASH           : {state['state_hash']}")
    print(f"OUTPUT               : {output_path}")
    print("=" * 100)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AURA v0.5.3.15 deterministic Position State Manager"
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help="v0.5.3.14 risk-gate JSON",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="persistent position-state JSON",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=None,
        help="optional existing state file; defaults to --output",
    )
    parser.add_argument(
        "--broker-state",
        type=Path,
        default=None,
        help="optional broker reconciliation snapshot; no broker API is called",
    )
    parser.add_argument(
        "--event",
        type=Path,
        default=None,
        help="optional execution lifecycle event JSON",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one deterministic state-management cycle and exit",
    )
    args = parser.parse_args()
    _ = args.once

    state_path = args.state or args.output

    try:
        risk = load_json(args.input)
        state = load_previous_state(state_path)
        broker_state = load_json(args.broker_state) if args.broker_state else None
        execution_event = load_json(args.event) if args.event else None

        result = evaluate(
            risk=risk,
            state=state,
            input_path=args.input,
            broker_state=broker_state,
            execution_event=execution_event,
        )

        write_json(args.output, result)
        print_report(result, args.output)

        # A safe BLOCKED result is intentional and is still an orchestration
        # success. The JSON is the authoritative outcome.
        return 0

    except Exception as exc:
        print()
        print("ENGINE ERROR")
        print("-" * 100)
        print(f"{type(exc).__name__}: {exc}")
        print("FAIL-CLOSED: no position transition was published.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
