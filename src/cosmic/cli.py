"""Command-line entry points: ``cosmic <group> <command>``.

Secrets are never accepted as command-line arguments (they would leak into
shell history and process listings); interactive prompts are used instead.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable, Coroutine
from typing import Any

from cosmic.core.config import Settings, load_settings
from cosmic.core.errors import CosmicError
from cosmic.core.logs import configure_logging
from cosmic.core.redaction import SECRETS


def _settings(process: str) -> Settings:
    settings = load_settings()
    SECRETS.register(settings.secret_values())
    configure_logging(settings.log_level, settings.log_json, process=process)
    return settings


def cmd_db_bootstrap(_args: argparse.Namespace) -> int:
    from cosmic.db.bootstrap import bootstrap

    settings = _settings("cli")
    asyncio.run(bootstrap(settings))
    print("database roles and database are in place")
    return 0


def cmd_db_migrate(args: argparse.Namespace) -> int:
    from cosmic.db.migrate import upgrade

    settings = _settings("cli")
    upgrade(settings, args.revision)
    print(f"migrated to {args.revision}")
    return 0


def cmd_db_downgrade(args: argparse.Namespace) -> int:
    from cosmic.db.migrate import downgrade

    settings = _settings("cli")
    if not args.confirm:
        print("refusing to downgrade without --confirm (take a backup first)", file=sys.stderr)
        return 2
    downgrade(settings, args.revision)
    print(f"downgraded to {args.revision}")
    return 0


def _run_async(factory: Callable[[Settings], Coroutine[Any, Any, int]], process: str) -> int:
    settings = _settings(process)
    return asyncio.run(factory(settings))


def cmd_admin_create_owner(_args: argparse.Namespace) -> int:
    from cosmic.identity.cli import create_owner, prompt_owner

    settings = _settings("cli")
    data = prompt_owner()
    return asyncio.run(create_owner(settings, data))


def cmd_admin_telegram_link_code(args: argparse.Namespace) -> int:
    """Server-admin path to the one-time Telegram link code (the portal offers the same)."""

    async def run(settings: Settings) -> int:
        from datetime import timedelta

        from sqlalchemy import select

        from cosmic.audit.service import Actor, ActorType
        from cosmic.core.clock import SystemClock
        from cosmic.db.engine import Database
        from cosmic.db.schema import users
        from cosmic.notify.telegram.linking import create_link_code_for

        db = Database.from_settings(settings)
        try:
            async with db.transaction() as conn:
                user_id = await conn.scalar(
                    select(users.c.id).where(users.c.username == args.username.strip().lower())
                )
                if user_id is None:
                    raise CosmicError(f"no user named {args.username}")
                code = await create_link_code_for(
                    conn,
                    user_id=user_id,
                    actor=Actor(ActorType.CLI, "telegram-link-code"),
                    now=SystemClock().now(),
                    ttl=timedelta(minutes=args.minutes),
                )
        finally:
            await db.dispose()
        print(f"send to the bot within {args.minutes} minutes:  /link {code}")
        return 0

    return _run_async(run, "cli")


def cmd_audit_verify(_args: argparse.Namespace) -> int:
    async def run(settings: Settings) -> int:
        from cosmic.audit.service import verify_chain
        from cosmic.db.engine import Database

        db = Database.from_settings(settings)
        try:
            async with db.connect() as conn:
                result = await verify_chain(conn)
        finally:
            await db.dispose()
        print(
            f"audit chain ok={result.ok} checked={result.checked}"
            + ("" if result.ok else f" first_bad_seq={result.first_bad_seq} reason={result.reason}")
        )
        return 0 if result.ok else 1

    return _run_async(run, "cli")


def cmd_web(_args: argparse.Namespace) -> int:
    import uvicorn

    from cosmic.web.app import create_app

    settings = _settings("web")
    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.web_host,
        port=settings.web_port,
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        server_header=False,
        date_header=False,
        log_config=None,
        access_log=False,
    )
    return 0


def cmd_worker(_args: argparse.Namespace) -> int:
    from cosmic.worker.main import run_worker

    return _run_async(run_worker, "worker")


def cmd_research_history(args: argparse.Namespace) -> int:
    """Fetch the strategy library's daily research history (normally done by the worker)."""

    async def run(settings: Settings) -> int:
        from cosmic.core.clock import SystemClock
        from cosmic.db.engine import Database
        from cosmic.market.jobs import build_ingest_service
        from cosmic.research.library import jobs as library_jobs
        from cosmic.sources.ingest import ensure_registry
        from cosmic.worker.context import WorkerContext

        db = Database.from_settings(settings)
        ctx = WorkerContext(settings=settings, db=db, clock=SystemClock(), worker_id="cli")
        try:
            async with db.transaction() as conn:
                await ensure_registry(conn, ctx.clock.now())
            ctx.services["ingest"] = build_ingest_service(ctx)
            if args.force:
                for _key, (source, days) in library_jobs.HISTORY.items():
                    await ctx.services["ingest"].backfill_bars(source, "1d", days=days)
            counts = await library_jobs.ensure_history(ctx)
        finally:
            client = ctx.services.get("http_client")
            if client is not None:
                await client.aclose()
            await db.dispose()
        for key, n in counts.items():
            print(f"{key}: {n} daily bars")
        return 0

    return _run_async(run, "worker")


