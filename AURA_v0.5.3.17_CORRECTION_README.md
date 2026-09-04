# AURA v0.5.3.17 — Execution & Decision Ledger

## Purpose

The Execution & Decision Ledger is the audit boundary after Paper Execution.

It records the complete AURA decision chain:

v0.5.3.12 Market State
→ v0.5.3.13 Signal Decision
→ v0.5.3.14 Risk Gate
→ v0.5.3.15 Position State
→ v0.5.3.16 Paper Execution
→ v0.5.3.17 Execution & Decision Ledger

The Ledger is audit-only. It does not create signals, make risk decisions,
manage positions, or execute orders.

## Actual input files

- `regime_output\market_state\market_state_snapshot.json`
- `regime_output\signal_decision\signal_decision.json`
- `regime_output\risk_gate\risk_gate.json`
- `regime_output\position_state\position_state.json`
- `regime_output\paper_execution\paper_execution.json`

## Output

- `regime_output\ledger\execution_decision_ledger.json`

## What is recorded

For each symbol, the Ledger records:

- Market State information
- Signal Decision information
- Risk Gate information
- Position state and position decision
- Paper Execution information
- Upstream declared versions
- Upstream state/input/result hashes
- Full raw upstream artifacts
- Event ID
- Sequence number
- Previous ledger record hash
- Current ledger record hash
- Safety guardrails

This preserves the evidence needed to answer what AURA decided and why.

## Schema correction

The original v0.5.3.17 implementation assumed the Market State file was:

`market_state.json`

The actual AURA file is:

`market_state_snapshot.json`

The corrected implementation uses the actual filename and the actual
`symbols`, `decisions`, and `positions` structures used by the existing
v0.5.3.12-v0.5.3.16 artifacts.

## Existing previous test

The first v0.5.3.17 test produced an incompatible ledger format.

That file should be preserved as:

`regime_output\ledger\execution_decision_ledger_v05317_previous_test.json`

The corrected implementation must not append new records to that old format.

## Safety

- Real orders: DISABLED
- Live execution: DISABLED
- Paper execution in this module: RECORD ONLY
- Risk decisions: DISABLED
- Market-data fetching: DISABLED
- Strategy mutation: DISABLED
- Fail-closed policy: ENABLED

An upstream BLOCKED or NO_ACTION state is valid audit information and should
still be recorded by the Ledger. The Ledger must not hide a blocked decision.

## Important

Do not modify v0.5.3.12-v0.5.3.16.

Do not rename or move their output files.

The Ledger is downstream and records the existing chain as supplied.
