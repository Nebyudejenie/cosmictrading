# COSMICTRADING QUANT

Evidence-driven crypto research with a human-approved trading engine. The system researches markets, proposes trades with evidence, costs and risks, and executes **only the exact trade the operator approves**, within deterministic risk limits.

**Current state:** paper trading only. Live order submission is disabled by construction: no live broker is configured and the go-live gates have not passed. See `docs/IMPLEMENTATION-STATUS.md` and `docs/BLOCKERS.md`.

## Quick start (development)

Requirements: Python 3.12, [uv](https://docs.astral.sh/uv/), Docker with Compose.

```bash
make setup      # install dependencies, create .env with random local secrets
make db-up      # PostgreSQL 17 on 127.0.0.1:55442
make migrate
uv run cosmic admin create-owner
make run-web    # http://127.0.0.1:8742
make run-worker
make check      # format, lint, types, secret scan, tests
```

## Documentation

Start with `docs/00-executive-summary.md`. Design decisions are in `docs/17-ADRs/`. Rules for contributors and coding agents are in `AGENTS.md`.

## Safety summary

- Free cloud AI only; $0 inference budget; no local models.
- Every new position needs explicit, authenticated, single-use approval of an exact, hashed trade packet.
- Risk checks are deterministic; unknown data, stale prices or unreconciled accounts block new exposure.
- Orders whose state is uncertain are reconciled, never blindly resubmitted.
- Audit history is append-only and hash-chained.
- Simulated values are always labelled `SIMULATED`.