def cmd_research_library(args: argparse.Namespace) -> int:
    """Evaluate the 100-rule library now and print what qualified."""

    async def run(settings: Settings) -> int:
        import uuid

        from cosmic.core.clock import SystemClock
        from cosmic.db.engine import Database
        from cosmic.research.library import store

        db = Database.from_settings(settings)
        try:
            scenarios = tuple(c for c in store.SCENARIOS if args.costs in ("all", c.name))
            out = await store.run_library(db, SystemClock().now(), scenarios)
            if out["status"] != "COMPLETED":
                print(f"{out['status']}: {out.get('missing')} (need {out.get('min_bars')} bars)")
                return 1
            async with db.connect() as conn:
                for name, run_id in out["runs"].items():
                    print(f"== {name} (run {run_id})")
                    rows = await store.run_evaluations(conn, uuid.UUID(run_id))
                    by_series: dict[str, list[Any]] = {}
                    for r in rows:
                        by_series.setdefault(r.series, []).append(r)
                    for series, evs in by_series.items():
                        tiers: dict[str, int] = {}
                        for r in evs:
                            tiers[r.tier] = tiers.get(r.tier, 0) + 1
                        print(f"  {series}: {tiers}")
                        for r in evs[: args.top]:
                            print(
                                f"    #{r.rank:<3} {r.strategy_key:32} {r.tier:9} "
                                f"DSR {float(r.deflated_sharpe):.2f}  "
                                f"timing Sharpe {float(r.timing_sharpe):.2f}"
                            )
        finally:
            await db.dispose()
        return 0

    return _run_async(run, "worker")


def cmd_research_library_status(_args: argparse.Namespace) -> int:
    async def run(settings: Settings) -> int:
        from cosmic.db.engine import Database
        from cosmic.research.library import store

        db = Database.from_settings(settings)
        try:
            async with db.connect() as conn:
                for costs in store.SCENARIOS:
                    run = await store.latest_run(conn, costs.name)
                    if run is None:
                        print(f"{costs.name}: no run yet")
                        continue
                    print(
                        f"{costs.name}: {run.created_at:%Y-%m-%d %H:%M} UTC, "
                        f"code {run.code_version} (current {store.CODE_VERSION}), "
                        f"{run.trials} trials"
                    )
                    for m in run.series:
                        print(
                            f"  {m['series']}: {m['from']}..{m['to']} {m['bars']} bars {m['tiers']}"
                        )
                active = await store.active(conn)
                print(f"switched on: {len(active)}")
                for r in active:
                    print(f"  {r.strategy_key} since {r.occurred_at:%Y-%m-%d %H:%M}")
        finally:
            await db.dispose()
        return 0

    return _run_async(run, "cli")


def cmd_broker_binance_testnet_check(args: argparse.Namespace) -> int:
    """Run the broker contract on the real Binance Spot Test Network and record the result.

    Keys come from the worker's mounted secrets (``--from-secrets``) or as JSON on stdin —
    {"api_key": "...", "private_key_pem": "..."} — so they never appear in process arguments
    or logs. Prints step results only. The run is stored (append-only, audited) with a
    fingerprint of the key: a PASSED run is what allows a test-network account (ADR-026).
    """

    async def run(settings: Settings) -> int:
        import json

        from cosmic.audit.service import Actor, ActorType
        from cosmic.core.clock import SystemClock
        from cosmic.db.engine import Database
        from cosmic.trading.brokers.binance_check import CONTRACT_VERSION, check_with_keys
        from cosmic.trading.brokers.binance_testnet import key_fingerprint
        from cosmic.trading.sandbox import record_contract_run

        if args.from_secrets:
            key, secret = settings.binance_testnet_api_key, settings.binance_testnet_private_key
            if key is None or secret is None:
                print(
                    "error: the testnet key files are not mounted "
                    "(COSMIC_BINANCE_TESTNET_API_KEY_FILE / _PRIVATE_KEY_FILE)",
                    file=sys.stderr,
                )
                return 2
            api_key, pem = key.get_secret_value(), secret.get_secret_value()
        else:
            try:
                keys = json.loads(sys.stdin.read())
                api_key, pem = str(keys["api_key"]), str(keys["private_key_pem"])
            except (ValueError, KeyError, TypeError):
                print(
                    'error: stdin must be JSON {"api_key": ..., "private_key_pem": ...} '
                    "(or use --from-secrets in the worker)",
                    file=sys.stderr,
                )
                return 2
        SECRETS.register([api_key])
        clock = SystemClock()
        started = clock.now()
        steps = await check_with_keys(api_key, pem)
        finished = clock.now()
        for step in steps:
            print(
                f"{'PASS' if step.passed else 'FAIL'}  {step.name}"
                + (f" ({step.detail})" if step.detail and not step.passed else "")
            )
        fingerprint = key_fingerprint(api_key)
        db = Database.from_settings(settings)
        try:
            async with db.transaction() as conn:
                run_id, ok = await record_contract_run(
                    conn,
                    steps=steps,
                    key_fingerprint=fingerprint,
                    code_version=CONTRACT_VERSION,
                    started_at=started,
                    finished_at=finished,
                    actor=Actor(ActorType.CLI, "binance-testnet-check"),
                )
        finally:
            await db.dispose()
        print(f"recorded as contract run {run_id} (key fingerprint {fingerprint})")
        print("CONTRACT PASSED on the Binance Spot Test Network" if ok else "CONTRACT FAILED")
        return 0 if ok else 1

    return _run_async(run, "cli")


