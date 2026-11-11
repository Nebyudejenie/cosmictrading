"""Provider registry loaded from the reviewed TOML file (keys never stored there)."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from cosmic.core.config import Settings
from cosmic.core.errors import ConfigError


@dataclass(frozen=True)
class ProviderConfig:
    key: str
    name: str
    base_url: str
    api_key_setting: str
    enabled: bool
    free_basis: str
    requires_attestation: bool
    runtime_price_check: bool
    model_id_pattern: str
    docs_url: str
    reviewed_on: str
    data_policy: str
    default_rpm: int
    default_rpd: int
    disabled_reason: str | None = None

    def model_allowed_by_pattern(self, model_id: str) -> bool:
        return bool(re.fullmatch(self.model_id_pattern, model_id))

    def api_key(self, settings: Settings) -> str | None:
        """The owner's portal key (decrypted in the worker only, S81), else the secret file's."""
        from cosmic.ai import keystore

        portal = keystore.cached(self.key)
        if portal:
            return portal
        if not self.api_key_setting:
            return None
        value = getattr(settings, self.api_key_setting, None)
        return None if value is None else str(value.get_secret_value())

    def key_source(self, settings: Settings) -> str:
        """PORTAL, FILE or NONE: where this process's key for the provider comes from."""
        from cosmic.ai import keystore

        if keystore.cached(self.key):
            return "PORTAL"
        value = getattr(settings, self.api_key_setting, None) if self.api_key_setting else None
        return "FILE" if value is not None and value.get_secret_value() else "NONE"


@lru_cache(maxsize=4)
def _load(path: str) -> tuple[ProviderConfig, ...]:
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"AI registry {path} cannot be read") from exc
    providers = []
    for p in data.get("providers", []):
        if not str(p.get("base_url", "")).startswith("https://"):
            raise ConfigError(f"AI provider {p.get('key')} must use https")
        providers.append(ProviderConfig(**p))
    if len({p.key for p in providers}) != len(providers):
        raise ConfigError("duplicate AI provider keys")
    return tuple(providers)


def load_registry(settings: Settings) -> dict[str, ProviderConfig]:
    path = settings.ai_registry_path
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[3] / path
    return {p.key: p for p in _load(str(path))}
