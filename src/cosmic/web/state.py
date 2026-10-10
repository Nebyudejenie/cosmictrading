"""Application state shared by request handlers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from fastapi.templating import Jinja2Templates

from cosmic.core.clock import Clock
from cosmic.core.config import Settings
from cosmic.db.engine import Database
from cosmic.identity.ratelimit import RateLimiter

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


@dataclass
class AppState:
    settings: Settings
    db: Database
    clock: Clock
    templates: Jinja2Templates
    login_ip_limiter: RateLimiter = field(default_factory=lambda: RateLimiter(10, 60))
    login_user_limiter: RateLimiter = field(default_factory=lambda: RateLimiter(10, 900))
    api_limiter: RateLimiter = field(default_factory=lambda: RateLimiter(240, 60))
    mfa_limiter: RateLimiter = field(default_factory=lambda: RateLimiter(10, 300))
