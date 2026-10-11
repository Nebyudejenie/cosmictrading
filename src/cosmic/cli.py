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

    audit = groups.add_parser("audit", help="audit log").add_subparsers(dest="cmd", required=True)
    audit.add_parser("verify", help="verify the hash chain").set_defaults(func=cmd_audit_verify)

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
