"""Import contracts (AI-23, PRIN-02, TRD-06, ARC-03)."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "cosmic"

FORBIDDEN = {
    # Financial authority, audit and identity never depend on AI or research workers.
    "trading": ("cosmic.ai", "cosmic.workforce", "cosmic.research"),
    "audit": ("cosmic.ai", "cosmic.workforce", "cosmic.trading", "cosmic.web"),
    "identity": ("cosmic.ai", "cosmic.workforce", "cosmic.trading", "cosmic.web"),
    "core": ("cosmic.",),
    # AI gateway never touches trading or brokers.
    "ai": ("cosmic.trading", "cosmic.workforce", "cosmic.web"),
    # Layering: market data is below research, trading and the workforce.
    "market": ("cosmic.research", "cosmic.trading", "cosmic.workforce", "cosmic.ai", "cosmic.web"),
    "sources": ("cosmic.research", "cosmic.trading", "cosmic.workforce", "cosmic.ai", "cosmic.web"),
}
# Workforce may only propose; never execute, approve or reach brokers.
WORKFORCE_FORBIDDEN = (
    "cosmic.trading.execution",
    "cosmic.trading.approvals",
    "cosmic.trading.brokers",
    "cosmic.trading.killswitch",
    "cosmic.trading.authority_admin",
)


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def _violations(package: str, prefixes: tuple[str, ...]) -> list[str]:
    bad: list[str] = []
    pkg_dir = SRC / package
    if not pkg_dir.exists():
        return bad
    for path in pkg_dir.rglob("*.py"):
        for name in imports_of(path):
            if package == "core" and name.startswith("cosmic.core"):
                continue
            if any(
                name == p
                or name.startswith(p if p.endswith(".") else p + ".")
                or name == p.rstrip(".")
                for p in prefixes
            ):
                bad.append(f"{path.relative_to(SRC)} imports {name}")
    return bad


def test_forbidden_imports() -> None:
    problems: list[str] = []
    for package, prefixes in FORBIDDEN.items():
        problems += _violations(package, prefixes)
    problems += _violations("workforce", WORKFORCE_FORBIDDEN)
    assert not problems, "\n".join(problems)


def test_only_adapters_import_http_clients() -> None:
    allowed = {"sources/connectors", "ai/adapters", "trading/brokers", "notify/telegram"}
    allowed.add("ops/probe.py")  # loopback-only readiness probe (127.0.0.1), not outbound traffic
    problems = []
    for path in SRC.rglob("*.py"):
        rel = str(path.relative_to(SRC))
        if any(rel.startswith(a) for a in allowed) or rel.startswith("sources/http"):
            continue
        names = imports_of(path)
        if {"httpx", "aiohttp", "requests", "urllib.request"} & names:
            problems.append(rel)
    assert not problems, f"HTTP clients imported outside adapters: {problems}"


def test_no_local_llm_or_paid_sdk_dependencies() -> None:
    pyproject = (SRC.parents[1] / "pyproject.toml").read_text().lower()
    for banned in (
        "ollama",
        "llama-cpp",
        "llama_cpp",
        "vllm",
        "transformers",
        'openai"',
        "anthropic",
    ):
        assert banned not in pyproject, banned
