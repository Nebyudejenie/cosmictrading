# Repository rules for engineers and coding agents

Read before changing anything:

1. `COSMICTRADING_QUANT_MASTER_BUILD_PROMPT.md` — product vision and execution directive (governs).
2. `docs/IMPLEMENTATION-STATUS.md` — what exists and what was verified.
3. `docs/BLOCKERS.md` — external dependencies; never work around them by inventing credentials, hosts, domains, broker APIs or results.
4. `docs/02-requirements-traceability.md` — update the State column when a requirement changes state.

Hard rules:

- **Never** enable live trading, add a live broker adapter without its sandbox contract tests, or raise the authority mode as a side effect of anything.
- **Never** let `cosmic.trading`, `cosmic.audit` or `cosmic.identity` import `cosmic.ai` or `cosmic.workforce` (enforced by `tests/unit/test_architecture.py`).
- AI: free cloud routes only (`FREE_CLOUD_AI_ONLY`), $0 budget, fail closed. No Ollama, no local models, no paid fallback.
- Money is `Decimal` / `NUMERIC(38,18)`. Never `float` in financial code.
- Secrets live only in secret files or `.env` (gitignored). Never print, log, commit or send them to AI/Telegram/the browser.
- Schema changes only through Alembic migrations in `src/cosmic/db/migrations/versions/`, with a tested `downgrade()`.
- Append-only tables stay append-only. Never weaken the triggers or grants.
- Simulated values are labelled `SIMULATED`; delayed data `DELAYED`; never present either as live.
- `make check` must pass before a commit. Report real results only.
- Do not touch other projects' containers, volumes, tunnels or ports on this machine. Dev ports: Postgres 127.0.0.1:55442, web 127.0.0.1:8742.
- Destructive operations (dropping data, DNS changes, VM changes) require explicit owner authorisation and a rollback plan.
