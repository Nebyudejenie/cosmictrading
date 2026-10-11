# 00 — Executive Summary

**COSMICTRADING QUANT** is a single-operator, evidence-driven crypto research and human-approved trading system. It researches markets with deterministic analytics and free cloud AI, proposes trades with full evidence, costs and risks, asks the operator to approve the **exact** trade, and then executes and manages only that trade — today against a clearly labelled paper broker, because no live broker has been selected.

## Where things stand (2026-10-12, end of S10)

- **Built and verified locally:** paper trading end to end — keyless delayed market data (Coinbase + Bitstamp) → research missions → proposal with a hashed packet → human approval (portal; Telegram tested with mocked updates) → execution against the paper venue → fills, protective stops, reconciliation → kill switch and checklist recovery — plus the free-only AI gateway (tested with a fake provider), the portal, encrypted backups and a restore drill. `make check`: 373 tests pass; `make scan`: no known vulnerable dependencies, bandit clean. The production Compose stack was run locally and driven through the real portal, including worker SIGKILLs and a full restart (doc 12 §7).
- **Not done, because access is missing:** no Proxmox VM (B-02), no domain/tunnel/DNS (B-03), no AI keys (B-04), no Telegram bot (B-05), no broker (B-06/B-07). Live trading is unreachable by construction and every live go-live gate is BLOCKED.
- The baseline vision the prompt references (`cosmictrading.md`) still does not exist (B-01).
- Requirements: 301 traced; 237 IMPLEMENTED, 26 DONE, 25 PARTIAL, 4 DEFERRED, 8 BLOCKED, 1 N/A (doc 02). Latest session report: `reports/2026-10-11-session-report.md`.

## Design in one paragraph

A modular monolith in Python (FastAPI, PostgreSQL 17) runs as `web` (portal + API), `worker` (jobs and the single execution authority) and `bot` (Telegram long polling). All money is `Decimal`/`NUMERIC`. Risk checks are pure deterministic functions; approvals are bound to a hashed packet, single-use and expiring; orders follow a broker-grounded state machine where timeouts become `UNKNOWN_ORDER_STATE` and are reconciled, never resubmitted. Audit is append-only and hash-chained in the database. The AI gateway calls only allowlisted free routes after owner attestation, fails closed, and is not importable from trading code. Market data comes from keyless Coinbase Exchange and Bitstamp endpoints, labelled `DELAYED`. Deployment is Docker Compose on a dedicated Ubuntu VM with Cloudflare Tunnel + Access in front of application login and MFA.

## What "done" means for this release

Paper trading end to end — data → research mission → proposal → human approval → execution → fills → reconciliation → kill switch → recovery — with the 20 acceptance tests of V6 §1831 passing (VM-level restart excepted until access exists), a locally verified production stack, a real restore drill, and honest status for every external dependency. Live trading remains unreachable by construction until a broker is chosen, every go-live gate passes and the owner activates it.

## Document map

| Doc | Topic |
|---|---|
| 01 | Current-state audit |
| 02 | Requirements traceability (301 requirements) |
| 03 | Scope, non-goals, roadmap (slices S1–S10) |
| 04 | System architecture |
| 05 | Data architecture and ERD |
| 06 | AI providers and workforce |
| 07 | Market data and research |
| 08 | Trading approval and execution |
| 09 | Threat model |
| 10 | API and integration contracts |
| 11 | UI and Telegram |
| 12 | Proxmox and Cloudflare deployment |
| 13 | Testing and acceptance |
| 14 | Observability, backup, recovery |
| 15 | Operations runbook |
| 16 | Cost and resource budget |
| 18 | User guide (operator) |
| 17-ADRs | Decisions |
| diagrams | Mermaid diagrams |
| BLOCKERS.md | External dependencies |
| IMPLEMENTATION-STATUS.md | Evidence log |
| reports/ | Session reports |