def cmd_golive_status(_args: argparse.Namespace) -> int:
    async def run(settings: Settings) -> int:
        from cosmic.core.clock import SystemClock
        from cosmic.db.engine import Database
        from cosmic.trading.golive import readiness

        db = Database.from_settings(settings)
        try:
            async with db.connect() as conn:
                gates = await readiness(conn, SystemClock().now())
        finally:
            await db.dispose()
        for g in gates:
            print(f"{g.key} {g.status:8} {g.title}")
            for r in g.reasons:
                print(f"           - {r}")
        print(
            "live trading: DISABLED"
            if any(g.status != "PASSED" for g in gates)
            else "all gates passed"
        )
        return 0

    return _run_async(run, "cli")


def cmd_golive_record(args: argparse.Namespace) -> int:
    import json
    from pathlib import Path

    evidence = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
    if not isinstance(evidence, dict):
        raise CosmicError("evidence file must contain a JSON object")

    async def run(settings: Settings) -> int:
        from cosmic.audit.service import Actor, ActorType
        from cosmic.core.clock import SystemClock
        from cosmic.db.engine import Database
        from cosmic.trading.golive import record_gate

        db = Database.from_settings(settings)
        try:
            async with db.transaction() as conn:
                await record_gate(
                    conn,
                    gate=args.gate,
                    status=args.status,
                    evidence=evidence,
                    actor=Actor(ActorType.CLI, "golive-record"),
                    now=SystemClock().now(),
                )
        finally:
            await db.dispose()
        print(f"recorded gate {args.gate} as {args.status}")
        return 0

    return _run_async(run, "cli")


def cmd_probe_web(_args: argparse.Namespace) -> int:
    """Smoke test: the local /readyz answers 200 (the container healthcheck is stdlib-only)."""
    from cosmic.ops.probe import local_ready

    return 0 if local_ready(load_settings().web_port) else 1


def cmd_probe_worker(args: argparse.Namespace) -> int:
    """Container healthcheck: this container's worker wrote a heartbeat recently."""

    async def run(settings: Settings) -> int:
        import socket
        from datetime import timedelta

        from sqlalchemy import func, select

        from cosmic.core.clock import SystemClock
        from cosmic.db.engine import Database
        from cosmic.db.schema import worker_heartbeats

        db = Database.from_settings(settings)
        try:
            async with db.connect() as conn:
                newest = await conn.scalar(
                    select(func.max(worker_heartbeats.c.last_beat_at)).where(
                        worker_heartbeats.c.worker_id.startswith(f"{socket.gethostname()}-")
                    )
                )
        except Exception:
            return 1
        finally:
            await db.dispose()
        cutoff = SystemClock().now() - timedelta(seconds=args.max_age)
        return 0 if newest is not None and newest >= cutoff else 1

    settings = load_settings()
    return asyncio.run(run(settings))


def cmd_backup_prune(args: argparse.Namespace) -> int:
    from pathlib import Path

    from cosmic.ops.backups import Retention, prune

    directory = Path(args.dir)
    if not directory.is_dir():
        raise CosmicError(f"{directory} is not a directory")
    doomed = prune(
        directory,
        Retention(args.daily, args.weekly, args.monthly, args.recent),
        dry_run=args.dry_run,
    )
    verb = "would delete" if args.dry_run else "deleted"
    for name in doomed:
        print(f"{verb} {name}")
    print(f"{verb} {len(doomed)} file(s)")
    return 0


