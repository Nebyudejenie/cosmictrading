"""Test-network (SANDBOX) accounts: the contract-run gate and account opening (ADR-026).

A SANDBOX account trades virtual funds on the Binance Spot Test Network. It can only be opened
when the adapter has passed its contract against the real test network with the key the worker
holds. The owner chooses how much USDT it manages; every other unit of the tracked assets that
the venue holds is booked as UNMANAGED. Reconciliation therefore accounts for all of it, while
equity, positions and risk see only the managed capital.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.orm import aliased

from cosmic.audit.service import Actor, record
from cosmic.core.errors import ConflictError, NotFoundError, ValidationFailedError
from cosmic.core.ids import uuid7
from cosmic.core.money import ZERO
from cosmic.db.schema_market import assets, instruments, venues
from cosmic.db.schema_trading import accounts, broker_contract_runs
from cosmic.trading import ledger
from cosmic.trading.brokers import binance_testnet, mt5_bridge
from cosmic.trading.brokers.base import VenueBalance
from cosmic.trading.brokers.binance_check import Step

ADAPTER = binance_testnet.ADAPTER  # the default (ADR-026); HFM is the other (ADR-028)


@dataclass(frozen=True)
class SandboxVenue:
    adapter: str
    venue: str
    quote: str
    environment: str
    label: str


SANDBOXES = {
    binance_testnet.ADAPTER: SandboxVenue(
        binance_testnet.ADAPTER,
        binance_testnet.VENUE,
        "USDT",
        binance_testnet.ENVIRONMENT,
        "Binance test network",
    ),
    mt5_bridge.ADAPTER: SandboxVenue(
        mt5_bridge.ADAPTER, mt5_bridge.VENUE, mt5_bridge.QUOTE, "HFMarketsGlobal-Demo", "HFM demo"
    ),
}
QUOTE = SANDBOXES[ADAPTER].quote  # kept for existing callers
ALIAS = re.compile(r"[a-z0-9][a-z0-9-]{1,40}")


async def record_contract_run(
    conn: AsyncConnection,
    *,
    steps: Sequence[Step],
    key_fingerprint: str,
    code_version: str,
    started_at: datetime,
    finished_at: datetime,
    actor: Actor,
    adapter: str = ADAPTER,
) -> tuple[uuid.UUID, bool]:
    """Store a contract run as evidence (append-only) and audit it. Returns (id, passed)."""
    passed = bool(steps) and all(s.passed for s in steps)
    run_id = uuid7()
    env = SANDBOXES[adapter]
    await conn.execute(
        broker_contract_runs.insert().values(
            id=run_id,
            adapter=adapter,
            environment=env.environment,
            key_fingerprint=key_fingerprint,
            status="PASSED" if passed else "FAILED",
            steps=[{"name": s.name, "passed": s.passed, "detail": s.detail[:300]} for s in steps],
            code_version=code_version,
            started_at=started_at,
            finished_at=finished_at,
            actor_type=actor.type.value,
            actor_id=actor.id or "",
        )
    )
    await record(
        conn,
        action="broker.contract_run",
        actor=actor,
        occurred_at=finished_at,
        entity_type="broker_contract_run",
        entity_id=run_id,
        payload={
            "adapter": adapter,
            "environment": env.environment,
            "status": "PASSED" if passed else "FAILED",
            "key_fingerprint": key_fingerprint,
            "failed_steps": [s.name for s in steps if not s.passed],
        },
    )
    return run_id, passed


async def latest_contract_run(
    conn: AsyncConnection, *, passed_only: bool = False, adapter: str = ADAPTER
) -> Any | None:
    q = select(broker_contract_runs).where(broker_contract_runs.c.adapter == adapter)
    if passed_only:
        q = q.where(broker_contract_runs.c.status == "PASSED")
    return (
        await conn.execute(q.order_by(desc(broker_contract_runs.c.finished_at)).limit(1))
    ).first()


async def sandbox_status(conn: AsyncConnection, adapter: str = ADAPTER) -> dict[str, Any]:
    """What the Operations page shows: the latest contract run and the open account."""
    alias = await conn.scalar(
        select(accounts.c.alias).where(
            accounts.c.kind == "SANDBOX",
            accounts.c.broker_adapter == adapter,
            accounts.c.status == "ACTIVE",
        )
    )
    run = await latest_contract_run(conn, adapter=adapter)
    return {"run": run, "alias": alias, "adapter": adapter, "label": SANDBOXES[adapter].label}


async def key_has_passed(
    conn: AsyncConnection, key_fingerprint: str, adapter: str = ADAPTER
) -> bool:
    row = (
        await conn.execute(
            select(broker_contract_runs.c.id)
            .where(
                broker_contract_runs.c.adapter == adapter,
                broker_contract_runs.c.key_fingerprint == key_fingerprint,
                broker_contract_runs.c.status == "PASSED",
            )
            .limit(1)
        )
    ).first()
    return row is not None


async def tracked_assets(conn: AsyncConnection, venue_key: str) -> set[str]:
    """Base and quote assets of the venue's instruments: what a SANDBOX account reconciles."""
    base, quote = aliased(assets), aliased(assets)
    rows = await conn.execute(
        select(base.c.symbol, quote.c.symbol)
        .select_from(instruments)
        .join(venues, venues.c.id == instruments.c.venue_id)
        .join(base, base.c.id == instruments.c.base_asset_id)
        .join(quote, quote.c.id == instruments.c.quote_asset_id)
        .where(venues.c.key == venue_key)
    )
    return {asset for r in rows for asset in r}


