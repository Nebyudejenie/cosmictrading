# ADR-007: Decimal arithmetic and NUMERIC(38,18) for all financial values
- **Status:** Accepted · **Date:** 2026-10-10
- **Decision:** `decimal.Decimal` (context precision 50, explicit quantisation with `ROUND_DOWN`/`ROUND_UP` where direction matters) and `NUMERIC(38,18)` columns for prices, quantities, fees, balances and ledger amounts. Parsing goes string → Decimal; floats are rejected by the money helpers.
- **Reason:** crypto quantities need ≥ 8 decimals (integer minor units per asset would need per-asset scales and complicate cross-asset maths).
- **Consequences:** a schema test asserts no float columns in financial tables; analytics (indicators, backtest statistics) may use float and are labelled as estimates.
