#!/usr/bin/env python3
"""
AURA v0.5.3.16 — Paper Execution Adapter

LOCKED ARCHITECTURE
-------------------
Consumes the authoritative output of AURA v0.5.3.15 — Position State Manager
and simulates execution without contacting a broker or exchange.

This layer deliberately does NOT:
- fetch market data;
- calculate indicators;
- create or modify signals;
- perform risk decisions;
- choose strategy parameters;
- perform live execution;
- submit real orders;
- modify the upstream position state directly.

Its responsibility is to translate an approved POSITION INTENT into a
deterministic PAPER ORDER / PAPER FILL EVENT.

Architecture:

    v0.5.3.14 Risk Gate
             |
             v
    v0.5.3.15 Position State Manager
             |
             | ENTRY_INTENT / EXIT intent
             v
    v0.5.3.16 Paper Execution Adapter
             |
             | simulated order + simulated fill
             v
    execution_event.json
             |
             v
    v0.5.3.15 Position State Manager

Safety:
- Real orders are impossible in this module.
- Live execution is permanently disabled.
- Paper execution is simulation only.
- RISK_PASS is never treated as a fill.
- Missing/invalid prices fail closed.
- Unsupported states fail closed.
- The adapter never invents a signal.

Determinism:
The same input, execution model, and timestamp produce the same fill.
The adapter supports explicit reference price, spread, slippage, fee,
and latency parameters. Defaults are deterministic and intentionally
conservative/simple for the first paper-execution build.

Input:
    regime_output/position_state/position_state.json

Output:
    regime_output/paper_execution/paper_execution.json

Optional:
    --input
    --output
    --timestamp
    --spread-bps
    --slippage-bps
    --fee-bps
    --latency-ms
    --fill-mode {FULL}
    --symbol
    --side
    --quantity
    --price

The adapter can also consume a direct paper-order JSON fixture using
--order. This is useful for deterministic tests without changing the
upstream strategy or manufacturing a real signal.
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


VERSION = "AURA v0.5.3.16"
ENGINE = "PAPER_EXECUTION_ADAPTER"

EXPECTED_SOURCE_ENGINE = "POSITION_STATE_MANAGER"
EXPECTED_SOURCE_VERSION = "AURA v0.5.3.15"

DEFAULT_INPUT = Path(r"regime_output\position_state\position_state.json")
DEFAULT_OUTPUT = Path(r"regime_output\paper_execution\paper_execution.json")

SYMBOLS = ("BTC/USD", "ETH/USD")

# Hard safety locks. There is intentionally no live execution switch.
ORDERS_ALLOWED = False
REAL_ORDER_SUBMISSION = False
LIVE_EXECUTION = False
PAPER_EXECUTION = True

SUPPORTED_INTENTS = {
    "ENTRY_INTENT",
    "EXIT_INTENT",
}

SUPPORTED_SIDES = {"BUY", "SELL"}


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


def valid_number(value: Any, *, positive: bool = False) -> bool:
    if isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(number):
        return False
    if positive and number <= 0:
        return False
    return True


def parse_iso_timestamp(value: Any) -> str:
    if value is None:
        return utc_now_iso()
    if not isinstance(value, str) or not value.strip():
        raise ValueError("INVALID_TIMESTAMP")
    text = value.strip()
    try:
        normalized = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("INVALID_TIMESTAMP") from exc
    if dt.tzinfo is None:
        raise ValueError("TIMESTAMP_MUST_BE_TIMEZONE_AWARE")
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def base_result() -> dict[str, Any]:
    return {
        "agent_version": VERSION,
        "engine": ENGINE,
        "state_version": 1,
        "generated_at": utc_now_iso(),
        "decision_status": "BLOCKED",
        "overall_action": "NO_ACTION",
        "paper_execution": True,
        "orders_allowed": False,
        "real_order_submission": False,
        "live_execution": False,
        "source_engine": EXPECTED_SOURCE_VERSION,
        "source_input": str(DEFAULT_INPUT),
        "input_state_hash": None,
        "execution_model": {
            "fill_mode": "FULL",
            "spread_bps": None,
            "slippage_bps": None,
            "fee_bps": None,
            "latency_ms": None,
        },
        "guardrails": {
            "single_source_of_truth_upstream": True,
            "position_state_manager_required": True,
            "signal_creation": False,
            "risk_decision": False,
            "position_sizing": False,
            "market_data_fetch": False,
            "indicator_recalculation": False,
            "real_order_submission": False,
            "live_execution": False,
            "fail_closed": True,
            "deterministic_fill_model": True,
        },
        "orders": [],
        "execution_events": [],
        "blocked_reasons": [],
        "result_hash": None,
    }


def canonical_input_hash(payload: dict[str, Any]) -> str:
    return sha256_text(stable_json(payload))


def validate_upstream_state(state: dict[str, Any]) -> tuple[bool, list[str]]:
    errors: list[str] = []

    if state.get("engine") != EXPECTED_SOURCE_ENGINE:
        errors.append("WRONG_UPSTREAM_ENGINE")

    if state.get("agent_version") != EXPECTED_SOURCE_VERSION:
        errors.append("WRONG_UPSTREAM_VERSION")

    if state.get("orders_allowed") is not False:
        # Some v0.5.3.15 files expose this as a top-level safety flag.
        errors.append("UPSTREAM_ORDERS_NOT_DISABLED")

    if state.get("paper_execution") is not False:
        # v0.5.3.15 is expected to be execution-disabled.
        errors.append("UPSTREAM_PAPER_EXECUTION_NOT_DISABLED")

    if state.get("live_execution") is not False:
        errors.append("UPSTREAM_LIVE_EXECUTION_NOT_DISABLED")

    guardrails = state.get("guardrails")
    if not isinstance(guardrails, dict):
        errors.append("MISSING_UPSTREAM_GUARDRAILS")
    else:
        if guardrails.get("fail_closed") is not True:
            errors.append("UPSTREAM_FAIL_CLOSED_NOT_ENFORCED")
        if guardrails.get("live_execution") is not False:
            errors.append("UPSTREAM_LIVE_EXECUTION_NOT_DISABLED")

    positions = state.get("positions")
    if not isinstance(positions, dict):
        errors.append("MISSING_UPSTREAM_POSITIONS")
    else:
        for symbol in SYMBOLS:
            if not isinstance(positions.get(symbol), dict):
                errors.append(f"MISSING_UPSTREAM_POSITION:{symbol}")

    return len(errors) == 0, sorted(set(errors))


def infer_side(position: dict[str, Any], decision: dict[str, Any]) -> str | None:
    # v0.5.3.15 intentionally does not own strategy direction. If a future
    # upstream decision contains an explicit side, use it. Never guess.
    for source in (decision, position):
        side = source.get("side")
        if isinstance(side, str):
            side = side.strip().upper()
            if side in SUPPORTED_SIDES:
                return side
    return None


def infer_reference_price(
    position: dict[str, Any],
    decision: dict[str, Any],
) -> float | None:
    # Explicit execution/reference price only. The adapter never fetches price.
    for source in (decision, position):
        for key in (
            "reference_price",
            "signal_price",
            "entry_price",
            "price",
        ):
            value = source.get(key)
            if valid_number(value, positive=True):
                return float(value)
    return None


def infer_quantity(
    position: dict[str, Any],
    decision: dict[str, Any],
) -> float | None:
    # Position sizing is deliberately NOT performed here.
    for source in (decision, position):
        for key in ("quantity", "position_quantity"):
            value = source.get(key)
            if valid_number(value, positive=True):
                return float(value)
    return None


def infer_intent(decision: dict[str, Any], position: dict[str, Any]) -> str | None:
    action = decision.get("action")
    if action == "ENTRY_INTENT":
        return "ENTRY_INTENT"

    # Future close/exit intent may be explicitly supplied by the position
    # manager. We do not infer an exit merely from an arbitrary state.
    if action == "EXIT_INTENT":
        return "EXIT_INTENT"

    explicit = decision.get("execution_intent")
    if isinstance(explicit, str) and explicit.upper() in SUPPORTED_INTENTS:
        return explicit.upper()

    return None


def apply_fill_model(
    *,
    side: str,
    reference_price: float,
    quantity: float,
    spread_bps: float,
    slippage_bps: float,
) -> tuple[float, float, float]:
    """
    Deterministic market-style fill model.

    BUY pays half-spread plus slippage above reference.
    SELL receives half-spread minus slippage below reference.

    Returns:
        fill_price, spread_cost_per_unit, slippage_cost_per_unit
    """
    spread_fraction = spread_bps / 10_000.0
    slippage_fraction = slippage_bps / 10_000.0

    half_spread = reference_price * spread_fraction / 2.0
    slippage = reference_price * slippage_fraction

    if side == "BUY":
        fill_price = reference_price + half_spread + slippage
    elif side == "SELL":
        fill_price = reference_price - half_spread - slippage
    else:
        raise ValueError("UNSUPPORTED_SIDE")

    if fill_price <= 0 or not math.isfinite(fill_price):
        raise ValueError("INVALID_SIMULATED_FILL_PRICE")

    return fill_price, half_spread, slippage


def build_execution_event(
    *,
    symbol: str,
    side: str,
    quantity: float,
    reference_price: float,
    fill_price: float,
    fee: float,
    timestamp: str,
    intent: str,
    idempotency_key: str | None,
    order_id: str,
    latency_ms: int,
) -> dict[str, Any]:
    is_exit = intent == "EXIT_INTENT"

    return {
        "event_type": "FILLED",
        "event_id": f"PAPER-{order_id}",
        "timestamp": timestamp,
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "reference_price": reference_price,
        "fill_price": fill_price,
        "average_entry_price": fill_price if not is_exit else None,
        "position_quantity": quantity,
        "fee": fee,
        "is_exit": is_exit,
        "paper": True,
        "real_order": False,
        "latency_ms": latency_ms,
        "idempotency_key": idempotency_key,
        "source_engine": ENGINE,
        "source_version": VERSION,
    }


def simulate_order(
    *,
    symbol: str,
    decision: dict[str, Any],
    position: dict[str, Any],
    timestamp: str,
    spread_bps: float,
    slippage_bps: float,
    fee_bps: float,
    latency_ms: int,
) -> dict[str, Any]:
    intent = infer_intent(decision, position)
    if intent not in SUPPORTED_INTENTS:
        raise ValueError("NO_SUPPORTED_EXECUTION_INTENT")

    side = infer_side(position, decision)
    if side not in SUPPORTED_SIDES:
        raise ValueError("MISSING_OR_INVALID_SIDE")

    quantity = infer_quantity(position, decision)
    if quantity is None:
        raise ValueError("MISSING_QUANTITY_POSITION_SIZING_NOT_PERFORMED")

    reference_price = infer_reference_price(position, decision)
    if reference_price is None:
        raise ValueError("MISSING_REFERENCE_PRICE")

    fill_price, spread_cost, slippage_cost = apply_fill_model(
        side=side,
        reference_price=reference_price,
        quantity=quantity,
        spread_bps=spread_bps,
        slippage_bps=slippage_bps,
    )

    gross_notional = fill_price * quantity
    fee = gross_notional * fee_bps / 10_000.0

    signal_ts = (
        decision.get("idempotency_key")
        or decision.get("signal_timestamp")
        or decision.get("timestamp")
    )
    if not isinstance(signal_ts, str) or not signal_ts:
        signal_ts = timestamp

    idempotency_key = decision.get("idempotency_key")
    if not isinstance(idempotency_key, str) or not idempotency_key:
        idempotency_key = f"{symbol}|{signal_ts}|{EXPECTED_SOURCE_VERSION}"

    raw_order_key = stable_json(
        {
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "reference_price": reference_price,
            "fill_price": fill_price,
            "intent": intent,
            "idempotency_key": idempotency_key,
        }
    )
    order_id = sha256_text(raw_order_key)[:24]

    event = build_execution_event(
        symbol=symbol,
        side=side,
        quantity=quantity,
        reference_price=reference_price,
        fill_price=fill_price,
        fee=fee,
        timestamp=timestamp,
        intent=intent,
        idempotency_key=idempotency_key,
        order_id=order_id,
        latency_ms=latency_ms,
    )

    return {
        "order_id": order_id,
        "symbol": symbol,
        "intent": intent,
        "side": side,
        "quantity": quantity,
        "reference_price": reference_price,
        "simulated_order_price": reference_price,
        "simulated_fill_price": fill_price,
        "spread_bps": spread_bps,
        "spread_cost_per_unit": spread_cost,
        "slippage_bps": slippage_bps,
        "slippage_cost_per_unit": slippage_cost,
        "fee_bps": fee_bps,
        "fee": fee,
        "gross_notional": gross_notional,
        "latency_ms": latency_ms,
        "fill_status": "FILLED",
        "paper": True,
        "real_order": False,
        "live_execution": False,
        "idempotency_key": idempotency_key,
        "execution_event": event,
    }


def finalize_hash(result: dict[str, Any]) -> None:
    copy = dict(result)
    copy["result_hash"] = None
    result["result_hash"] = sha256_text(stable_json(copy))


def evaluate(
    state: dict[str, Any],
    input_path: Path,
    *,
    spread_bps: float,
    slippage_bps: float,
    fee_bps: float,
    latency_ms: int,
    timestamp: str,
    selected_symbol: str | None,
    direct_order: dict[str, Any] | None,
) -> dict[str, Any]:
    result = base_result()
    result["source_input"] = str(input_path.resolve())
    result["input_state_hash"] = canonical_input_hash(state)
    result["execution_model"] = {
        "fill_mode": "FULL",
        "spread_bps": spread_bps,
        "slippage_bps": slippage_bps,
        "fee_bps": fee_bps,
        "latency_ms": latency_ms,
    }

    if direct_order is not None:
        # Direct fixtures are explicitly marked as test/simulation input.
        try:
            symbol = direct_order.get("symbol")
            if symbol not in SYMBOLS:
                raise ValueError("INVALID_SYMBOL")
            decision = dict(direct_order)
            position = dict(direct_order)
            paper_order = simulate_order(
                symbol=symbol,
                decision=decision,
                position=position,
                timestamp=timestamp,
                spread_bps=spread_bps,
                slippage_bps=slippage_bps,
                fee_bps=fee_bps,
                latency_ms=latency_ms,
            )
            result["orders"].append(paper_order)
            result["execution_events"].append(paper_order["execution_event"])
            result["decision_status"] = "EXECUTED_PAPER"
            result["overall_action"] = "PAPER_FILL"
            finalize_hash(result)
            return result
        except Exception as exc:
            result["blocked_reasons"] = [f"DIRECT_ORDER_BLOCKED:{exc}"]
            finalize_hash(result)
            return result

    integrity_ok, errors = validate_upstream_state(state)
    if not integrity_ok:
        result["blocked_reasons"] = errors
        finalize_hash(result)
        return result

    positions = state["positions"]
    decisions = state.get("decisions")
    if not isinstance(decisions, dict):
        result["blocked_reasons"] = ["MISSING_UPSTREAM_DECISIONS"]
        finalize_hash(result)
        return result

    symbols = [selected_symbol] if selected_symbol else list(SYMBOLS)
    if any(symbol not in SYMBOLS for symbol in symbols):
        result["blocked_reasons"] = ["INVALID_SELECTED_SYMBOL"]
        finalize_hash(result)
        return result

    for symbol in symbols:
        position = positions.get(symbol)
        decision = decisions.get(symbol)

        if not isinstance(position, dict) or not isinstance(decision, dict):
            result["blocked_reasons"].append(f"MISSING_DECISION_OR_POSITION:{symbol}")
            continue

        action = decision.get("action")
        state_name = position.get("position_state")

        # Normal safe case: there is no executable intent.
        if action in {"NO_ACTION", "NO_NEW_ENTRY", None}:
            continue

        # The manager may report an existing position without creating an
        # execution intent. That is not an instruction to trade.
        if action == "MANAGE_EXISTING":
            continue

        # Only explicit ENTRY_INTENT / EXIT_INTENT may reach simulation.
        if action not in SUPPORTED_INTENTS:
            result["blocked_reasons"].append(
                f"UNSUPPORTED_UPSTREAM_ACTION:{symbol}:{action}"
            )
            continue

        # ENTRY_INTENT is only meaningful from FLAT.
        if action == "ENTRY_INTENT" and state_name != "ENTRY_PENDING":
            result["blocked_reasons"].append(
                f"INVALID_ENTRY_STATE:{symbol}:{state_name}"
            )
            continue

        try:
            paper_order = simulate_order(
                symbol=symbol,
                decision=decision,
                position=position,
                timestamp=timestamp,
                spread_bps=spread_bps,
                slippage_bps=slippage_bps,
                fee_bps=fee_bps,
                latency_ms=latency_ms,
            )
            result["orders"].append(paper_order)
            result["execution_events"].append(paper_order["execution_event"])
        except Exception as exc:
            result["blocked_reasons"].append(
                f"SIMULATION_BLOCKED:{symbol}:{type(exc).__name__}:{exc}"
            )

    if result["orders"] and not result["blocked_reasons"]:
        result["decision_status"] = "EXECUTED_PAPER"
        result["overall_action"] = "PAPER_FILL"
    elif result["orders"] and result["blocked_reasons"]:
        result["decision_status"] = "PARTIAL"
        result["overall_action"] = "PAPER_FILL_WITH_BLOCKS"
    else:
        if result["blocked_reasons"]:
            result["decision_status"] = "BLOCKED"
        else:
            result["decision_status"] = "NO_EXECUTION"
        result["overall_action"] = "NO_ACTION"

    finalize_hash(result)
    return result


def print_report(result: dict[str, Any], output_path: Path) -> None:
    print("=" * 100)
    print(f"{VERSION} — PAPER EXECUTION ADAPTER")
    print("=" * 100)
    print()
    print("MODE                 : PAPER SIMULATION ONLY")
    print("REAL ORDERS          : DISABLED")
    print("LIVE EXECUTION       : DISABLED")
    print("POSITION SIZING      : DISABLED")
    print("RISK DECISION        : DISABLED")
    print("MARKET DATA FETCH    : DISABLED")
    print("FAIL-CLOSED POLICY   : ENABLED")
    print()
    print(f"SOURCE               : {EXPECTED_SOURCE_VERSION} POSITION STATE")
    print()
    print("EXECUTION")
    print("-" * 100)
    print(f"STATUS               : {result['decision_status']}")
    print(f"OVERALL ACTION       : {result['overall_action']}")
    print(f"ORDERS SIMULATED     : {len(result['orders'])}")
    print()

    for order in result["orders"]:
        print(order["symbol"])
        print(f"  ORDER ID           : {order['order_id']}")
        print(f"  INTENT             : {order['intent']}")
        print(f"  SIDE               : {order['side']}")
        print(f"  QUANTITY           : {order['quantity']}")
        print(f"  REFERENCE PRICE    : {order['reference_price']}")
        print(f"  SIMULATED FILL     : {order['simulated_fill_price']}")
        print(f"  SPREAD (bps)       : {order['spread_bps']}")
        print(f"  SLIPPAGE (bps)     : {order['slippage_bps']}")
        print(f"  FEE (bps)          : {order['fee_bps']}")
        print(f"  FEE                : {order['fee']}")
        print(f"  FILL STATUS        : {order['fill_status']}")
        print(f"  IDEMPOTENCY KEY    : {order['idempotency_key']}")
        print()

    if result["blocked_reasons"]:
        print("FAIL-CLOSED REASONS")
        print("-" * 100)
        for reason in result["blocked_reasons"]:
            print(f"  - {reason}")
        print()

    print(f"RESULT HASH          : {result['result_hash']}")
    print(f"OUTPUT               : {output_path}")
    print("=" * 100)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AURA v0.5.3.16 deterministic Paper Execution Adapter"
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help="v0.5.3.15 position-state JSON",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="paper-execution JSON output",
    )
    parser.add_argument(
        "--order",
        type=Path,
        default=None,
        help="optional direct paper-order test fixture JSON",
    )
    parser.add_argument(
        "--symbol",
        choices=SYMBOLS,
        default=None,
        help="process only one symbol from the upstream state",
    )
    parser.add_argument(
        "--timestamp",
        default=None,
        help="explicit timezone-aware timestamp for deterministic simulation",
    )
    parser.add_argument(
        "--spread-bps",
        type=float,
        default=0.0,
        help="simulated round-trip spread basis points; half applied to each side",
    )
    parser.add_argument(
        "--slippage-bps",
        type=float,
        default=0.0,
        help="simulated adverse slippage in basis points",
    )
    parser.add_argument(
        "--fee-bps",
        type=float,
        default=0.0,
        help="simulated execution fee in basis points",
    )
    parser.add_argument(
        "--latency-ms",
        type=int,
        default=0,
        help="recorded simulated latency; no wall-clock sleep is performed",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one deterministic cycle and exit",
    )
    args = parser.parse_args()
    _ = args.once

    try:
        if args.spread_bps < 0 or args.slippage_bps < 0 or args.fee_bps < 0:
            raise ValueError("EXECUTION_COSTS_CANNOT_BE_NEGATIVE")
        if args.latency_ms < 0:
            raise ValueError("LATENCY_CANNOT_BE_NEGATIVE")

        timestamp = parse_iso_timestamp(args.timestamp)

        if args.order:
            source_state = {}
            direct_order = load_json(args.order)
        else:
            source_state = load_json(args.input)
            direct_order = None

        result = evaluate(
            state=source_state,
            input_path=args.input,
            spread_bps=args.spread_bps,
            slippage_bps=args.slippage_bps,
            fee_bps=args.fee_bps,
            latency_ms=args.latency_ms,
            timestamp=timestamp,
            selected_symbol=args.symbol,
            direct_order=direct_order,
        )

        write_json(args.output, result)
        print_report(result, args.output)

        # Safe BLOCKED / NO_EXECUTION outcomes are valid orchestration results.
        return 0

    except Exception as exc:
        print()
        print("ENGINE ERROR")
        print("-" * 100)
        print(f"{type(exc).__name__}: {exc}")
        print("FAIL-CLOSED: no paper fill was published.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
