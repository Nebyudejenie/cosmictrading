"""AI provider keys typed by the owner in the portal (S81; the owner's choice of 2026-11-11,
AGENTS.md's one exception to "secrets only in secret files").

- **Stored only encrypted:** AES-GCM with a 256-bit key derived (HKDF-SHA256) from the server's
  own ``secret_key``. The provider's key is bound in as associated data, so a stored key cannot
  be decrypted for, or sent to, another provider.
- **Kept and shown:** only the last four characters, for the mask.
- **Decrypted only by the worker,** which calls the providers (``refresh``). The web process
  only encrypts, masks and revokes. A decrypted key is registered with the log scrubber at once.
- **One active key per provider:** a new one replaces it; revoked keys stay, unusable. Every
  change is audited, with the provider and the mask only.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from cosmic.audit.service import Actor
from cosmic.audit.service import record as audit
from cosmic.core.config import Settings
from cosmic.core.errors import ConfigError, ValidationFailedError
from cosmic.core.ids import uuid7
from cosmic.core.redaction import SECRETS
from cosmic.db.schema_ai import ai_provider_keys

INFO = b"cosmic ai provider keys v1"
KEY_SHAPE = re.compile(r"[\x21-\x7e]{8,512}")  # printable, no spaces: what providers issue
_CACHE: dict[str, str] = {}  # provider → decrypted key, in the worker only


def _aead(settings: Settings) -> AESGCM:
    secret = settings.secret_key.get_secret_value().encode() if settings.secret_key else b""
    if len(secret) < 16:
        raise ConfigError("the server's secret_key is missing or too short to protect AI keys")
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=INFO).derive(secret)
    return AESGCM(derived)


def mask(last4: str) -> str:
    return "••••" + last4


async def active(conn: AsyncConnection) -> dict[str, Any]:
    """Each provider's active portal key, without decrypting it (for the portal)."""
    rows = await conn.execute(
        select(
            ai_provider_keys.c.provider_key,
            ai_provider_keys.c.last4,
            ai_provider_keys.c.added_at,
        ).where(ai_provider_keys.c.revoked_at.is_(None))
    )
    return {r.provider_key: r for r in rows}


async def save(
    conn: AsyncConnection,
    settings: Settings,
    provider_key: str,
    plaintext: str,
    *,
    actor: Actor,
    now: datetime,
) -> str:
    """Encrypt and store a new key, replacing the provider's active one; returns the mask."""
    value = plaintext.strip()
    if not KEY_SHAPE.fullmatch(value):
        raise ValidationFailedError(
            "API key: 8 to 512 printable characters without spaces, as the provider issued it"
        )
    await _revoke(conn, provider_key, actor=actor, now=now)
    nonce = os.urandom(12)
    ciphertext = _aead(settings).encrypt(nonce, value.encode(), provider_key.encode())
    last4 = value[-4:]
    await conn.execute(
        ai_provider_keys.insert().values(
            id=uuid7(),
            provider_key=provider_key,
            ciphertext=ciphertext,
            nonce=nonce,
            last4=last4,
            added_at=now,
            added_by=f"{actor.type.value}:{actor.id}",
        )
    )
    await audit(
        conn,
        action="ai.key_saved",
        actor=actor,
        occurred_at=now,
        entity_type="ai_provider",
        entity_id=provider_key,
        payload={"provider": provider_key, "key": mask(last4)},
    )
    return mask(last4)


async def _revoke(conn: AsyncConnection, provider_key: str, *, actor: Actor, now: datetime) -> bool:
    done = await conn.execute(
        update(ai_provider_keys)
        .where(
            ai_provider_keys.c.provider_key == provider_key,
            ai_provider_keys.c.revoked_at.is_(None),
        )
        .values(revoked_at=now, revoked_by=f"{actor.type.value}:{actor.id}")
    )
    return bool(done.rowcount)


async def revoke(conn: AsyncConnection, provider_key: str, *, actor: Actor, now: datetime) -> bool:
    """The owner revokes the provider's portal key: it is never used again."""
    done = await _revoke(conn, provider_key, actor=actor, now=now)
    if done:
        await audit(
            conn,
            action="ai.key_revoked",
            actor=actor,
            occurred_at=now,
            entity_type="ai_provider",
            entity_id=provider_key,
            payload={"provider": provider_key},
        )
    return done


async def refresh(conn: AsyncConnection, settings: Settings) -> dict[str, str]:
    """The worker's decrypted portal keys, by provider (registered with the log scrubber). A
    key that fails to decrypt (the server secret changed) is skipped, never used."""
    rows = await conn.execute(
        select(
            ai_provider_keys.c.provider_key, ai_provider_keys.c.ciphertext, ai_provider_keys.c.nonce
        ).where(ai_provider_keys.c.revoked_at.is_(None))
    )
    aead = _aead(settings)
    found: dict[str, str] = {}
    for r in rows:
        try:
            found[r.provider_key] = aead.decrypt(
                bytes(r.nonce), bytes(r.ciphertext), r.provider_key.encode()
            ).decode()
        except InvalidTag:
            continue
    SECRETS.register(found.values())
    _CACHE.clear()
    _CACHE.update(found)
    return found


def cached(provider_key: str) -> str | None:
    """The worker's decrypted portal key for this provider, or None."""
    return _CACHE.get(provider_key)
