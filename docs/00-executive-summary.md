# 00 — Executive Summary

**COSMICTRADING QUANT** is a single-operator, evidence-driven crypto research and human-approved trading system. It researches markets with deterministic analytics and free cloud AI, proposes trades with full evidence, costs and risks, asks the operator to approve the **exact** trade, and then executes and manages only that trade — today against a clearly labelled paper broker, because no live broker has been selected.

## Where things stand (2026-10-10)

- The repository was empty apart from the master prompt. The baseline vision it references (`cosmictrading.md`) does not exist (B-01).
- Owner decisions: crypto spot first; prepare and verify deployment locally (no Proxmox access yet, B-02); no domain yet (B-03).
- No AI keys, Telegram token or broker credentials exist (B-04 to B-07). Everything that depends on them is built and tested with fakes and stays switched off.

## Design in one paragraph

A modular monolith in Python (FastAPI, PostgreSQL 17) runs as `web` (portal + API), `worker` (jobs and the single execution authority) and `bot` (Telegram long polling). All money is `Decimal`/`NUMERIC`. Risk checks are pure deterministic functions; approvals are bound to a hashed packet, single-use and expiring; orders follow a broker-grounded state machine where timeouts become `UNKNOWN_ORDER_STATE` and are reconciled, never resubmitted. Audit is append-only and hash-chained in the database. The AI gateway calls only allowlisted free routes after owner attestation, fails closed, and is not importable from trading code. Market data comes from keyless Coinbase Exchange and Bitstamp endpoints, labelled `DELAYED`. Deployment is Docker Compose on a dedicated Ubuntu VM with Cloudflare Tunnel + Access in front of application login and MFA.

## What "done" will mean for this release

Paper trading end to end — data → research mission → proposal → human approval → execution → fills → reconciliation → kill switch → recovery — with the 20 acceptance tests of V6 §1831 passing (VM-level restart excepted until access exists), a locally verified production stack, a real restore drill, and honest status for every external dependency. Live trading remains unreachable by construction until a broker is chosen, every go-live gate passes and the owner activates it.

## Document map

| Doc | Topic |
|---|---|
| 01 | Current-state audit |
| 02 | Requirements traceability (≈230 requirements) |
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
| 17-ADRs | Decisions |
| diagrams | Mermaid diagrams |
| BLOCKERS.md | External dependencies |
| IMPLEMENTATION-STATUS.md | Evidence log |
