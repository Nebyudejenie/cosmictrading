# ADR-009: Append-only, hash-chained audit enforced in the database
- **Status:** Accepted · **Date:** 2026-10-10
- **Decision:** `audit_events` INSERT-only for the app role; triggers reject UPDATE/DELETE/TRUNCATE for every role; a BEFORE INSERT trigger serialises with a transaction advisory lock and computes `seq`, `prev_hash`, `hash` (SHA-256). `audit_verify()` recomputes the chain; an hourly job raises a CRITICAL incident on a break. Other immutable tables (order events, fills, ledger, packets, transitions, risk check runs) use the same trigger without the chain.
- **Trade-off:** audit inserts serialise until commit; mitigated by writing audit last in each transaction. A superuser can still alter data; the chain makes that evident, and backups preserve history.
