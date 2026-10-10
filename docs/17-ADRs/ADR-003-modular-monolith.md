# ADR-003: Modular monolith, three processes
- **Status:** Accepted · **Date:** 2026-10-10
- **Decision:** one package `cosmic`, one image, processes `web`, `worker`, `bot`. Module boundaries are enforced by import-contract tests.
- **Alternatives:** microservices (rejected: operational cost on one 10 GB VM, no independent scaling need); single process (rejected: execution and request handling must not interfere; the bot is optional).
- **Consequences:** simple deploy; the worker can later be split by job class if measurements show contention.
