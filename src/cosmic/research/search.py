"""Full-text search over research evidence (DATA-10).

Searches claims (with their VERIFIED / INFERRED / CONTRADICTED status), change-first mission
reports and — for users who may read accounts — proposal summaries and theses. PostgreSQL
full-text search; the expressions match the GIN indexes of migration 0007. The user's text
is only ever a bound parameter to ``websearch_to_tsquery``, which accepts any input.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

MAX_QUERY = 200
HEADLINE = "StartSel=«, StopSel=», MaxWords=35, MinWords=12, MaxFragments=2"

CLAIMS = text("""
    SELECT c.id, c.statement, c.status, c.claim_type, c.as_of, c.mission_id,
           ts_rank(to_tsvector('english', c.statement), q) AS rank,
           ts_headline('english', c.statement, q, :opts) AS snippet
    FROM claims c, websearch_to_tsquery('english', :q) q
    WHERE to_tsvector('english', c.statement) @@ q
    ORDER BY rank DESC, c.created_at DESC
    LIMIT :limit
""")
REPORTS = text("""
    SELECT a.id, a.mission_id, a.created_at, m.kind AS mission_kind,
           ts_rank(jsonb_to_tsvector('english', a.content, '["string"]'), q) AS rank,
           ts_headline('english', a.content::text, q, :opts) AS snippet
    FROM artifacts a JOIN missions m ON m.id = a.mission_id,
         websearch_to_tsquery('english', :q) q
    WHERE a.kind = 'report'
      AND jsonb_to_tsvector('english', a.content, '["string"]') @@ q
    ORDER BY rank DESC, a.created_at DESC
    LIMIT :limit
""")
PROPOSALS = text("""
    SELECT t.id, t.trade_code, t.status, t.is_simulated, t.created_at,
           ts_rank(to_tsvector('english', t.plain_summary || ' ' || t.thesis), q) AS rank,
           ts_headline('english', t.plain_summary || ' ' || t.thesis, q, :opts) AS snippet
    FROM trade_intents t, websearch_to_tsquery('english', :q) q
    WHERE to_tsvector('english', t.plain_summary || ' ' || t.thesis) @@ q
    ORDER BY rank DESC, t.created_at DESC
    LIMIT :limit
""")


@dataclass(frozen=True)
class Hit:
    kind: str  # CLAIM | REPORT | PROPOSAL
    id: uuid.UUID
    title: str
    snippet: str
    status: str | None
    at: datetime | None
    link: str
    rank: float


def _clean(snippet: str) -> str:
    """Make a snippet from JSON text readable (quotes, braces and keys are noise)."""
    s = re.sub(r'"[a-z_]+":\s*', "", snippet)
    return re.sub(r'[{}\[\]"]+', " ", s).strip()


async def search(
    conn: AsyncConnection, query: str, *, include_proposals: bool, limit: int = 25
) -> list[Hit]:
    q = query.strip()[:MAX_QUERY]
    if not q:
        return []
    params = {"q": q, "limit": limit, "opts": HEADLINE}
    hits: list[Hit] = []
    for r in await conn.execute(CLAIMS, params):
        hits.append(
            Hit(
                "CLAIM",
                r.id,
                f"Claim ({r.claim_type})",
                r.snippet,
                r.status,
                r.as_of,
                f"/research/{r.mission_id}" if r.mission_id else "/research",
                float(r.rank),
            )
        )
    for r in await conn.execute(REPORTS, params):
        hits.append(
            Hit(
                "REPORT",
                r.id,
                f"Mission report ({r.mission_kind})",
                _clean(r.snippet),
                None,
                r.created_at,
                f"/research/{r.mission_id}",
                float(r.rank),
            )
        )
    if include_proposals:
        for r in await conn.execute(PROPOSALS, params):
            hits.append(
                Hit(
                    "PROPOSAL",
                    r.id,
                    f"Proposal {r.trade_code}",
                    r.snippet,
                    f"{r.status}{' SIMULATED' if r.is_simulated else ''}",
                    r.created_at,
                    f"/approvals/{r.id}",
                    float(r.rank),
                )
            )
    hits.sort(key=lambda h: h.rank, reverse=True)
    return hits[:limit]
