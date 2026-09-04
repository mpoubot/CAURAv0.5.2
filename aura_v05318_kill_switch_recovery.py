#!/usr/bin/env python3
"""AURA v0.5.3.18 — Kill Switch / Recovery
Deterministic global execution-safety controller.
"""
from __future__ import annotations
import argparse, hashlib, json, sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = "0.5.3.18"
DEFAULT_STATE_FILE = Path("aura_v05318_kill_switch_state.json")
VALID_STATES = {"RUNNING", "HALTED", "RECOVERY_PENDING"}
REQUIRED_CHECKS = (
    "market_state_healthy", "signal_state_healthy", "risk_gate_healthy",
    "position_state_healthy", "execution_state_healthy", "ledger_healthy",
    "no_unresolved_execution",
)

def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00","Z")

def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",",":"), ensure_ascii=False)

def digest(obj):
    return hashlib.sha256(canonical(obj).encode()).hexdigest()

def default_state():
    return {
        "schema_version":"AURA-KS-1", "aura_version":VERSION,
        "component":"KILL_SWITCH_RECOVERY", "state":"RUNNING",
        "kill_switch":False, "halt_reason":None, "halted_at":None,
        "recovery_requested_at":None, "recovered_at":None,
        "last_transition":{"event":"INITIALIZE","timestamp":utc_now(),"reason":"DEFAULT_SAFE_START"},
        "execution_barrier":{
            "orders_allowed":False, "paper_execution":False,
            "live_execution":False, "execution_permitted":False
        }
    }

def load_state(path):
    if not path.exists():
        return default_state()
    try:
        data=json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data,dict):
            raise ValueError("state is not an object")
    except Exception:
        s=default_state()
        s.update(state="HALTED",kill_switch=True,halt_reason="STATE_FILE_INVALID",
                 halted_at=utc_now(),
                 last_transition={"event":"FAIL_CLOSED","timestamp":utc_now(),"reason":"STATE_FILE_INVALID"})
        return s
    if data.get("state") not in VALID_STATES:
        data["state"]="HALTED"; data["kill_switch"]=True
        data["halt_reason"]="INVALID_STATE"
    return data

def enforce(state):
    # v0.5.3.18 hard execution barrier: all execution remains OFF.
    state["execution_barrier"]={
        "orders_allowed":False,"paper_execution":False,
        "live_execution":False,"execution_permitted":False
    }
    state["execution_blocked"]=True
    return state

def save_state(path,state):
    state=enforce(state)
    unsigned={k:v for k,v in state.items() if k!="state_hash"}
    state["state_hash"]=digest(unsigned)
    tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(state,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")
    tmp.replace(path)

def do_halt(path,reason):
    s=load_state(path); now=utc_now()
    s.update(state="HALTED",kill_switch=True,halt_reason=reason,
             halted_at=s.get("halted_at") or now,recovery_requested_at=None,recovered_at=None)
    s["last_transition"]={"event":"HALT","timestamp":now,"reason":reason}
    save_state(path,s); return s

def recovery_checks(check_file):
    try:
        obj=json.loads(check_file.read_text(encoding="utf-8"))
    except Exception:
        return False,list(REQUIRED_CHECKS)
    if not isinstance(obj,dict):
        return False,list(REQUIRED_CHECKS)
    bad=[k for k in REQUIRED_CHECKS if obj.get(k) is not True]
    return not bad,bad

def do_recover(path,check_file):
    s=load_state(path); now=utc_now()
    if s.get("state")!="HALTED" or s.get("kill_switch") is not True:
        s["last_transition"]={"event":"RECOVERY_BLOCKED","timestamp":now,"reason":"SYSTEM_NOT_HALTED"}
        save_state(path,s); return s
    ok,bad=recovery_checks(check_file)
    s["recovery_requested_at"]=now
    if not ok:
        s.update(state="RECOVERY_PENDING",kill_switch=True)
        s["last_transition"]={"event":"RECOVERY_BLOCKED","timestamp":now,
                               "reason":"RECOVERY_CHECK_FAILED","failed_checks":bad}
    else:
        s.update(state="RUNNING",kill_switch=False,halt_reason=None,recovered_at=now)
        s["last_transition"]={"event":"RECOVERED","timestamp":now,"reason":"ALL_RECOVERY_CHECKS_PASSED"}
    save_state(path,s); return s

def show(s):
    b=s["execution_barrier"]
    print(f"AURA v{VERSION} — KILL SWITCH / RECOVERY")
    print(f"STATE                : {s.get('state')}")
    print(f"KILL SWITCH          : {'ON' if s.get('kill_switch') else 'OFF'}")
    print(f"HALT REASON          : {s.get('halt_reason')}")
    print(f"ORDERS ALLOWED       : {b['orders_allowed']}")
    print(f"PAPER EXECUTION      : {b['paper_execution']}")
    print(f"LIVE EXECUTION       : {b['live_execution']}")
    print(f"EXECUTION PERMITTED  : {b['execution_permitted']}")
    print(f"EXECUTION BLOCKED    : {s.get('execution_blocked')}")
    print(f"LAST TRANSITION      : {s.get('last_transition')}")
    print(f"STATE HASH           : {s.get('state_hash','NOT_PERSISTED')}")

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--state-file",default=str(DEFAULT_STATE_FILE))
    p.add_argument("--status",action="store_true")
    p.add_argument("--halt",action="store_true")
    p.add_argument("--reason",default="MANUAL_HALT")
    p.add_argument("--recover",action="store_true")
    p.add_argument("--check-file")
    a=p.parse_args()
    actions=sum((a.status,a.halt,a.recover))
    if actions>1: p.error("Choose only one of --status, --halt, --recover")
    path=Path(a.state_file)
    if a.halt: show(do_halt(path,a.reason)); return 0
    if a.recover:
        if not a.check_file: p.error("--recover requires --check-file")
        show(do_recover(path,Path(a.check_file))); return 0
    s=load_state(path); save_state(path,s); show(s); return 0

if __name__=="__main__":
    sys.exit(main())
