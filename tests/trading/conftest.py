from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
import pytest

from tests.conftest import truncate_all


@pytest.fixture(autouse=True)
async def _clean_database(superuser_conn: asyncpg.Connection) -> AsyncIterator[None]:
    await truncate_all(superuser_conn)
    yield
