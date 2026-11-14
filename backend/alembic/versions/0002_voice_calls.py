"""AI voice channel: additive columns on calls (stage 4, ТЗ §13).

Fresh databases already get these columns from 0001 (``Base.metadata.create_all``); this revision brings
databases created before stage 4 up to date. Every statement is idempotent (IF NOT EXISTS).

Revision ID: 0002_voice_calls
Revises: 0001_initial
Create Date: 2026-11-14
"""

from alembic import op

revision = "0002_voice_calls"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

STATEMENTS = [
    "ALTER TABLE calls ADD COLUMN IF NOT EXISTS conversation_id UUID "
    "REFERENCES conversations(id) ON DELETE SET NULL",
    "ALTER TABLE calls ADD COLUMN IF NOT EXISTS booking_id UUID REFERENCES bookings(id)",
    "ALTER TABLE calls ADD COLUMN IF NOT EXISTS language VARCHAR(5)",
    "ALTER TABLE calls ADD COLUMN IF NOT EXISTS started_at TIMESTAMP WITH TIME ZONE",
    "ALTER TABLE calls ADD COLUMN IF NOT EXISTS ended_at TIMESTAMP WITH TIME ZONE",
    "ALTER TABLE calls ADD COLUMN IF NOT EXISTS transcript_items JSONB NOT NULL DEFAULT '[]'::jsonb",
    "ALTER TABLE calls ADD COLUMN IF NOT EXISTS metrics JSONB NOT NULL DEFAULT '{}'::jsonb",
    "ALTER TABLE calls ADD COLUMN IF NOT EXISTS meta JSONB NOT NULL DEFAULT '{}'::jsonb",
    "CREATE INDEX IF NOT EXISTS ix_calls_provider_ref ON calls (provider_ref)",
    "CREATE INDEX IF NOT EXISTS ix_calls_tenant_created ON calls (tenant_id, created_at)",
]


def upgrade() -> None:
    for sql in STATEMENTS:
        op.execute(sql)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_calls_tenant_created")
    for col in ("meta", "metrics", "transcript_items", "ended_at", "started_at", "language", "booking_id"):
        op.execute(f"ALTER TABLE calls DROP COLUMN IF EXISTS {col}")
    op.execute("ALTER TABLE calls DROP COLUMN IF EXISTS conversation_id")
