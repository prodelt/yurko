"""initial schema: laws, law_versions, articles, article_embeddings + Ukrainian FTS

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-06-06

Applies db/schema.sql so the migration and PostgresRepository.ensure_schema()
share a single source of truth. The SQL is idempotent.
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "db" / "schema.sql"


def upgrade() -> None:
    op.execute(_SCHEMA_PATH.read_text(encoding="utf-8"))


def downgrade() -> None:
    op.execute("""
        DROP TABLE IF EXISTS article_embeddings;
        DROP TABLE IF EXISTS articles;
        DROP TABLE IF EXISTS law_versions;
        DROP TABLE IF EXISTS laws;
        DROP TEXT SEARCH CONFIGURATION IF EXISTS ukrainian;
        """)
