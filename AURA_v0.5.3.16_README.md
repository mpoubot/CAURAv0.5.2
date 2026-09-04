# AURA v0.5.3.16 — Paper Execution Adapter

This build is the execution simulation boundary between v0.5.3.15 Position
State Manager and the future v0.5.3.17 Decision/Execution Ledger.

## Hard boundaries

- No exchange or broker connection.
- No real order submission.
- No live execution.
- No signal generation.
- No risk decisions.
- No position sizing.
- No market-data fetching.
- No indicator recalculation.
- A fill is only simulated from an explicit execution intent plus explicit
  reference price and quantity.

## Normal chain

v0.5.3.14 Risk Gate
→ v0.5.3.15 Position State Manager
→ v0.5.3.16 Paper Execution Adapter
→ execution event
→ v0.5.3.15 Position State Manager

## Deterministic test

python aura_v05316_paper_execution_adapter.py ^
  --order aura_v05316_paper_order_test_fixture.json ^
  --timestamp 2026-08-28T12:00:00Z ^
  --spread-bps 2 ^
  --slippage-bps 1 ^
  --fee-bps 4 ^
  --latency-ms 100 ^
  --output paper_execution_test.json

Expected simulated BUY fill:

reference = 100000
half spread = 100000 * 2 / 10000 / 2 = 10
slippage = 100000 * 1 / 10000 = 10
fill = 100020

The resulting JSON contains a FILLED event in the exact event shape that
v0.5.3.15 already knows how to consume.

## Important

The real v0.5.3.15 state file should not be altered by this adapter. The
adapter produces an execution event; the Position State Manager remains the
authority that applies that event to local position state.