async def open_sandbox_account(
    conn: AsyncConnection,
    *,
    alias: str,
    managed_capital: Decimal,
    balances: Sequence[VenueBalance],
    key_fingerprint: str,
    now: datetime,
    actor: Actor,
    adapter: str = ADAPTER,
) -> uuid.UUID:
    """Open the SANDBOX account for ``adapter`` from the venue's current balances."""
    env = SANDBOXES[adapter]
    quote = env.quote
    if not ALIAS.fullmatch(alias):
        raise ValidationFailedError("alias: lower-case letters, digits and dashes (2–41)")
    if not await key_has_passed(conn, key_fingerprint, adapter):
        check = (
            "cosmic broker hfm-check"
            if adapter == mt5_bridge.ADAPTER
            else "cosmic broker binance-testnet-check --from-secrets"
        )
        raise ConflictError(
            f"{env.label}: this account/key has no PASSED contract run; run `{check}` in the "
            "worker first"
        )
    if not managed_capital.is_finite() or managed_capital <= ZERO:
        raise ValidationFailedError("the capital to manage must be a positive number")
    venue_id = await conn.scalar(select(venues.c.id).where(venues.c.key == env.venue))
    if venue_id is None:
        raise NotFoundError(f"unknown venue {env.venue}")
    if (await conn.execute(select(accounts.c.id).where(accounts.c.alias == alias))).first():
        raise ConflictError("account alias already exists")
    open_alias = await conn.scalar(
        select(accounts.c.alias).where(
            accounts.c.kind == "SANDBOX",
            accounts.c.broker_adapter == adapter,
            accounts.c.status == "ACTIVE",
        )
    )
    if open_alias is not None:
        raise ConflictError(f"{env.label} account {open_alias} is already open; disable it first")
    free = {b.asset: b.available for b in balances}
    total = {b.asset: b.total for b in balances}
    if managed_capital > free.get(quote, ZERO):
        raise ValidationFailedError(
            f"the test account has {free.get(quote, ZERO)} {quote} free; manage at most that"
        )
    account_id = uuid7()
    await conn.execute(
        accounts.insert().values(
            id=account_id,
            alias=alias,
            kind="SANDBOX",
            venue_id=venue_id,
            broker_adapter=adapter,
            base_currency=quote,
            status="ACTIVE",
            reconciliation_status="UNKNOWN",
            created_at=now,
            updated_at=now,
        )
    )
    await ledger.post(
        conn,
        account_id=account_id,
        kind="CAPITAL",
        ref_type="account_funding",
        ref_id=str(account_id),
        description="Test-network capital (virtual funds, not real money)",
        entries=[
            ledger.Entry("CASH", quote, managed_capital),
            ledger.Entry("CAPITAL", quote, -managed_capital),
        ],
        now=now,
        is_simulated=True,
    )
    unmanaged = {
        asset: total.get(asset, ZERO) - (managed_capital if asset == quote else ZERO)
        for asset in sorted(await tracked_assets(conn, env.venue))
    }
    entries = [
        entry
        for asset, amount in unmanaged.items()
        if amount != ZERO
        for entry in (
            ledger.Entry("UNMANAGED", asset, amount),
            ledger.Entry("CAPITAL", asset, -amount),
        )
    ]
    if entries:
        await ledger.post(
            conn,
            account_id=account_id,
            kind="CAPITAL",
            ref_type="unmanaged_opening",
            ref_id=str(account_id),
            description="Test-network holdings this account does not manage (virtual funds)",
            entries=entries,
            now=now,
            is_simulated=True,
        )
    await record(
        conn,
        action="account.created",
        actor=actor,
        occurred_at=now,
        entity_type="account",
        entity_id=account_id,
        payload={
            "alias": alias,
            "kind": "SANDBOX",
            "venue": env.venue,
            "managed_capital": managed_capital,
            "currency": quote,
            "unmanaged": {a: v for a, v in unmanaged.items() if v != ZERO},
            "key_fingerprint": key_fingerprint,
        },
    )
    return account_id
