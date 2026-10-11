"""Validated configuration with environment tiers and fail-early checks (CFG-01…06).

Secrets may be supplied directly (``COSMIC_DB_APP_PASSWORD``) or, preferably in
production, as a file path (``COSMIC_DB_APP_PASSWORD_FILE``). Error messages name
the setting, never its value.
"""

from __future__ import annotations

import base64
import binascii
import os
from enum import StrEnum
from functools import cached_property
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from cosmic.core.errors import ConfigError


class Environment(StrEnum):
    LOCAL_DEVELOPMENT = "LOCAL_DEVELOPMENT"
    TEST = "TEST"
    PAPER_TRADING = "PAPER_TRADING"
    STAGING = "STAGING"
    LIVE_DISABLED = "LIVE_DISABLED"
    LIVE_PENDING_APPROVAL = "LIVE_PENDING_APPROVAL"
    LIVE = "LIVE"


DEV_ENVIRONMENTS = frozenset({Environment.LOCAL_DEVELOPMENT, Environment.TEST})
DEV_SECRET_PREFIX = "insecure-dev-"  # noqa: S105 - marker identifying dev-only values

# Settings that may be provided via a *_FILE companion variable.
SECRET_FIELDS = (
    "db_app_password",
    "db_owner_password",
    "db_superuser_password",
    "secret_key",
    "metrics_token",
    "groq_api_key",
    "gemini_api_key",
    "openrouter_api_key",
    "telegram_bot_token",
)
# Optional integrations: an empty *_FILE means "not configured" (so deployments can always
# mount the file). Required secrets must never be empty.
OPTIONAL_SECRET_FIELDS = frozenset(
    {"metrics_token", "groq_api_key", "gemini_api_key", "openrouter_api_key", "telegram_bot_token"}
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="COSMIC_",
        env_file=None,
        extra="ignore",
        case_sensitive=False,
    )

    env: Environment = Environment.LOCAL_DEVELOPMENT

    # Database (components, so passwords can come from files)
    db_host: str = "127.0.0.1"
    db_port: int = 55442
    db_name: str = "cosmic"
    db_app_user: str = "cosmic_app"
    db_app_password: SecretStr | None = None
    db_owner_user: str = "cosmic_owner"
    db_owner_password: SecretStr | None = None
    db_superuser: str = "postgres"
    db_superuser_password: SecretStr | None = None
    db_pool_size: int = Field(default=5, ge=1, le=50)

    # Web
    web_host: str = "127.0.0.1"
    web_port: int = 8742
    # Proxies whose X-Forwarded-For is trusted (comma-separated IPs). Production sets the
    # cloudflared container's fixed address; "*" would let clients spoof their IP.
    forwarded_allow_ips: str = "127.0.0.1"
    allowed_hosts: list[str] = Field(default_factory=lambda: ["127.0.0.1", "localhost"])
    cookie_secure: bool = True
    session_absolute_hours: int = Field(default=12, ge=1, le=72)
    session_idle_minutes: int = Field(default=30, ge=5, le=240)
    step_up_minutes: int = Field(default=10, ge=1, le=30)
    debug: bool = False

    # Secrets
    secret_key: SecretStr | None = None  # base64, 32 bytes (AES-256-GCM)
    metrics_token: SecretStr | None = None

    # Logging
    log_level: str = "INFO"
    log_json: bool = True

    # Authority
    max_authority_mode: str = "PAPER_TRADING"

    # AI (FREE_CLOUD_AI_ONLY is the only supported policy)
    ai_policy: str = "FREE_CLOUD_AI_ONLY"
    ai_registry_path: Path = Path("config/ai_registry.toml")
    groq_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    openrouter_api_key: SecretStr | None = None

    # Telegram
    telegram_bot_token: SecretStr | None = None
    telegram_approval_requires_totp: bool = True

    # Paper broker
    paper_fault_injection: bool = False

    # Worker / resources
    data_path: Path = Path("/")
    disk_warning_free_pct: int = Field(default=15, ge=1, le=90)
    disk_critical_free_pct: int = Field(default=10, ge=1, le=90)
    market_data_enabled: bool = True
    # Touched after each successful database heartbeat; container healthchecks stat it
    # instead of starting a Python process that opens a database connection.
    heartbeat_file: Path | None = None

    @model_validator(mode="after")
    def _validate(self) -> Settings:
        problems: list[str] = []
        if self.ai_policy != "FREE_CLOUD_AI_ONLY":
            problems.append("COSMIC_AI_POLICY must be FREE_CLOUD_AI_ONLY (no paid or local AI)")
        if self.env is Environment.LIVE or self.env is Environment.LIVE_PENDING_APPROVAL:
            problems.append(
                f"COSMIC_ENV={self.env.value} requires a registered live broker adapter and "
                "passed go-live gates; none exist in this build"
            )
        if self.disk_critical_free_pct >= self.disk_warning_free_pct:
            problems.append("COSMIC_DISK_CRITICAL_FREE_PCT must be below the warning threshold")
        if self.secret_key is not None:
            try:
                raw = base64.b64decode(self.secret_key.get_secret_value(), validate=True)
            except (binascii.Error, ValueError):
                raw = b""
            if len(raw) != 32 and not self._is_dev_secret(self.secret_key):
                problems.append("COSMIC_SECRET_KEY must be base64 for exactly 32 bytes")
        if "*" in self.forwarded_allow_ips:
            problems.append("COSMIC_FORWARDED_ALLOW_IPS must list proxy addresses, not '*'")
        if self.env not in DEV_ENVIRONMENTS:
            if self.debug:
                problems.append("COSMIC_DEBUG must be false outside LOCAL_DEVELOPMENT/TEST")
            if not self.cookie_secure:
                problems.append("COSMIC_COOKIE_SECURE must be true outside LOCAL_DEVELOPMENT/TEST")
            if self.paper_fault_injection:
                problems.append(
                    "COSMIC_PAPER_FAULT_INJECTION is only allowed in LOCAL_DEVELOPMENT/TEST"
                )
            # Runtime processes need only the app role; the owner (DDL) password is given to
            # the migration job alone and is checked where it is used (owner_dsn).
            for name in ("secret_key", "db_app_password"):
                value = getattr(self, name)
                if value is None:
                    problems.append(f"COSMIC_{name.upper()} is required in {self.env.value}")
            for name in ("secret_key", "db_app_password", "db_owner_password"):
                value = getattr(self, name)
                if value is not None and self._is_dev_secret(value):
                    problems.append(
                        f"COSMIC_{name.upper()} uses a development value in {self.env.value}"
                    )
        if problems:
            raise ConfigError("; ".join(problems))
        return self

    @staticmethod
    def _is_dev_secret(value: SecretStr) -> bool:
        return value.get_secret_value().startswith(DEV_SECRET_PREFIX)

    # ---- derived values -------------------------------------------------

    def _dsn(self, user: str, password: SecretStr | None, database: str | None = None) -> str:
        if password is None:
            raise ConfigError(f"password for database user '{user}' is not configured")
        pw = quote(password.get_secret_value(), safe="")
        return (
            f"postgresql+asyncpg://{quote(user, safe='')}:{pw}@{self.db_host}:{self.db_port}/"
            f"{database or self.db_name}"
        )

    def app_dsn(self, database: str | None = None) -> str:
        return self._dsn(self.db_app_user, self.db_app_password, database)

    def owner_dsn(self, database: str | None = None) -> str:
        return self._dsn(self.db_owner_user, self.db_owner_password, database)

    def superuser_dsn(self, database: str = "postgres") -> str:
        return self._dsn(self.db_superuser, self.db_superuser_password, database)

    @cached_property
    def secret_key_bytes(self) -> bytes:
        if self.secret_key is None:
            raise ConfigError("COSMIC_SECRET_KEY is not configured")
        value = self.secret_key.get_secret_value()
        if value.startswith(DEV_SECRET_PREFIX):
            # Deterministic 32-byte key derived from the dev marker (dev/test only).
            import hashlib

            return hashlib.sha256(value.encode()).digest()
        return base64.b64decode(value)

    @property
    def cookie_name(self) -> str:
        return "__Host-cosmic_session" if self.cookie_secure else "cosmic_session"

    def secret_values(self) -> list[str]:
        """All configured secret values, for log/prompt redaction (never displayed)."""
        values: list[str] = []
        for name in SECRET_FIELDS:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                raw = value.get_secret_value()
                if len(raw) >= 6:
                    values.append(raw)
        return values

    def integration_status(self) -> dict[str, str]:
        """Feature-flag view of external integrations (CFG-06). Never reveals values."""
        return {
            "groq": "CONFIGURED" if self.groq_api_key else "NOT_CONFIGURED",
            "gemini": "CONFIGURED" if self.gemini_api_key else "NOT_CONFIGURED",
            "openrouter": "CONFIGURED" if self.openrouter_api_key else "NOT_CONFIGURED",
            "telegram": "CONFIGURED" if self.telegram_bot_token else "NOT_CONFIGURED",
            "live_broker": "NOT_CONFIGURED",
            "cloudflare_tunnel": "NOT_CONFIGURED",
        }


def _file_overrides(environ: dict[str, str]) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    for name in SECRET_FIELDS:
        file_var = f"COSMIC_{name.upper()}_FILE"
        path = environ.get(file_var)
        if not path:
            continue
        try:
            content = Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"{file_var} points to an unreadable file") from exc
        if not content:
            if name in OPTIONAL_SECRET_FIELDS:
                continue
            raise ConfigError(f"{file_var} points to an empty file")
        overrides[name] = content
    return overrides


def load_settings(environ: dict[str, str] | None = None, **overrides: Any) -> Settings:
    """Build settings from the environment (+ ``*_FILE`` secrets), failing early."""
    env = dict(os.environ if environ is None else environ)
    values = _file_overrides(env)
    values.update(overrides)
    try:
        if environ is None:
            return Settings(**values)
        # Explicit environment: isolate from the process environment.
        saved = os.environ.copy()
        try:
            os.environ.clear()
            os.environ.update(env)
            return Settings(**values)
        finally:
            os.environ.clear()
            os.environ.update(saved)
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(p) for p in err["loc"]) or "settings" for err in exc.errors()}
        )
        raise ConfigError("invalid configuration for: " + ", ".join(fields)) from None
