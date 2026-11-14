"""One AI turn: context building, LLM + tools, guards, funnel/tag updates and the AIRun audit record.

The caller (app.services.messaging.process_ai_turn) owns the transaction and the outbound delivery; on
any exception raised from here it rolls back the turn's SAVEPOINT, stores the failed AIRun via
`record_failed_run` and sends the tenant's fallback message (AT-10).
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai import knowledge, prompts
from app.ai.llm import LLM, LLMError, LLMRequest, get_llm
from app.ai.text import detect_language
from app.ai.tools import (
    TAG_WANTS_TODAY,
    TOOL_DEFINITIONS,
    ToolContext,
    advance_lead_stage,
    allowed_tags,
    execute_tool,
    tool_executor,
)
from app.models import AIRun, Branch, Contact, Conversation, Message, Service, Tenant
from app.services.common import tz, utcnow
from app.services.tenant_settings import merged_settings

log = logging.getLogger("ai.orchestrator")

# Phrases that state a booking was made; allowed only after a successful booking tool call (ТЗ §7.3).
_BOOKING_CLAIM = re.compile(
    r"(вы записаны|вы успешно записаны|запись подтверждена|запись создана|бронь подтверждена|"
    r"запись перенесена|жазылуыңыз расталды|сіз жазылдыңыз|жазылуыңыз ауыстырылды)",
    re.IGNORECASE,
)
_SAFE_UNCONFIRMED = {
    "ru": "Сейчас проверю и подтвержу запись. Подтвердите, пожалуйста, выбранные дату и время ещё раз.",
    "kk": "Қазір тексеріп, жазылуды растаймын. Таңдаған күн мен уақытты тағы бір рет растаңызшы.",
}
_WANTS_TODAY = ("сегодня", "бүгін")


@dataclass
class TurnOutcome:
    reply_text: str
    language: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    used_article_ids: list[str] = field(default_factory=list)
    escalated: bool = False
    booking_done: bool = False
    ai_run_id: uuid.UUID | None = None


class TurnFailed(RuntimeError):
    """Wraps any failure of the turn with the data needed for the audit record."""

    def __init__(
        self,
        cause: BaseException,
        *,
        provider: str,
        model: str,
        latency_ms: int,
        tool_calls: list[dict[str, Any]],
        article_ids: list[str],
    ):
        super().__init__(f"{cause.__class__.__name__}: {cause}")
        self.cause = cause
        self.provider, self.model, self.latency_ms = provider, model, latency_ms
        self.tool_calls, self.article_ids = tool_calls, article_ids


# ---------------------------------------------------------------- context helpers
def _primary_branch(db: Session, tenant_id: uuid.UUID, branch_id: uuid.UUID | None) -> Branch | None:
    if branch_id:
        b = db.scalar(select(Branch).where(Branch.id == branch_id, Branch.tenant_id == tenant_id))
        if b is not None:
            return b
    return db.scalar(
        select(Branch)
        .where(Branch.tenant_id == tenant_id, Branch.active.is_(True))
        .order_by(Branch.created_at)
        .limit(1)
    )


def _branches(db: Session, tenant_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(Branch)
        .where(Branch.tenant_id == tenant_id, Branch.active.is_(True))
        .order_by(Branch.created_at)
    ).all()
    return [{"id": str(b.id), "name": b.name, "address": b.address, "timezone": b.timezone} for b in rows]


def _services(db: Session, tenant_id: uuid.UUID, lang: str) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(Service)
        .where(Service.tenant_id == tenant_id, Service.published.is_(True), Service.active.is_(True))
        .order_by(Service.category, Service.title_ru)
        .limit(60)
    ).all()
    out = []
    for s in rows:
        title = s.title_kk if lang == "kk" and s.title_kk else s.title_ru
        price = str(int(s.price)) if s.price == s.price.to_integral_value() else str(s.price)
        out.append({"service_id": str(s.id), "title": title, "duration_min": s.duration_min, "price": price})
    return out


def _message_text(m: Message) -> str:
    if m.text:
        return m.text
    return f"[{m.message_type}]"


def build_history(db: Session, conv: Conversation, trigger: Message, limit: int) -> list[dict[str, Any]]:
    """Last `limit` visible messages up to the trigger, as alternating user/assistant turns."""
    rows = db.scalars(
        select(Message)
        .where(
            Message.tenant_id == conv.tenant_id,
            Message.conversation_id == conv.id,
            Message.is_internal_note.is_(False),
            Message.received_at <= trigger.received_at,
            Message.id != trigger.id,
        )
        .order_by(Message.received_at.desc())
        .limit(max(limit - 1, 0))
    ).all()
    turns: list[dict[str, Any]] = []
    for m in [*reversed(rows), trigger]:
        role = "user" if m.direction == "in" else "assistant"
        text = _message_text(m)
        if m.author == "human":
            text = f"[администратор] {text}"
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"] += "\n" + text
        else:
            turns.append({"role": role, "content": text})
    while turns and turns[0]["role"] != "user":
        turns.pop(0)
    return turns


def _choose_language(text: str, current: str | None, default: str) -> str:
    return detect_language(text) or current or default


def _guard_reply(reply: str, ctx: ToolContext, lang: str) -> tuple[str, str | None]:
    if _BOOKING_CLAIM.search(reply) and not ctx.booking_done:
        return _SAFE_UNCONFIRMED.get(lang, _SAFE_UNCONFIRMED["ru"]), "unverified_booking_claim"
    return reply, None


def _auto_crm(ctx: ToolContext, text: str) -> None:
    """Funnel stages and auto-tags derived from what happened in the turn (CRM keeps final control)."""
    ok = {c["name"] for c in ctx.calls if c.get("ok")}
    stage = None
    if ok & {"get_available_slots"}:
        stage = "booking_pending"
    elif ok & {"list_services"}:
        stage = "info_requested"
    if stage and not ctx.booking_done:
        sp = ctx.db.begin_nested()
        try:
            advance_lead_stage(ctx, stage)
            sp.commit()
        except Exception:
            sp.rollback()
            log.exception("lead stage update failed")
    low = text.lower()
    if any(w in low for w in _WANTS_TODAY) and TAG_WANTS_TODAY in allowed_tags():
        tagged = any(c["name"] == "add_tag" and c["input"].get("tag") == TAG_WANTS_TODAY for c in ctx.calls)
        if not tagged:
            execute_tool(ctx, "add_tag", {"tag": TAG_WANTS_TODAY})


# ---------------------------------------------------------------- the turn
def _run(
    db: Session,
    tenant: Tenant,
    *,
    text: str,
    lang: str,
    history: list[dict[str, Any]],
    state: dict[str, Any],
    contact: Contact | None,
    conv: Conversation | None,
    trigger_id: uuid.UUID | None,
    branch: Branch | None,
    llm: LLM,
    dry_run: bool,
    include_drafts: bool,
) -> tuple[TurnOutcome, ToolContext, Any, list[str], int]:
    settings = merged_settings(tenant)
    ai_cfg = settings.get("ai", {})
    tz_name = branch.timezone if branch else "Asia/Almaty"
    now_local = utcnow().astimezone(tz(tz_name))
    arts = knowledge.retrieve(db, tenant.id, text, lang, include_drafts=include_drafts)
    article_ids = [str(a.id) for a in arts]
    article_dicts = [
        {
            "id": str(a.id),
            "section": a.section,
            "language": a.language,
            "title": a.title,
            "content": a.content,
        }
        for a in arts
    ]
    branches = _branches(db, tenant.id)
    contact_profile = {
        "name": (contact.name if contact else "") or None,
        "language": lang,
        "opted_out_of_marketing": bool(contact.opted_out) if contact else False,
    }
    pctx = prompts.PromptContext(
        now_local=now_local,
        tenant_name=tenant.name,
        niche=tenant.niche,
        tone=ai_cfg.get("tone", "neutral"),
        language=lang,
        branches=branches,
        services=_services(db, tenant.id, lang),
        articles=article_dicts,
        contact=contact_profile,
        state=state,
        forbidden_topics=list(ai_cfg.get("forbidden_topics") or []),
        allowed_tags=allowed_tags(),
        requires_admin_confirmation=bool(settings.get("requires_admin_confirmation")),
        channel=conv.channel if conv else "whatsapp",
    )
    ctx = ToolContext(
        db=db,
        tenant_id=tenant.id,
        contact_id=contact.id if contact else None,
        conversation_id=conv.id if conv else None,
        trigger_message_id=trigger_id,
        channel=conv.channel if conv else "whatsapp",
        language=lang,
        branch_id=branch.id if branch else None,
        timezone=tz_name,
        dry_run=dry_run,
    )
    req = LLMRequest(
        system_stable=prompts.STABLE_SYSTEM_PROMPT,
        system_dynamic=prompts.build_context_block(pctx),
        messages=history,
        tools=TOOL_DEFINITIONS,
        language=lang,
        latest_text=text,
        state=state,
        today=now_local.date(),
        now_local=now_local,
        tenant_name=tenant.name,
        contact_name=(contact.name if contact else "") or "",
        branches=branches,
        articles=article_dicts,
    )
    started = time.perf_counter()
    try:
        result = llm.run(req, tool_executor(ctx))
        reply = (result.text or "").strip()
        if not reply:
            if not ctx.escalated:
                raise LLMError("empty reply")
            reply = prompts.FALLBACK_REPLIES["escalated"].get(
                lang, prompts.FALLBACK_REPLIES["escalated"]["ru"]
            )
    except Exception as exc:
        raise TurnFailed(
            exc,
            provider=llm.provider,
            model=llm.model,
            latency_ms=int((time.perf_counter() - started) * 1000),
            tool_calls=ctx.calls,
            article_ids=article_ids,
        ) from exc
    latency = int((time.perf_counter() - started) * 1000)
    reply, guard = _guard_reply(reply, ctx, lang)
    if guard:
        ctx.calls.append({"name": "_guard", "input": {}, "ok": False, "error": guard})
    outcome = TurnOutcome(
        reply_text=reply,
        language=lang,
        tool_calls=ctx.calls,
        used_article_ids=article_ids,
        escalated=ctx.escalated,
        booking_done=ctx.booking_done,
    )
    return outcome, ctx, result, article_ids, latency


def run_turn(db: Session, conv: Conversation, trigger: Message, *, llm: LLM | None = None) -> TurnOutcome:
    """Run the AI for one inbound client message and persist state + AIRun (no delivery, no commit)."""
    tenant = db.get(Tenant, conv.tenant_id)
    assert tenant is not None
    settings = merged_settings(tenant)
    contact = db.scalar(
        select(Contact).where(Contact.id == conv.contact_id, Contact.tenant_id == conv.tenant_id)
    )
    text = _message_text(trigger)

    lang = _choose_language(trigger.text or "", conv.language, tenant.default_language)  # AT-16
    if lang != conv.language:
        conv.language = lang
    if contact is not None and contact.language != lang:
        contact.language = lang
    trigger.language = lang

    history = build_history(db, conv, trigger, int(settings.get("ai", {}).get("max_history_messages", 20)))
    state = dict(conv.state or {})
    branch = _primary_branch(db, conv.tenant_id, conv.branch_id)
    try:
        llm = llm or get_llm()
    except LLMError as exc:
        raise TurnFailed(exc, provider="none", model="", latency_ms=0, tool_calls=[], article_ids=[]) from exc

    outcome, ctx, result, article_ids, latency = _run(
        db,
        tenant,
        text=text,
        lang=lang,
        history=history,
        state=state,
        contact=contact,
        conv=conv,
        trigger_id=trigger.id,
        branch=branch,
        llm=llm,
        dry_run=False,
        include_drafts=False,
    )
    _auto_crm(ctx, text)
    state["language"] = lang
    conv.state = state  # new dict -> JSON column change is detected
    run = AIRun(
        tenant_id=conv.tenant_id,
        conversation_id=conv.id,
        trigger_message_id=trigger.id,
        provider=llm.provider,
        model=llm.model,
        prompt_version=prompts.PROMPT_VERSION,
        knowledge_article_ids=article_ids,
        tool_calls=ctx.calls,
        reply_text=outcome.reply_text,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        latency_ms=latency,
        error=next((c["error"] for c in ctx.calls if c["name"] == "_guard"), None),
    )
    db.add(run)
    db.flush()
    outcome.ai_run_id = run.id
    outcome.tool_calls = ctx.calls
    return outcome


def record_failed_run(db: Session, conv: Conversation, trigger: Message, exc: BaseException) -> AIRun:
    """Audit a failed turn (called after the turn's SAVEPOINT was rolled back)."""
    tf = exc if isinstance(exc, TurnFailed) else None
    run = AIRun(
        tenant_id=conv.tenant_id,
        conversation_id=conv.id,
        trigger_message_id=trigger.id,
        provider=tf.provider if tf else "unknown",
        model=tf.model if tf else "",
        prompt_version=prompts.PROMPT_VERSION,
        knowledge_article_ids=tf.article_ids if tf else [],
        tool_calls=[{**c, "rolled_back": True} for c in tf.tool_calls] if tf else [],
        latency_ms=tf.latency_ms if tf else 0,
        error=str(exc)[:2000],
    )
    db.add(run)
    db.flush()
    return run


def run_sandbox(
    db: Session,
    tenant_id: uuid.UUID,
    question: str,
    language: str | None = None,
    *,
    include_drafts: bool = False,
    llm: LLM | None = None,
) -> TurnOutcome:
    """Knowledge-base test (/knowledge/test): same prompt, tools and knowledge, but every mutating tool is
    stubbed and everything the turn touched is rolled back."""
    tenant = db.get(Tenant, tenant_id)
    assert tenant is not None
    lang = language or detect_language(question) or tenant.default_language
    sp = db.begin_nested()
    try:
        outcome, *_ = _run(
            db,
            tenant,
            text=question,
            lang=lang,
            history=[{"role": "user", "content": question}],
            state={},
            contact=None,
            conv=None,
            trigger_id=None,
            branch=_primary_branch(db, tenant_id, None),
            llm=llm or get_llm(),
            dry_run=True,
            include_drafts=include_drafts,
        )
    finally:
        sp.rollback()
    return outcome
