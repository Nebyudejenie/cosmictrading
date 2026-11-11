"""Authenticated encryption of the few secrets the owner types in the portal (AES-GCM).

The key is derived (HKDF-SHA256) from the server's own ``secret_key`` with a purpose label, so
each kind of secret has its own key, and whatever identifies the secret's owner (an AI provider,
an MT5 account) is bound in as associated data: a stored secret moved to another record cannot
be decrypted for it. A secret that fails to decrypt is treated as absent, never as an error that
could leak it.
"""

from __future__ import annotations

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from cosmic.core.config import Settings
from cosmic.core.errors import ConfigError

NONCE_BYTES = 12


class Sealer:
    def __init__(self, settings: Settings, purpose: bytes) -> None:
        secret = settings.secret_key.get_secret_value().encode() if settings.secret_key else b""
        if len(secret) < 16:
            raise ConfigError("the server's secret_key is missing or too short to protect secrets")
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=purpose).derive(secret)
        self._aead = AESGCM(key)

    def seal(self, plaintext: str, bound_to: str) -> tuple[bytes, bytes]:
        """(nonce, ciphertext) of ``plaintext``, usable only with the same ``bound_to``."""
        nonce = os.urandom(NONCE_BYTES)
        return nonce, self._aead.encrypt(nonce, plaintext.encode(), bound_to.encode())

    def open(self, nonce: bytes, ciphertext: bytes, bound_to: str) -> str | None:
        """The plaintext, or None when it was sealed for something else or altered."""
        try:
            return self._aead.decrypt(bytes(nonce), bytes(ciphertext), bound_to.encode()).decode()
        except (InvalidTag, ValueError):
            return None
