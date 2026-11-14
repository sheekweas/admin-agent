"""Knowledge-base retrieval for the AI agent (ТЗ §7.4): only PUBLISHED articles of the tenant are used.

Scoring is a small, dependency-free keyword/stem overlap (title weighs double). The candidate set per
tenant is small (tens to hundreds of articles), so scoring in Python is cheap and keeps the DB portable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.text import content_tokens, overlap
from app.models import KnowledgeArticle

MIN_SCORE = 2
MAX_CANDIDATES = 500


@dataclass(frozen=True)
class RetrievedArticle:
    id: uuid.UUID
    section: str
    language: str
    title: str
    content: str
    version: int
    score: int


def _score(query: list[str], art: KnowledgeArticle) -> int:
    return 2 * overlap(query, content_tokens(art.title)) + overlap(query, content_tokens(art.content))


def retrieve(
    db: Session,
    tenant_id: uuid.UUID,
    query: str,
    language: str,
    *,
    limit: int = 4,
    include_drafts: bool = False,
    always_sections: tuple[str, ...] = (),
) -> list[RetrievedArticle]:
    """Best matching articles for `query`; articles in `language` win ties over the other language.

    `always_sections` (e.g. ("company", "rules")) are included regardless of the query.
    `include_drafts` is only for the knowledge sandbox (/knowledge/test), never for live chats.
    """
    statuses = ("published", "draft") if include_drafts else ("published",)
    rows = db.scalars(
        select(KnowledgeArticle)
        .where(KnowledgeArticle.tenant_id == tenant_id, KnowledgeArticle.status.in_(statuses))
        .order_by(KnowledgeArticle.updated_at.desc())
        .limit(MAX_CANDIDATES)
    ).all()
    q = content_tokens(query)
    scored: list[tuple[int, int, KnowledgeArticle]] = []
    for art in rows:
        pinned = art.section in always_sections and art.language == language
        score = _score(q, art) if q else 0
        if score >= MIN_SCORE or pinned:
            scored.append((score + (100 if pinned else 0), 1 if art.language == language else 0, art))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [
        RetrievedArticle(a.id, a.section, a.language, a.title, a.content, a.version, s)
        for s, _, a in scored[:limit]
    ]
