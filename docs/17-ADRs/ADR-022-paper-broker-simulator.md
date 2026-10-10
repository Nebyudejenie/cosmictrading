# ADR-022: PaperBroker as an independent simulated venue
- **Status:** Accepted · **Date:** 2026-10-10
- **Decision:** the paper broker keeps its own order/fill/balance tables, fills only against fresh ingested quotes, models spread/slippage/fees (labelled ASSUMPTION) and partial fills, supports fault injection only in LOCAL/TEST, and implements the same adapter contract a live adapter must pass.
- **Consequences:** reconciliation is exercised against a genuinely separate record; paper results are never presented as live performance.
