"""Fail if a tracked (or about-to-be-tracked) file contains credentials.

Checks: known credential formats, and any secret value from the local .env.
Prints file:line and the kind of match, never the matched value.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cosmic.core.redaction import SECRET_PATTERNS

ROOT = Path(__file__).resolve().parents[1]
SKIP_SUFFIXES = {".png", ".jpg", ".gif", ".ico", ".woff", ".woff2", ".lock"}
ENV_SECRET_KEYS = ("PASSWORD", "SECRET", "TOKEN", "API_KEY")


def candidate_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    return [ROOT / p for p in out if p and (ROOT / p).is_file()]


def env_secrets() -> list[str]:
    env = ROOT / ".env"
    values = []
    if env.exists():
        for line in env.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and any(k in key.upper() for k in ENV_SECRET_KEYS) and len(value.strip()) >= 8:
                values.append(value.strip())
    return values


def main() -> int:
    secrets = env_secrets()
    findings: list[str] = []
    for path in candidate_files():
        if path.suffix in SKIP_SUFFIXES or path.name == ".env":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    findings.append(
                        f"{path.relative_to(ROOT)}:{lineno}: matches {pattern.pattern[:30]}…"
                    )
            for value in secrets:
                if value in line:
                    findings.append(
                        f"{path.relative_to(ROOT)}:{lineno}: contains a value from .env"
                    )
    if findings:
        print("secret scan FAILED:")
        print("\n".join(findings))
        return 1
    print(f"secret scan passed ({len(candidate_files())} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
