# ADR-005: PostgreSQL as the only datastore; queue in Postgres; no Redis
- **Status:** Accepted · **Date:** 2026-10-10
- **Decision:** PostgreSQL 17 holds state, audit, cache and the job queue (`SELECT … FOR UPDATE SKIP LOCKED`). Advisory locks provide the execution leader and audit chain serialisation.
- **Reason:** transactional integrity across order state, ledger and audit in one database; job volume is tiny; one less service on a 10 GB VM.
- **Revisit when:** queue latency or DB load measurements show contention.