def cmd_bot(_args: argparse.Namespace) -> int:
    from cosmic.notify.telegram.runner import run_bot

    return _run_async(run_bot, "bot")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cosmic", description="COSMICTRADING QUANT")
    groups = parser.add_subparsers(dest="group", required=True)

    db = groups.add_parser("db", help="database administration").add_subparsers(
        dest="cmd", required=True
    )
    db.add_parser("bootstrap", help="create roles and database (superuser)").set_defaults(
        func=cmd_db_bootstrap
    )
    mig = db.add_parser("migrate", help="apply migrations")
    mig.add_argument("--revision", default="head")
    mig.set_defaults(func=cmd_db_migrate)
    down = db.add_parser("downgrade", help="roll back migrations")
    down.add_argument("revision")
    down.add_argument("--confirm", action="store_true")
    down.set_defaults(func=cmd_db_downgrade)

    admin = groups.add_parser("admin", help="administration").add_subparsers(
        dest="cmd", required=True
    )
    admin.add_parser("create-owner", help="create the owner account (interactive)").set_defaults(
        func=cmd_admin_create_owner
    )

    tg = admin.add_parser("telegram-link-code", help="one-time code to link a Telegram account")
    tg.add_argument("username")
    tg.add_argument("--minutes", type=int, default=30, help="lifetime, 1-60 minutes")
    tg.set_defaults(func=cmd_admin_telegram_link_code)

    audit = groups.add_parser("audit", help="audit log").add_subparsers(dest="cmd", required=True)
    audit.add_parser("verify", help="verify the hash chain").set_defaults(func=cmd_audit_verify)

    research = groups.add_parser("research", help="strategy library").add_subparsers(
        dest="cmd", required=True
    )
    rh = research.add_parser("history", help="fetch the daily research history now")
    rh.add_argument("--force", action="store_true", help="backfill even if history exists")
    rh.set_defaults(func=cmd_research_history)
    rl = research.add_parser("library", help="evaluate the 100-rule library now")
    rl.add_argument("--costs", default="all", choices=["all", "binance_like", "coinbase_like"])
    rl.add_argument("--top", type=int, default=5, help="rules to list per series")
    rl.set_defaults(func=cmd_research_library)
    research.add_parser("library-status", help="latest library runs").set_defaults(
        func=cmd_research_library_status
    )

    broker = groups.add_parser("broker", help="broker adapters").add_subparsers(
        dest="cmd", required=True
    )
    check = broker.add_parser(
        "binance-testnet-check",
        help="contract run on the real Binance Spot Test Network, recorded (keys as JSON on "
        "stdin, or --from-secrets in the worker)",
    )
    check.add_argument(
        "--from-secrets",
        action="store_true",
        help="use the testnet key files mounted in this container",
    )
    check.set_defaults(func=cmd_broker_binance_testnet_check)

    golive = groups.add_parser("golive", help="go-live readiness").add_subparsers(
        dest="cmd", required=True
    )
    golive.add_parser("status", help="print gates A–I").set_defaults(func=cmd_golive_status)
    rec = golive.add_parser("record", help="record evidence for a gate (append-only)")
    rec.add_argument("gate", choices=list("ABCDEFGHI"))
    rec.add_argument("--status", required=True, choices=["PASSED", "FAILED", "NOT_MET"])
    rec.add_argument(
        "--evidence", required=True, help="path to a JSON file describing the evidence"
    )
    rec.set_defaults(func=cmd_golive_record)

    probe = groups.add_parser("probe", help="container healthchecks").add_subparsers(
        dest="cmd", required=True
    )
    probe.add_parser("web", help="local /readyz answers 200").set_defaults(func=cmd_probe_web)
    pw = probe.add_parser("worker", help="this container's worker heartbeat is recent")
    pw.add_argument("--max-age", type=int, default=60, help="seconds")
    pw.set_defaults(func=cmd_probe_worker)

    backup = groups.add_parser("backup", help="backup maintenance").add_subparsers(
        dest="cmd", required=True
    )
    bp = backup.add_parser("prune", help="apply 14 daily / 8 weekly / 6 monthly retention")
    bp.add_argument("--dir", required=True)
    bp.add_argument("--daily", type=int, default=14)
    bp.add_argument("--weekly", type=int, default=8)
    bp.add_argument("--monthly", type=int, default=6)
    bp.add_argument("--recent", type=int, default=3, help="newest sets always kept")
    bp.add_argument("--dry-run", action="store_true")
    bp.set_defaults(func=cmd_backup_prune)

    groups.add_parser("web", help="run the portal and API").set_defaults(func=cmd_web)
    groups.add_parser("worker", help="run the background worker").set_defaults(func=cmd_worker)
    groups.add_parser("bot", help="run the Telegram bot (needs a token)").set_defaults(func=cmd_bot)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result: int = args.func(args)
        return result
    except CosmicError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
