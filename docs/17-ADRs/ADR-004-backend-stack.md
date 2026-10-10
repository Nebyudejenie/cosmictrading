# ADR-004: Python 3.12 + FastAPI + Pydantic v2 + SQLAlchemy Core/asyncpg + Alembic
- **Status:** Accepted · **Date:** 2026-10-10
- **Context:** greenfield; the owner's other active project already uses this stack successfully (ARADA ADR-004); Python suits quant analytics and has mature Telegram (aiogram 3) and HTTP (httpx) libraries; `decimal.Decimal` is exact.
- **Decision:** Python 3.12 (image pins the patch version), FastAPI, Jinja2, Pydantic v2 + pydantic-settings, SQLAlchemy 2 Core over asyncpg, Alembic, httpx, structlog, argon2-cffi, pyotp, cryptography, aiogram 3; dev: pytest, pytest-asyncio, hypothesis, ruff, mypy strict, pip-audit, bandit. Dependencies locked with `uv.lock`.
- **Alternatives:** Go (fast, less quant tooling), TypeScript/NestJS, Java/Spring (heavier).
- **Consequences:** single language end to end; CPU-heavy analytics stay small at this scale (pure Python, no numpy/pandas until measured need).
