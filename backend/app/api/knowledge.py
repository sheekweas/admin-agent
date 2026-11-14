"""Knowledge base (ТЗ §7.4): draft -> published -> archived; AI uses only published articles.

Editing a published article bumps its version and returns it to draft, so changed content reaches the AI
only after an owner re-publishes it ("knowledge.publish"). /knowledge/test runs the real agent in a
sandbox (mutating tools stubbed, everything rolled back).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.ai import orchestrator
from app.db import get_db
from app.deps import AuthContext, require
from app.models import KnowledgeArticle
from app.services.audit import audit
from app.services.common import DomainError, get_or_404

router = APIRouter()

DB = Annotated[Session, Depends(get_db)]
KRead = Annotated[AuthContext, Depends(require("knowledge.read"))]
KWrite = Annotated[AuthContext, Depends(require("knowledge.write"))]
KPublish = Annotated[AuthContext, Depends(require("knowledge.publish"))]
Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0)]

Section = Literal["company", "branches", "services", "prices", "staff", "promo", "rules", "faq", "scripts"]
Lang = Literal["ru", "kk"]


class ArticleIn(BaseModel):
    section: Section = "faq"
    language: Lang = "ru"
    title: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=20000)
    test_questions: list[str] = Field(default_factory=list, max_length=50)


class ArticlePatch(BaseModel):
    section: Section | None = None
    language: Lang | None = None
    title: str | None = Field(default=None, min_length=1, max_length=300)
    content: str | None = Field(default=None, min_length=1, max_length=20000)
    test_questions: list[str] | None = Field(default=None, max_length=50)


class TestIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    language: Lang | None = None
    include_drafts: bool = False


def article_out(a: KnowledgeArticle) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "section": a.section,
        "language": a.language,
        "title": a.title,
        "content": a.content,
        "status": a.status,
        "version": a.version,
        "author_id": str(a.author_id) if a.author_id else None,
        "test_questions": a.test_questions or [],
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
    }


@router.get("")
def list_articles(
    *,
    status: str | None = None,
    language: str | None = None,
    section: str | None = None,
    q: str | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
    ctx: KRead,
    db: DB,
) -> dict:
    stmt = select(KnowledgeArticle).where(KnowledgeArticle.tenant_id == ctx.tenant_id)
    if status:
        stmt = stmt.where(KnowledgeArticle.status.in_(status.split(",")))
    if language:
        stmt = stmt.where(KnowledgeArticle.language == language)
    if section:
        stmt = stmt.where(KnowledgeArticle.section == section)
    if q and q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(KnowledgeArticle.title.ilike(like), KnowledgeArticle.content.ilike(like)))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(KnowledgeArticle.updated_at.desc()).limit(limit).offset(offset)).all()
    return {"items": [article_out(a) for a in rows], "total": total}


@router.post("", status_code=201)
def create_article(body: ArticleIn, ctx: KWrite, db: DB) -> dict:
    a = KnowledgeArticle(
        tenant_id=ctx.tenant_id,
        section=body.section,
        language=body.language,
        title=body.title,
        content=body.content,
        status="draft",
        version=1,
        author_id=ctx.user.id,
        test_questions=body.test_questions,
    )
    db.add(a)
    db.flush()
    audit(
        db,
        ctx.tenant_id,
        "user",
        str(ctx.user.id),
        "knowledge.create",
        "knowledge_article",
        str(a.id),
        {},
        ip=ctx.ip,
    )
    db.commit()
    return article_out(a)


@router.get("/{article_id}")
def get_article(article_id: uuid.UUID, ctx: KRead, db: DB) -> dict:
    return article_out(get_or_404(db, KnowledgeArticle, article_id, ctx.tenant_id))


@router.patch("/{article_id}")
def update_article(article_id: uuid.UUID, body: ArticlePatch, ctx: KWrite, db: DB) -> dict:
    a = get_or_404(db, KnowledgeArticle, article_id, ctx.tenant_id)
    if a.status == "archived":
        raise DomainError(409, "ARCHIVED", "Archived articles cannot be edited")
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    content_changed = any(getattr(a, k) != v for k, v in changes.items() if k != "test_questions")
    for k, v in changes.items():
        setattr(a, k, v)
    previous = a.status
    if content_changed and a.status == "published":
        a.version += 1
        a.status = "draft"  # changed content must be re-published by an authorised person
    audit(
        db,
        ctx.tenant_id,
        "user",
        str(ctx.user.id),
        "knowledge.update",
        "knowledge_article",
        str(a.id),
        {"fields": sorted(changes), "version": a.version, "from": previous, "to": a.status},
        ip=ctx.ip,
    )
    db.commit()
    return article_out(a)


@router.post("/{article_id}/publish")
def publish_article(article_id: uuid.UUID, ctx: KPublish, db: DB) -> dict:
    a = get_or_404(db, KnowledgeArticle, article_id, ctx.tenant_id)
    if a.status != "draft":
        raise DomainError(409, "INVALID_TRANSITION", f"Cannot publish a {a.status} article", current=a.status)
    a.status = "published"
    audit(
        db,
        ctx.tenant_id,
        "user",
        str(ctx.user.id),
        "knowledge.publish",
        "knowledge_article",
        str(a.id),
        {"version": a.version},
        ip=ctx.ip,
    )
    db.commit()
    return article_out(a)


@router.post("/{article_id}/archive")
def archive_article(article_id: uuid.UUID, ctx: KWrite, db: DB) -> dict:
    a = get_or_404(db, KnowledgeArticle, article_id, ctx.tenant_id)
    if a.status == "archived":
        raise DomainError(409, "INVALID_TRANSITION", "Article is already archived", current=a.status)
    previous, a.status = a.status, "archived"
    audit(
        db,
        ctx.tenant_id,
        "user",
        str(ctx.user.id),
        "knowledge.archive",
        "knowledge_article",
        str(a.id),
        {"from": previous},
        ip=ctx.ip,
    )
    db.commit()
    return article_out(a)


@router.post("/test")
def test_question(body: TestIn, ctx: KRead, db: DB) -> dict:
    """Sandbox: how would the AI answer? No messages, bookings, tasks or tags are created."""
    try:
        outcome = orchestrator.run_sandbox(
            db, ctx.tenant_id, body.question, body.language, include_drafts=body.include_drafts
        )
    except orchestrator.TurnFailed as exc:
        raise DomainError(
            503, "AI_UNAVAILABLE", "AI is temporarily unavailable", reason=str(exc.cause)[:300]
        ) from exc
    finally:
        db.rollback()
    arts = (
        db.scalars(
            select(KnowledgeArticle).where(
                KnowledgeArticle.tenant_id == ctx.tenant_id,
                KnowledgeArticle.id.in_([uuid.UUID(i) for i in outcome.used_article_ids]),
            )
        ).all()
        if outcome.used_article_ids
        else []
    )
    return {
        "reply": outcome.reply_text,
        "language": outcome.language,
        "escalated": outcome.escalated,
        "tool_calls": [
            {k: c.get(k) for k in ("name", "input", "ok", "error", "result")} for c in outcome.tool_calls
        ],
        "used_articles": [
            {"id": str(a.id), "title": a.title, "status": a.status, "version": a.version} for a in arts
        ],
    }
