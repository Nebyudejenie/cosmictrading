"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

from alembic import op

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = None
depends_on = None


def upgrade(app_role: str = "cosmic_app") -> None:
    pass


def downgrade(app_role: str = "cosmic_app") -> None:
    pass
