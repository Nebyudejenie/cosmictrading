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
        forwarded_allow_ips="*"
        if settings.env.value not in ("LOCAL_DEVELOPMENT", "TEST")
        else "127.0.0.1",
        server_header=False,
        date_header=False,
        log_config=None,
        access_log=False,
    )
    return 0


def cmd_worker(_args: argparse.Namespace) -> int:
    from cosmic.worker.main import run_worker

    return _run_async(run_worker, "worker")


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
