#!/usr/bin/env python3
"""
AURA v0.5.3.19 — Agent Supervisor

Supervisory layer for the deterministic AURA execution chain.

Design principles:
- Observes; it does not calculate trading signals.
- Does not change strategy parameters.
- Does not bypass Risk Gate, Position State, Ledger, or Kill Switch.
- Fail-closed: any required health/integrity failure makes execution non-permitted.
- Default mode is audit-only. Optional --enforce-kill-switch can request v0.5.3.18 halt.
- No exchange/API/network access.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple


VERSION = "AURA v0.5.3.19"
ENGINE = "AGENT_SUPERVISOR"

DEFAULT_FILES = {
    "market_state": Path(r"regime_output\market_state\market_state_snapshot.json"),
    "signal_decision": Path(r"regime_output\signal_decision\signal_decision.json"),
    "risk_gate": Path(r"regime_output\risk_gate\risk_gate.json"),
    "position_state": Path(r"regime_output\position_state\position_state.json"),
    "paper_execution": Path(r"regime_output\paper_execution\paper_execution.json"),
    "ledger": Path(r"regime_output\ledger\execution_decision_ledger.json"),
    "kill_switch": Path(r"regime_output\kill_switch\kill_switch_state.json"),
}

# These are intentionally conservative. Unknown/missing fields are unhealthy.
REQUIRED_HEALTH_KEYS = {
    "market_state": ("healthy", "market_state_healthy"),
    "signal_decision": ("healthy", "signal_state_healthy"),
    "risk_gate": ("healthy", "risk_gate_healthy"),
    "position_state": ("healthy", "position_state_healthy"),
    "paper_execution": ("healthy", "execution_state_healthy"),
    "ledger": ("healthy", "ledger_healthy"),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_json(obj: Any) -> str:
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def load_json(path: Path) -> Tuple[Dict[str, Any] | None, str | None]:
    if not path.exists():
        return None, f"FILE_NOT_FOUND:{path}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"INVALID_JSON:{path}:{exc}"
    if not isinstance(data, dict):
        return None, f"INVALID_ROOT_TYPE:{path}"
    return data, None


def get_nested(data: Dict[str, Any], *keys: str) -> Any:
    cur: Any = data
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def first_value(data: Dict[str, Any], names: Tuple[str, ...]) -> Any:
    for name in names:
        if name in data:
            return data[name]
    return None


def component_health(name: str, data: Dict[str, Any]) -> Tuple[bool, str]:
    candidates = REQUIRED_HEALTH_KEYS.get(name, ())
    value = first_value(data, candidates)
    if value is None:
        # Accept common status forms only when explicitly healthy.
        status = first_value(data, ("status", "engine_status", "state"))
        if isinstance(status, str) and status.upper() in {"HEALTHY", "RUNNING", "OK", "READY", "RECORDED"}:
            return True, f"STATUS_{status.upper()}"
        return False, "HEALTH_FIELD_MISSING"
    if value is True:
        return True, "HEALTHY_TRUE"
    if isinstance(value, str) and value.upper() in {"TRUE", "HEALTHY", "OK", "RUNNING", "READY"}:
        return True, "HEALTHY_STRING"
    return False, f"HEALTH_NOT_TRUE:{value}"


def kill_switch_safe(data: Dict[str, Any]) -> Tuple[bool, str]:
    # Safe means execution remains blocked. The supervisor must never infer
    # permission from a healthy supervisor state alone.
    execution_permitted = first_value(data, ("execution_permitted",))
    orders_allowed = first_value(data, ("orders_allowed",))
    live_execution = first_value(data, ("live_execution",))

    if execution_permitted is True:
        return False, "EXECUTION_ALREADY_PERMITTED"
    if orders_allowed is True:
        return False, "ORDERS_ALREADY_ALLOWED"
    if live_execution is True:
        return False, "LIVE_EXECUTION_ENABLED"

    return True, "EXECUTION_BARRIER_INTACT"


def evaluate(
    files: Dict[str, Path],
    fixture: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    loaded: Dict[str, Dict[str, Any]] = {}
    failures: List[str] = []

    if fixture is not None:
        for name, data in fixture.get("components", {}).items():
            if isinstance(data, dict):
                loaded[name] = data

    for name, path in files.items():
        if name in loaded:
            continue
        data, err = load_json(path)
        if err:
            failures.append(f"{name}:{err}")
        else:
            loaded[name] = data  # type: ignore[assignment]

    health: Dict[str, Dict[str, Any]] = {}
    for name in REQUIRED_HEALTH_KEYS:
        data = loaded.get(name)
        if data is None:
            health[name] = {"healthy": False, "reason": "INPUT_UNAVAILABLE"}
            failures.append(f"{name}:INPUT_UNAVAILABLE")
            continue
        ok, reason = component_health(name, data)
        health[name] = {"healthy": ok, "reason": reason}
        if not ok:
            failures.append(f"{name}:{reason}")

    ks = loaded.get("kill_switch")
    if ks is not None:
        ok, reason = kill_switch_safe(ks)
    else:
        # Missing kill-switch state is itself a fail-closed condition.
        ok, reason = False, "KILL_SWITCH_STATE_UNAVAILABLE"
    health["kill_switch"] = {"healthy": ok, "reason": reason}
    if not ok:
        failures.append(f"kill_switch:{reason}")

    # Optional cross-component consistency checks.
    source_versions = {
        name: first_value(data, ("agent_version", "source_engine", "engine_version"))
        for name, data in loaded.items()
    }

    execution_permitted = False
    execution_blocked = True
    overall = "SUPERVISOR_HEALTHY" if not failures else "SUPERVISOR_DEGRADED"

    return {
        "agent_version": VERSION,
        "engine": ENGINE,
        "generated_at": utc_now(),
        "mode": "AUDIT_ONLY",
        "fail_closed": True,
        "supervisor_status": overall,
        "execution_permitted": execution_permitted,
        "execution_blocked": execution_blocked,
        "halt_required": bool(failures),
        "failure_count": len(failures),
        "failures": failures,
        "component_health": health,
        "source_versions": source_versions,
        "guardrails": {
            "strategy_mutation": False,
            "signal_recalculation": False,
            "risk_recalculation": False,
            "position_override": False,
            "ledger_bypass": False,
            "market_data_fetch": False,
            "real_orders": False,
            "live_execution": False,
            "execution_permission": False,
            "fail_closed": True,
        },
    }


def write_output(result: Dict[str, Any], path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(result)
    payload["record_hash"] = sha256_json(payload)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload["record_hash"]


def enforce_kill_switch(reason: str) -> Tuple[bool, str]:
    """
    Explicit opt-in only. Uses the already-established .18 CLI contract.
    This is deliberately never enabled by default.
    """
    script = Path("aura_v05318_kill_switch_recovery.py")
    if not script.exists():
        return False, "KILL_SWITCH_SCRIPT_NOT_FOUND"

    cmd = [sys.executable, str(script), "--halt", "--reason", reason]
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except Exception as exc:
        return False, f"KILL_SWITCH_INVOCATION_ERROR:{exc}"

    if completed.returncode != 0:
        return False, f"KILL_SWITCH_HALT_FAILED:RC={completed.returncode}"

    return True, "KILL_SWITCH_HALT_REQUESTED"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=VERSION)
    p.add_argument("--once", action="store_true", help="Run one supervisory audit.")
    p.add_argument("--status", action="store_true", help="Show the latest supervisor result.")
    p.add_argument("--fixture", type=Path, help="Use a deterministic JSON test fixture.")
    p.add_argument("--output", type=Path, default=Path(r"regime_output\supervisor\agent_supervisor.json"))
    p.add_argument("--enforce-kill-switch", action="store_true",
                   help="On supervisor failure, explicitly request .18 HALT. Default is audit-only.")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if args.status:
        data, err = load_json(args.output)
        if err:
            print(f"ENGINE ERROR\n{err}")
            return 1
        print(f"{VERSION} — AGENT SUPERVISOR")
        print(f"STATUS               : {data.get('supervisor_status')}")
        print(f"EXECUTION PERMITTED  : {data.get('execution_permitted')}")
        print(f"EXECUTION BLOCKED    : {data.get('execution_blocked')}")
        print(f"HALT REQUIRED        : {data.get('halt_required')}")
        print(f"FAILURE COUNT        : {data.get('failure_count')}")
        print(f"OUTPUT               : {args.output}")
        return 0

    fixture = None
    if args.fixture:
        fixture, err = load_json(args.fixture)
        if err:
            print(f"ENGINE ERROR\n{err}")
            return 1

    result = evaluate(DEFAULT_FILES, fixture=fixture)

    if args.enforce_kill_switch and result["halt_required"]:
        ok, reason = enforce_kill_switch("SUPERVISOR_FAIL_CLOSED")
        result["kill_switch_enforcement"] = {
            "requested": True,
            "success": ok,
            "reason": reason,
        }
        if not ok:
            result["failures"].append(f"kill_switch_enforcement:{reason}")
            result["failure_count"] = len(result["failures"])
    else:
        result["kill_switch_enforcement"] = {
            "requested": False,
            "success": False,
            "reason": "AUDIT_ONLY",
        }

    record_hash = write_output(result, args.output)

    print("=" * 88)
    print(f"{VERSION} — AGENT SUPERVISOR")
    print("=" * 88)
    print(f"MODE                 : {result['mode']}")
    print(f"SUPERVISOR STATUS    : {result['supervisor_status']}")
    print(f"EXECUTION PERMITTED  : {result['execution_permitted']}")
    print(f"EXECUTION BLOCKED    : {result['execution_blocked']}")
    print(f"HALT REQUIRED        : {result['halt_required']}")
    print(f"FAILURES             : {result['failure_count']}")
    if result["failures"]:
        print("\nFAILURES")
        print("-" * 88)
        for failure in result["failures"]:
            print(f"  - {failure}")
    print("\nGUARDRAILS")
    print("-" * 88)
    print("STRATEGY MUTATION    : FALSE")
    print("SIGNAL RECALCULATION : FALSE")
    print("RISK RECALCULATION   : FALSE")
    print("POSITION OVERRIDE    : FALSE")
    print("LEDGER BYPASS        : FALSE")
    print("REAL ORDERS          : FALSE")
    print("LIVE EXECUTION       : FALSE")
    print("EXECUTION PERMISSION : FALSE")
    print("FAIL-CLOSED          : TRUE")
    print(f"\nRECORD HASH          : {record_hash}")
    print(f"OUTPUT               : {args.output}")
    print("=" * 88)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
