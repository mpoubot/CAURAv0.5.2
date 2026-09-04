#!/usr/bin/env python3
"""AURA v0.5.3.20 - deterministic read-only competition dashboard."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = "AURA v0.5.3.20"

FILES = {
    "market_state": "regime_output/market_state/market_state.json",
    "signal_decision": "regime_output/signal_decision/signal_decision.json",
    "risk_gate": "regime_output/risk_gate/risk_gate.json",
    "position_state": "regime_output/position_state/position_state.json",
    "paper_execution": "regime_output/paper_execution/paper_execution.json",
    "ledger": "regime_output/ledger/decision_execution_ledger.json",
    "kill_switch": "regime_output/kill_switch/kill_switch.json",
    "supervisor": "regime_output/supervisor/agent_supervisor.json",
}

# Health fields are component-specific. Missing/unknown values are NOT healthy.
HEALTH = {
    "market_state": ("market_state_healthy", "healthy", "status"),
    "signal_decision": ("signal_state_healthy", "healthy", "status"),
    "risk_gate": ("risk_gate_healthy", "healthy", "status"),
    "position_state": ("position_state_healthy", "healthy", "status"),
    "paper_execution": ("execution_state_healthy", "healthy", "status"),
    "ledger": ("ledger_healthy", "healthy", "status"),
    "supervisor": ("supervisor_status", "healthy", "status"),
}


def now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def load(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.exists():
        return None, f"FILE_NOT_FOUND:{path}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"INVALID_JSON:{path}:{exc}"
    if not isinstance(data, dict):
        return None, f"INVALID_ROOT_TYPE:{path}"
    return data, None


def first(data: dict[str, Any] | None, keys: tuple[str, ...]) -> Any:
    if not isinstance(data, dict):
        return None
    for key in keys:
        if key in data:
            return data[key]
    return None


def health(name: str, data: dict[str, Any] | None) -> tuple[str, str]:
    if data is None:
        return "UNAVAILABLE", "INPUT_UNAVAILABLE"

    value = first(data, HEALTH.get(name, ("healthy", "status")))
    if value is None:
        return "UNKNOWN", "HEALTH_FIELD_MISSING"

    if isinstance(value, bool):
        return ("HEALTHY", "HEALTHY_TRUE") if value else ("UNHEALTHY", "HEALTH_FALSE")

    if isinstance(value, str):
        upper = value.upper()

        if name == "supervisor":
            if upper == "SUPERVISOR_HEALTHY":
                return "HEALTHY", upper
            if upper.startswith("SUPERVISOR_"):
                return "UNHEALTHY", upper

        if upper in {"TRUE", "HEALTHY", "RUNNING", "OK", "READY", "RECORDED"}:
            return "HEALTHY", upper

        if upper in {"FALSE", "UNHEALTHY", "ERROR", "FAILED"}:
            return "UNHEALTHY", upper

        if upper == "HALTED":
            return "UNHEALTHY", upper

        if upper == "BLOCKED":
            return "HEALTHY", "BLOCKED_SAFE"

    return "UNKNOWN", f"UNRECOGNIZED_HEALTH_VALUE:{value}"


def kill_switch_health(data: dict[str, Any] | None) -> tuple[str, str]:
    """
    The kill switch is a safety barrier, not an execution-enable flag.

    A healthy/safe kill switch normally has:
      execution_permitted = False
      orders_allowed      = False
      live_execution      = False

    Therefore those FALSE values must not by themselves make the component
    unhealthy. The dashboard only reports the observed state and never changes it.
    """
    if data is None:
        return "UNAVAILABLE", "INPUT_UNAVAILABLE"

    execution_permitted = first(data, ("execution_permitted",))
    orders_allowed = first(data, ("orders_allowed",))
    live_execution = first(data, ("live_execution",))
    state = first(data, ("state", "status"))

    # Fail closed if any execution-enabling field is TRUE.
    if execution_permitted is True:
        return "UNHEALTHY", "EXECUTION_ALREADY_PERMITTED"
    if orders_allowed is True:
        return "UNHEALTHY", "ORDERS_ALREADY_ALLOWED"
    if live_execution is True:
        return "UNHEALTHY", "LIVE_EXECUTION_ENABLED"

    if isinstance(state, str):
        upper = state.upper()
        if upper == "HALTED":
            return "HEALTHY", "HALTED_SAFE"
        if upper in {"RUNNING", "RECOVERED", "SAFE", "OFF"}:
            return "HEALTHY", upper

    # Explicitly blocked execution with no enabling flags is safe.
    if (
        execution_permitted is False
        and orders_allowed is False
        and live_execution is False
    ):
        return "HEALTHY", "EXECUTION_BARRIER_INTACT"

    return "UNKNOWN", "KILL_SWITCH_STATE_UNCERTAIN"


def summary_value(
    data: dict[str, Any] | None, keys: tuple[str, ...]
) -> Any:
    """Return a field if present; never raise KeyError."""
    return first(data, keys)


def make(root: Path) -> dict[str, Any]:
    loaded: dict[str, dict[str, Any] | None] = {}
    errors: dict[str, str] = {}
    components: dict[str, dict[str, str]] = {}

    for name, relative_path in FILES.items():
        data, error = load(root / relative_path)
        loaded[name] = data
        if error:
            errors[name] = error

        if name == "kill_switch":
            status, reason = kill_switch_health(data)
        else:
            status, reason = health(name, data)

        components[name] = {"status": status, "reason": reason}

    failures = [
        f"{name}:{item['reason']}"
        for name, item in components.items()
        if item["status"] not in {"HEALTHY"}
    ]

    # v0.5.3.20 is observational only. These values are hard barriers.
    competition = {
        "paper_only": True,
        "orders_allowed": False,
        "live_execution": False,
        "execution_permitted": False,
    }

    execution_barrier = {
        "dashboard_can_authorize": False,
        "execution_permitted": False,
        "execution_blocked": True,
        "orders_allowed": False,
        # This means "competition mode is paper-only", not that the paper
        # execution engine has actually executed or is enabled.
        "paper_execution": False,
        "live_execution": False,
    }

    supervisor_data = loaded.get("supervisor")
    kill_switch_data = loaded.get("kill_switch")

    # .14 contract: overall_risk_decision is preferred. Do not require
    # risk_decision at the top level.
    summary = {
        "market_state": summary_value(
            loaded.get("market_state"),
            ("state", "market_state", "market_regime"),
        ),
        "signal_decision": summary_value(
            loaded.get("signal_decision"),
            ("decision", "signal", "signal_decision"),
        ),
        "risk_decision": summary_value(
            loaded.get("risk_gate"),
            (
                "overall_risk_decision",
                "risk_decision",
                "decision",
                "risk_gate_decision",
            ),
        ),
        "position_state": summary_value(
            loaded.get("position_state"),
            ("state", "position_state"),
        ),
        "paper_execution_state": summary_value(
            loaded.get("paper_execution"),
            ("state", "execution_state"),
        ),
        "ledger_state": summary_value(
            loaded.get("ledger"),
            ("state", "ledger_status"),
        ),
    }

    out: dict[str, Any] = {
        "dashboard_version": VERSION,
        "generated_at": now(),
        "mode": "READ_ONLY_AUDIT",
        "competition": competition,
        "system": {
            "status": "HEALTHY" if not failures and not errors else "DEGRADED",
            "component_count": len(components),
            "healthy_count": sum(
                item["status"] == "HEALTHY" for item in components.values()
            ),
            "failure_count": len(failures) + len(errors),
            "failures": failures,
            "input_errors": errors,
        },
        "component_health": components,
        "execution_barrier": execution_barrier,
        "supervisor": {
            "status": summary_value(
                supervisor_data,
                ("supervisor_status", "status"),
            )
            if supervisor_data
            else "UNAVAILABLE",
            "halt_required": bool(
                summary_value(supervisor_data, ("halt_required",))
                if supervisor_data
                else True
            ),
            "failures": summary_value(supervisor_data, ("failures",))
            if supervisor_data
            else {},
        },
        "kill_switch": {
            "state": summary_value(
                kill_switch_data,
                ("state", "status"),
            )
            if kill_switch_data
            else "UNAVAILABLE",
            "halt_reason": summary_value(
                kill_switch_data,
                ("halt_reason",),
            )
            if kill_switch_data
            else None,
        },
        "summary": summary,
    }

    # Hash the complete deterministic payload before adding the hash itself.
    raw = json.dumps(
        out,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    out["record_hash"] = hashlib.sha256(raw).hexdigest()

    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    out = make(root)

    dashboard_dir = root / "regime_output" / "dashboard"
    dashboard_dir.mkdir(parents=True, exist_ok=True)

    json_path = dashboard_dir / "competition_dashboard.json"
    html_path = dashboard_dir / "competition_dashboard.html"

    json_path.write_text(
        json.dumps(out, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    rows = "".join(
        f"<tr><td>{html.escape(name)}</td>"
        f"<td>{html.escape(item['status'])}</td>"
        f"<td>{html.escape(item['reason'])}</td></tr>"
        for name, item in out["component_health"].items()
    )

    html_path.write_text(
        f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{html.escape(VERSION)}</title>
<style>
body{{font:16px Arial;margin:30px;background:#f5f7fa}}
section{{background:white;padding:20px;margin:15px 0;border-radius:8px}}
table{{border-collapse:collapse;width:100%}}
td,th{{padding:9px;border-bottom:1px solid #ddd;text-align:left}}
pre{{white-space:pre-wrap}}
</style>
</head>
<body>
<section>
<h1>{html.escape(VERSION)}</h1>
<p>READ_ONLY_AUDIT</p>
<h2>System: {html.escape(out["system"]["status"])}</h2>
<p>
Healthy: {out["system"]["healthy_count"]}/{out["system"]["component_count"]}
| Failures: {out["system"]["failure_count"]}
</p>
</section>

<section>
<h2>Execution Barrier</h2>
<b>
EXECUTION PERMITTED: FALSE |
ORDERS ALLOWED: FALSE |
PAPER EXECUTION: FALSE |
LIVE EXECUTION: FALSE
</b>
<p>Dashboard authority: NONE.</p>
</section>

<section>
<h2>Component Health</h2>
<table>
<tr><th>Component</th><th>Status</th><th>Reason</th></tr>
{rows}
</table>
</section>

<section>
<h2>Summary</h2>
<pre>{html.escape(json.dumps(out["summary"], indent=2, ensure_ascii=False))}</pre>
</section>

<section>
<h2>Record Hash</h2>
<pre>{html.escape(out["record_hash"])}</pre>
</section>
</body>
</html>
""",
        encoding="utf-8",
    )

    print("=" * 88)
    print(VERSION)
    print("=" * 88)
    print(f"MODE                  : {out['mode']}")
    print(f"SYSTEM STATUS         : {out['system']['status']}")
    print(
        f"HEALTHY COMPONENTS    : "
        f"{out['system']['healthy_count']}/{out['system']['component_count']}"
    )
    print(f"FAILURES              : {out['system']['failure_count']}")
    print("ORDERS ALLOWED        : False")
    print("PAPER EXECUTION       : False")
    print("LIVE EXECUTION        : False")
    print("EXECUTION PERMITTED   : False")
    print("EXECUTION BLOCKED     : True")
    print("\nCOMPONENT HEALTH")
    for name, item in out["component_health"].items():
        print(f"{name:22} : {item['status']:12} ({item['reason']})")
    print(f"\nJSON OUTPUT           : {json_path.relative_to(root)}")
    print(f"HTML OUTPUT           : {html_path.relative_to(root)}")
    print(f"RECORD HASH           : {out['record_hash']}")
    print("=" * 88)


if __name__ == "__main__":
    main()
