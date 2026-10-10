"""Interactive owner bootstrap (secrets are prompted, never passed as arguments)."""

from __future__ import annotations

import getpass
from dataclasses import dataclass

from cosmic.audit.service import Actor, ActorType
from cosmic.core.clock import SystemClock
from cosmic.core.config import Settings
from cosmic.core.errors import ConflictError, ValidationFailedError
from cosmic.db.engine import Database
from cosmic.identity.capabilities import Role
from cosmic.identity.service import count_owners, create_user


@dataclass(frozen=True)
class OwnerInput:
    username: str
    display_name: str
    password: str


def prompt_owner() -> OwnerInput:
    username = input("Owner username: ").strip().lower()
    display = input("Display name: ").strip()
    password = getpass.getpass("Password (min 12 chars): ")
    if password != getpass.getpass("Repeat password: "):
        raise ValidationFailedError("passwords do not match")
    return OwnerInput(username, display, password)


async def create_owner(settings: Settings, data: OwnerInput) -> int:
    db = Database.from_settings(settings)
    try:
        async with db.transaction() as conn:
            if await count_owners(conn) > 0:
                raise ConflictError("an owner account already exists")
            await create_user(
                conn,
                username=data.username,
                display_name=data.display_name,
                password=data.password,
                roles=[Role.OWNER],
                now=SystemClock().now(),
                actor=Actor(ActorType.CLI, "create-owner"),
            )
    finally:
        await db.dispose()
    print(f"owner '{data.username}' created. Sign in and enrol your authenticator app.")
    return 0
