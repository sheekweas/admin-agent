# Internal service contracts (shared between modules)

All service functions take `db: Session` and `tenant_id: uuid.UUID` first. They **do not commit** unless
stated; the API layer / worker owns the transaction (`db.commit()`). Errors are raised as
`app.services.common.DomainError(status, CODE, message, **extra)`.

## Booking engine — `app/services/booking.py` (owner: booking agent)

```python
def get_available_slots(db, tenant_id, *, service_id, date_from: date, date_to: date,
                        staff_id: UUID | None = None, branch_id: UUID | None = None,
                        limit: int = 50) -> list[Slot]
# Slot = dataclass(staff_id, staff_name, branch_id, start_at: datetime(tz aware UTC), end_at, slot_id: str)
# slot_id is an opaque string "<staff_id>|<iso start utc>|<service_id>" that create_booking accepts.

def create_booking(db, tenant_id, *, contact_id, service_id, start_at: datetime | None = None,
                   staff_id: UUID | None = None, slot_id: str | None = None, branch_id=None,
                   idempotency_key: str | None, channel: str, source: str,
                   actor_type: str, actor_id: str | None, comment: str = "",
                   status: str = "confirmed") -> Booking
# Atomic: locks staff row FOR UPDATE, re-validates schedule, inserts; DB exclusion constraint
# is the final guard. Same idempotency_key -> returns existing booking.
# Raises DomainError(409, "SLOT_UNAVAILABLE", ..., alternatives=[slot dicts]).
# Emits outbox booking.created + booking.confirmed (if confirmed), updates lead stage to "booked".

def reschedule_booking(db, tenant_id, booking_id, *, new_start_at=None, new_staff_id=None, new_slot_id=None,
                       actor_type, actor_id, contact_id: UUID | None = None) -> Booking  # returns NEW booking
def cancel_booking(db, tenant_id, booking_id, *, reason: str, actor_type, actor_id,
                   contact_id: UUID | None = None) -> Booking
def set_booking_status(db, tenant_id, booking_id, status: str, *, actor_type, actor_id) -> Booking
def get_booking_for_contact(db, tenant_id, booking_id_or_number, contact_id) -> Booking | None
def list_contact_bookings(db, tenant_id, contact_id, upcoming_only=True) -> list[Booking]
def booking_to_dict(b: Booking) -> dict
```
`contact_id` on reschedule/cancel = ownership check for AI/client calls (ТЗ §7.3).

## CRM — `app/services/crm.py` (owner: booking agent)
```python
def get_or_create_contact_by_channel(db, tenant_id, *, channel: str, external_id: str,
                                     phone: str | None, name: str | None, language: str | None) -> Contact
def open_or_get_lead(db, tenant_id, contact_id, *, source: str, service_id=None) -> Lead   # no duplicates: reuses open lead
def set_lead_stage(db, tenant_id, lead_id, stage: str, *, reason: str = "", actor_type="system", actor_id=None) -> Lead
def add_contact_tags(db, tenant_id, contact_id, tags: list[str]) -> None   # only from ALLOWED_AUTO_TAGS for AI
def set_opt_out(db, tenant_id, contact_id, opted_out: bool, actor_type, actor_id) -> None
```

## Escalation — `app/services/escalation.py` (owner: ops agent)
```python
def create_admin_task(db, tenant_id, *, question: str, conversation_id=None, contact_id=None,
                      kind="question", priority="normal", branch_id=None, payload=None) -> AdminTask
# sets next_escalation_at = now + repeat_after_min; emits admin_task.created (Telegram send via outbox)
def ack_task(db, tenant_id, task_id, user_id, via: str) -> AdminTask      # stops timers (AT-06)
def resolve_task(db, tenant_id, task_id, user_id, answer: str, relay_to_client: bool, add_to_knowledge: bool) -> AdminTask
def process_due_escalations(db, now=None) -> int   # called by worker every 30s
```

## Messaging — `app/services/messaging.py` (owner: AI/WhatsApp agent)
```python
def send_client_message(db, tenant_id, conversation_id, text: str, *, author: str = "ai",
                        author_user_id=None) -> Message   # stores outbound msg and queues delivery
def handle_inbound(db, normalized: NormalizedMessage) -> None   # used by webhooks (idempotent)
```

## Notifications — `app/services/notifications.py` (owner: ops agent)
```python
def notify_admins(db, tenant_id, kind: str, text: str, *, buttons=None, branch_id=None, dedupe_key=None) -> list[Notification]
```

## Outbox
`app.services.common.emit_event(db, tenant_id, event_type, aggregate_type, aggregate_id, payload)` —
the worker (`app/workers/tasks.py::dispatch_outbox`) fans events out to Telegram, client WhatsApp
confirmations, reminders scheduling and webhook subscriptions (Zapier).
