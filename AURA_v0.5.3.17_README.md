# AURA v0.5.3.17 — Execution & Decision Ledger

The ledger is the immutable audit boundary after v0.5.3.16 Paper Execution.

It records the chain:

v0.5.3.12 Market State
→ v0.5.3.13 Signal Decision
→ v0.5.3.14 Risk Gate
→ v0.5.3.15 Position State
→ v0.5.3.16 Paper Execution
→ v0.5.3.17 Ledger

The ledger does not make trading decisions and does not execute anything.

## Recorded information

Each symbol record includes:
- market state snapshot
- signal snapshot
- risk-gate snapshot
- position before / after
- position-state snapshot
- paper execution orders/events
- source versions
- source content hashes
- previous record hash
- record hash
- deterministic event ID

## Safety

Real orders: disabled.
Live execution: disabled.
Paper execution: record-only in this module.
Risk decisions: disabled.
Market-data fetching: disabled.
Fail-closed: enabled.

The ledger is append-only and deduplicates event IDs. Up to 5,000 records
are retained in the JSON ledger file.

## Default paths

Inputs:
- regime_output/market_state/market_state.json
- regime_output/signal_decision/signal_decision.json
- regime_output/risk_gate/risk_gate.json
- regime_output/position_state/position_state.json
- regime_output/paper_execution/paper_execution.json

Output:
- regime_output/ledger/execution_decision_ledger.json

The module records upstream objects as supplied. It does not recalculate
market state, signals, risk, or execution.
