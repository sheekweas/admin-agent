"""Server-side auth sessions and JWT revocation (v1.1.0).

Every access token carries ``sid`` = auth_sessions.id; a token is accepted only while its row is not revoked and
not expired. Tokens issued by v1.0 (no ``sid``) are rejected, so users log in once after the upgrade.
Fresh test databases get the table from ``Base.metadata.create_all``; statements are idempotent (IF NOT EXISTS).

Revision ID: 0003_auth_sessions
Revises: 0002_voice_calls
Create Date: 2026-11-15
"""

from alembic import op

revision = "0003_auth_sessions"
down_revision = "0002_voice_calls"
branch_labels = None
depends_on = None

STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS auth_sessions (
        id UUID PRIMARY KEY,
        user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
        created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
        last_seen_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
        expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
        revoked_at TIMESTAMP WITH TIME ZONE,
        ip VARCHAR(64),
        user_agent VARCHAR(300)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_auth_sessions_user_id ON auth_sessions (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_auth_sessions_tenant_id ON auth_sessions (tenant_id)",
    "CREATE INDEX IF NOT EXISTS ix_auth_sessions_expires_at ON auth_sessions (expires_at)",
]


def upgrade() -> None:
    for sql in STATEMENTS:
        op.execute(sql)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS auth_sessions")
