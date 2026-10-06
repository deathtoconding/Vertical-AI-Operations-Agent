"""Widen ``incidents.escalation_reason`` to free text.

Revision ID: 0002_widen_escalation_reason
Revises: 0001_initial_schema
Create Date: 2026-10-06 10:34:00.000000+00:00

Why: escalation reasons are sentences (they name the rule, the check and the observed value).
A 48-character column silently truncates the one field an on-call engineer reads first — or,
worse, raises a StringDataRightTruncation that turns an escalation into a 500. Widening it is
the honest fix; shortening the reason would be losing information to fit a schema mistake.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_widen_escalation_reason"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "incidents",
        "escalation_reason",
        existing_type=sa.String(length=48),
        type_=sa.Text(),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "incidents",
        "escalation_reason",
        existing_type=sa.Text(),
        type_=sa.String(length=48),
        existing_nullable=True,
    )
