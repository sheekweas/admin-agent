"""Explicit response models for endpoints that historically returned plain dicts (OpenAPI completeness).

The JSON produced is byte-for-byte what the dict endpoints returned before (tests/test_api_contract_snapshot.py):
- datetimes keep Python's ``isoformat()`` (``+00:00`` offsets, like FastAPI's jsonable_encoder), not ``Z``;
- money stays a decimal STRING (``"5000.00"``, ``"0"``) — never a float;
- KPI rates are percentage points on a 0–100 scale (``12.5`` means 12.5 %), not 0–1 fractions.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, WithJsonSchema

_DT_SCHEMA = WithJsonSchema({"type": "string", "format": "date-time"})

# A datetime serialized exactly like jsonable_encoder does (isoformat, "+00:00").
DateTimeOut = Annotated[
    datetime, PlainSerializer(lambda v: v.isoformat(), return_type=str, when_used="json"), _DT_SCHEMA
]
# A value that is already an ISO-8601 string (passed through untouched).
IsoDateTimeStr = Annotated[str, _DT_SCHEMA]
MoneyStr = Annotated[
    str,
    Field(
        pattern=r"^-?\d+(\.\d+)?$",
        description="Money as a decimal string in the organization currency (never a float).",
        examples=["5000.00"],
    ),
]
Percent = Annotated[
    float,
    Field(
        description="Percentage points on a 0–100 scale with one decimal (12.5 means 12.5 %).",
        json_schema_extra={"minimum": 0},
        examples=[42.5],
    ),
]


# ---------------------------------------------------------------- errors (documentation)
class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="allow")
    code: str = Field(examples=["NOT_FOUND"])
    message: str


class ErrorResponse(BaseModel):
    detail: ErrorDetail


def error_responses(*codes: int, **descriptions: str) -> dict[int | str, dict[str, Any]]:
    defaults = {
        401: "Missing or invalid token",
        403: "Missing permission / no access to the branch or organization",
        404: "Not found — ids of other organizations are indistinguishable from missing ones",
        409: "Conflict with the current state",
        413: "Request body too large",
        422: "Validation error",
        429: "Too many requests (per-IP rate limit)",
    }
    return {
        c: {"model": ErrorResponse, "description": descriptions.get(f"e{c}", defaults.get(c, ""))}
        for c in codes
    }


# ---------------------------------------------------------------- payments
class PaymentOut(BaseModel):
    id: uuid.UUID
    booking_id: uuid.UUID | None
    visit_id: uuid.UUID | None
    contact_id: uuid.UUID | None
    branch_id: uuid.UUID | None
    staff_id: uuid.UUID | None
    kind: str = Field(description="payment | prepayment | refund | adjustment")
    amount: MoneyStr = Field(
        description="Positive decimal string; refunds are stored positive with kind=refund."
    )
    currency: str = Field(examples=["KZT"])
    method: str = Field(description="cash | card | transfer | online")
    status: str = Field(description="confirmed | refunded | cancelled")
    confirmation_source: str
    transaction_ref: str | None
    paid_at: DateTimeOut | None
    comment: str
    created_by: uuid.UUID | None
    refund_of_id: uuid.UUID | None
    refunded_amount: MoneyStr | None = Field(description="Sum refunded so far; null for refund rows.")
    created_at: DateTimeOut | None


class PaymentList(BaseModel):
    items: list[PaymentOut]
    total: int


# ---------------------------------------------------------------- integrations
class IntegrationOut(BaseModel):
    id: uuid.UUID | None
    kind: Literal["whatsapp", "telegram", "voice", "zapier"]
    enabled: bool
    configured: bool
    status: str = Field(description="ok | error | unknown")
    last_error: str | None
    config: dict[str, Any] = Field(description="Non-secret configuration (e.g. phone_number_id).")
    secrets_set: list[str] = Field(description="Names of the secrets that are stored.")
    secrets: dict[str, str] = Field(description='Masked previews only ("••••abcd"); secrets are write-only.')
    updated_at: DateTimeOut | None


class IntegrationList(BaseModel):
    items: list[IntegrationOut]


class IntegrationTestOut(BaseModel):
    ok: bool
    detail: str


class STTStatusOut(BaseModel):
    provider: Literal["openai_compatible", "mock", "none"]
    configured: bool = Field(description="False when STT_PROVIDER=none or STT_BASE_URL is missing.")
    model: str | None


class STTTestOut(BaseModel):
    ok: bool
    provider: str
    latency_ms: int
    error: str | None


class WebhookSubscriptionOut(BaseModel):
    id: uuid.UUID
    url: str
    events: list[str]
    include_pii: bool
    enabled: bool
    created_at: DateTimeOut | None


class WebhookSubscriptionCreated(WebhookSubscriptionOut):
    secret: str = Field(
        description="HMAC secret for X-CRM-Signature. Returned ONLY in this creation response — store it now."
    )


class WebhookSubscriptionList(BaseModel):
    items: list[WebhookSubscriptionOut]
    total: int
    available_events: list[str]


class NotificationEventOut(BaseModel):
    id: uuid.UUID
    source: Literal["notification"]
    channel: str = Field(description="telegram | whatsapp | voice | webhook")
    kind: str
    recipient: str = Field(description="Masked phone / chat id / webhook host.")
    status: str = Field(description="pending | sent | failed | cancelled | dead")
    attempts: int
    next_attempt_at: DateTimeOut | None
    scheduled_for: DateTimeOut | None
    last_error: str | None
    provider_ref: str | None
    event_id: uuid.UUID | None
    created_at: DateTimeOut | None
    updated_at: DateTimeOut | None


class OutboxEventOut(BaseModel):
    id: uuid.UUID
    source: Literal["outbox"]
    channel: Literal["outbox"]
    kind: str = Field(description="Event type, e.g. booking.created")
    status: str = Field(description="pending | processed | failed | dead")
    attempts: int
    next_attempt_at: DateTimeOut | None
    last_error: str | None
    aggregate_type: str
    aggregate_id: uuid.UUID | None
    created_at: DateTimeOut | None
    processed_at: DateTimeOut | None


DeliveryEventOut = Annotated[NotificationEventOut | OutboxEventOut, Field(discriminator="source")]


class DeliveryEventList(BaseModel):
    items: list[DeliveryEventOut]
    total: int


# ---------------------------------------------------------------- audit
class AuditEntryOut(BaseModel):
    id: uuid.UUID
    actor_type: str = Field(description="user | ai | system | telegram | ...")
    actor_id: str | None
    action: str = Field(examples=["booking.cancel"])
    entity: str | None
    entity_id: str | None
    payload: dict[str, Any] | None
    ip: str | None
    created_at: DateTimeOut | None


class AuditList(BaseModel):
    items: list[AuditEntryOut]
    total: int


# ---------------------------------------------------------------- reports
class KpiOut(BaseModel):
    revenue: MoneyStr | None = Field(description="Confirmed payments (gross). null without finance.read.")
    refunds: MoneyStr | None = Field(description="Confirmed refunds. null without finance.read.")
    net_revenue: MoneyStr | None = Field(description="revenue − refunds. null without finance.read.")
    bookings: int
    visits: int
    no_shows: int
    cancellations: int
    new_leads: int
    conversion_to_booking: Percent
    conversion_to_visit: Percent
    no_show_rate: Percent
    avg_check: MoneyStr | None = Field(description="Average confirmed payment. null without finance.read.")
    ai_resolution_rate: Percent = Field(
        description="Share of AI conversations closed without a human (0–100)."
    )
    open_tasks: int
    unpaid_completed: int


class RevenueDay(BaseModel):
    date: date
    amount: MoneyStr | None


class BookingsDay(BaseModel):
    date: date
    bookings: int
    visits: int


class ServiceRow(BaseModel):
    service_id: uuid.UUID | None
    title: str
    revenue: MoneyStr | None
    count: int


class StaffRow(BaseModel):
    staff_id: uuid.UUID
    name: str
    revenue: MoneyStr | None
    visits: int
    utilization: Percent = Field(
        description="Booked / available minutes, percentage points (may exceed 100)."
    )


class SourceRow(BaseModel):
    source: str
    count: int


class StageRow(BaseModel):
    stage: str
    count: int


class HourRow(BaseModel):
    hour: int = Field(ge=0, le=23)
    bookings: int


class CancellationsDay(BaseModel):
    date: date
    cancelled: int
    no_show: int


class ReportOverview(BaseModel):
    date_from: date
    date_to: date
    currency: str = Field(examples=["KZT"])
    kpi: KpiOut
    revenue_by_day: list[RevenueDay]
    bookings_by_day: list[BookingsDay]
    top_services: list[ServiceRow]
    top_staff: list[StaffRow]
    lead_sources: list[SourceRow]
    funnel: list[StageRow]
    load_by_hour: list[HourRow]
    cancellations_by_day: list[CancellationsDay]


# ---------------------------------------------------------------- inbox
class ConversationContactOut(BaseModel):
    id: uuid.UUID
    name: str
    phone_e164: str | None
    opted_out: bool


class LastMessageOut(BaseModel):
    text: str
    author: str
    at: IsoDateTimeStr | None


class ConversationOut(BaseModel):
    id: uuid.UUID
    contact: ConversationContactOut | None
    channel: str
    mode: str = Field(description="ai | human | waiting_admin")
    status: str
    language: str | None
    branch_id: uuid.UUID | None
    owner_user_id: uuid.UUID | None
    last_message: LastMessageOut | None
    last_message_at: IsoDateTimeStr | None
    unread_count: int
    open_task_id: uuid.UUID | None


class ConversationDetail(ConversationOut):
    state: dict[str, Any] = Field(description="AI dialogue state (slots collected so far).")


class ConversationList(BaseModel):
    items: list[ConversationOut]
    total: int


class MessageOut(BaseModel):
    id: uuid.UUID
    direction: Literal["in", "out"]
    author: str = Field(description="client | ai | human | system")
    message_type: str
    text: str
    media_url: str | None
    provider_status: str = Field(description="received | queued | sent | delivered | read | failed")
    is_internal_note: bool
    received_at: IsoDateTimeStr | None
    author_user_id: uuid.UUID | None
    error: str | None
    language: str | None
    meta: dict[str, Any] = Field(
        default_factory=dict,
        description="Channel metadata. Voice notes: transcribed (bool), stt (provider), stt_confidence "
        "(0–1|null), stt_language (ru|kk|null), stt_error (str|null), stt_low_confidence (true when the "
        "client was asked to repeat).",
    )


class MessageList(BaseModel):
    items: list[MessageOut]
    total: int


class SimulateOut(BaseModel):
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    mode: str | None
    replies: list[MessageOut]


# ---------------------------------------------------------------- knowledge
class ArticleOut(BaseModel):
    id: uuid.UUID
    section: str
    language: str
    title: str
    content: str
    status: str = Field(description="draft | published | archived")
    version: int
    author_id: uuid.UUID | None
    test_questions: list[str]
    created_at: IsoDateTimeStr | None
    updated_at: IsoDateTimeStr | None


class ArticleList(BaseModel):
    items: list[ArticleOut]
    total: int


class SandboxToolCall(BaseModel):
    name: str | None
    input: dict[str, Any] | None
    ok: bool | None
    error: str | None
    result: dict[str, Any] | None


class UsedArticle(BaseModel):
    id: uuid.UUID
    title: str
    status: str
    version: int


class KnowledgeTestOut(BaseModel):
    reply: str
    language: str
    escalated: bool
    tool_calls: list[SandboxToolCall]
    used_articles: list[UsedArticle]


# ---------------------------------------------------------------- voice (calls)
CallDirection = Literal["in", "out"]
CallPurpose = Literal[
    "client_ai", "admin_escalation", "booking_confirmation", "reminder", "callback", "reschedule"
]
CallStatus = Literal[
    "queued", "ringing", "in_progress", "answered", "completed", "no_answer", "busy", "failed", "cancelled"
]


class CallContactOut(BaseModel):
    id: uuid.UUID
    name: str
    phone_e164: str | None = Field(description="Masked for the analyst role.")


class CallOut(BaseModel):
    id: uuid.UUID
    direction: CallDirection
    purpose: CallPurpose
    status: CallStatus
    from_number: str | None = Field(description="E.164; masked for the analyst role.")
    to_number: str | None = Field(description="E.164; masked for the analyst role.")
    contact: CallContactOut | None
    admin_task_id: uuid.UUID | None
    booking_id: uuid.UUID | None
    started_at: IsoDateTimeStr | None = Field(description="When the call was answered (null if never).")
    ended_at: IsoDateTimeStr | None
    duration_sec: int
    transferred_to_human: bool
    recording_available: bool = Field(description="A recording exists (only recorded with consent).")
    created_at: IsoDateTimeStr | None


class CallList(BaseModel):
    items: list[CallOut]
    total: int


class TranscriptEntry(BaseModel):
    speaker: Literal["client", "ai", "admin", "system"]
    text: str
    at: IsoDateTimeStr | None
    confidence: float | None = Field(description="Speech-recognition confidence 0–1 (client turns only).")


class CallMetricsOut(BaseModel):
    first_response_ms: int | None = Field(description="Time to the first voice reply to the caller, ms.")
    turns: int = Field(description="Recognised client utterances.")
    recognition_retries: int = Field(description="Silent / low-confidence utterances that were re-asked.")
    transferred_to_human: bool
    booking_created: bool


class CallDetail(CallOut):
    transcript: list[TranscriptEntry] = Field(description="Empty after the retention period.")
    metrics: CallMetricsOut


class VoiceReport(BaseModel):
    calls: int = Field(description="Client calls (inbound AI + outbound) created in the period.")
    answered: int
    avg_duration_sec: float
    avg_first_response_ms: float
    transfer_rate: Percent = Field(description="Answered calls transferred to a human, 0–100.")
    booking_rate: Percent = Field(description="Answered calls that created a booking, 0–100.")
    recognition_retry_rate: Percent = Field(description="Failed recognitions / all client utterances, 0–100.")


# ---------------------------------------------------------------- webhooks
class WhatsAppWebhookAck(BaseModel):
    status: Literal["ok"]
    accepted: int = Field(description="New inbound messages queued for the AI (duplicates are not counted).")


class OkOut(BaseModel):
    ok: bool


class OkStatus(BaseModel):
    status: Literal["ok"]
