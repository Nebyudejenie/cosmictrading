"""Split a SQL script into statements, respecting quotes and dollar-quoted bodies.

Used by migrations: the asyncpg driver prepares statements, and a prepared
statement may contain only one command.
"""

from __future__ import annotations

import re

_DOLLAR_TAG = re.compile(r"\$[A-Za-z_]*\$")


def split_sql(script: str) -> list[str]:
    statements: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(script)
    while i < n:
        ch = script[i]
        if ch == "-" and script.startswith("--", i):
            end = script.find("\n", i)
            end = n if end == -1 else end
            i = end
            continue
        if ch == "'":
            end = i + 1
            while end < n:
                if script[end] == "'" and end + 1 < n and script[end + 1] == "'":
                    end += 2
                    continue
                if script[end] == "'":
                    break
                end += 1
            buf.append(script[i : end + 1])
            i = end + 1
            continue
        if ch == "$":
            match = _DOLLAR_TAG.match(script, i)
            if match:
                tag = match.group(0)
                end = script.find(tag, match.end())
                if end == -1:
                    raise ValueError(f"unterminated dollar quote {tag}")
                buf.append(script[i : end + len(tag)])
                i = end + len(tag)
                continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def run_script(script: str) -> None:
    """Execute every statement of ``script`` inside the current Alembic migration."""
    from alembic import op

    for statement in split_sql(script):
        op.execute(statement)
