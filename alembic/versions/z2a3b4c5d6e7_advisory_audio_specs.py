"""Replace active pre-master handoff requirements with advisory source/master specs.

Revision ID: z2a3b4c5d6e7
Revises: y1z2a3b4c5d6
"""
import json
from alembic import op
import sqlalchemy as sa

revision = "z2a3b4c5d6e7"
down_revision = "y1z2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    for table, old, new in (
        ("albums", "premaster_spec", "audio_specs"),
        ("tracks", "premaster_spec_override", "audio_spec_overrides"),
    ):
        if not sa.inspect(bind).has_table(table):
            continue
        columns = {c["name"] for c in sa.inspect(bind).get_columns(table)}
        if new not in columns:
            with op.batch_alter_table(table) as batch:
                batch.add_column(sa.Column(new, sa.Text(), nullable=True))
        for row in bind.execute(sa.text(f"SELECT id, {old} FROM {table} WHERE {old} IS NOT NULL AND {new} IS NULL")).mappings():
            value = json.dumps({"source": json.loads(row[old]), "master": None})
            bind.execute(sa.text(f"UPDATE {table} SET {new}=:value WHERE id=:id"), {"value": value, "id": row["id"]})
    # Historical handoffs and source versions remain untouched.


def downgrade():
    for table, column in (("tracks", "audio_spec_overrides"), ("albums", "audio_specs")):
        with op.batch_alter_table(table) as batch:
            batch.drop_column(column)
