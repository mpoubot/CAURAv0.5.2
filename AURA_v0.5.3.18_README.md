# AURA v0.5.3.18 — Kill Switch / Recovery

## Purpose
A deterministic global safety controller. It is not a strategy or execution engine.

## Core rule
**If AURA is uncertain, it does not trade.**

## States
- `RUNNING`
- `HALTED`
- `RECOVERY_PENDING`

A persisted `HALTED` state survives restart. Missing or corrupt state fails closed.

## Recovery
Recovery is explicit and deterministic. The system must be halted, all required recovery checks must pass, and only then may the kill switch clear. Even after recovery, execution remains disabled in this build.

## Hard execution barrier
```text
orders_allowed      = false
paper_execution     = false
live_execution      = false
execution_permitted = false
```

## Recovery checks
- market_state_healthy
- signal_state_healthy
- risk_gate_healthy
- position_state_healthy
- execution_state_healthy
- ledger_healthy
- no_unresolved_execution

Any missing, false, malformed, or unknown check blocks recovery.

## Does not do
This component does not generate signals, calculate indicators, size positions, submit/cancel orders, invent position state, override Risk Gate, or enable paper/live execution.

## Acceptance goals
1. Safe initial state.
2. Manual halt -> `HALTED`.
3. Halt persists across restart.
4. Halt blocks execution.
5. Failed recovery remains blocked.
6. Successful recovery clears the kill switch.
7. Successful recovery still leaves execution disabled.
8. Corrupt state fails closed.
