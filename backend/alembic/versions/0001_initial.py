"""initial schema + DB-level double-booking protection

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-14
"""
from alembic import op

import app.models  # noqa: F401
from app.db import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

ACTIVE = "('held','pending','confirmed','checked_in')"

CONSTRAINTS_SQL = [
    "CREATE EXTENSION IF NOT EXISTS btree_gist",
    # ТЗ §8.3 / §17.1: no overlapping active bookings for one staff member (buffers included)
    f"""ALTER TABLE bookings ADD CONSTRAINT ex_bookings_staff_overlap
        EXCLUDE USING gist (staff_id WITH =, tstzrange(occupied_start, occupied_end, '[)') WITH &&)
        WHERE (status IN {ACTIVE})""",
    # ... and for one limited resource (chair/cabinet with capacity 1)
    f"""ALTER TABLE bookings ADD CONSTRAINT ex_bookings_resource_overlap
        EXCLUDE USING gist (resource_id WITH =, tstzrange(occupied_start, occupied_end, '[)') WITH &&)
        WHERE (resource_id IS NOT NULL AND status IN {ACTIVE})""",
    "ALTER TABLE bookings ADD CONSTRAINT ck_bookings_time CHECK (end_at > start_at AND occupied_end > occupied_start)",
    "ALTER TABLE payments ADD CONSTRAINT ck_payments_amount CHECK (amount >= 0)",
    "CREATE INDEX IF NOT EXISTS ix_contacts_tenant_name ON contacts (tenant_id, lower(name))",
    "CREATE INDEX IF NOT EXISTS ix_leads_contact_created ON leads (contact_id, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_outbox_pending ON outbox_events (created_at) WHERE status = 'pending'",
]


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)
    for sql in CONSTRAINTS_SQL:
        op.execute(sql)


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
