"""The Alembic migrations must build exactly what the models describe."""
import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, Engine, select, text

from parlaytracker.core.models import Base, Sportsbook

pytestmark = pytest.mark.db

MODEL_SCHEMA = "model_check"


def _catalog(conn: Connection, schema: str) -> dict[str, set]:
    """Columns, constraints and indexes of a schema, with the schema name stripped out."""
    params = {"s": schema}
    columns = conn.execute(text("""
        SELECT table_name, column_name, data_type, is_nullable, column_default,
               character_maximum_length, numeric_precision, numeric_scale
        FROM information_schema.columns
        WHERE table_schema = :s AND table_name <> 'alembic_version'
    """), params)
    constraints = conn.execute(text("""
        SELECT c.relname, con.conname, pg_get_constraintdef(con.oid)
        FROM pg_constraint con
        JOIN pg_class c ON c.oid = con.conrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = :s AND c.relname <> 'alembic_version'
    """), params)
    indexes = conn.execute(text("""
        SELECT tablename, indexname, indexdef FROM pg_indexes
        WHERE schemaname = :s AND tablename <> 'alembic_version'
    """), params)

    def strip(row):
        return tuple(str(v).replace(f"{schema}.", "") if v is not None else None for v in row)

    return {
        "columns": {strip(r) for r in columns},
        "constraints": {strip(r) for r in constraints},
        "indexes": {strip(r) for r in indexes},
    }


def test_migrated_schema_matches_models(engine: Engine):
    with engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {MODEL_SCHEMA} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {MODEL_SCHEMA}"))
        Base.metadata.create_all(conn.execution_options(schema_translate_map={None: MODEL_SCHEMA}))
        migrated, modelled = _catalog(conn, "public"), _catalog(conn, MODEL_SCHEMA)
        conn.execute(text(f"DROP SCHEMA {MODEL_SCHEMA} CASCADE"))
    for kind in ("columns", "constraints", "indexes"):
        assert migrated[kind] == modelled[kind], kind


def test_no_changes_pending_for_autogenerate(engine: Engine):
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        assert compare_metadata(ctx, Base.metadata) == []


def test_downgrade_and_upgrade_again(engine: Engine, make_alembic_config):
    with engine.begin() as conn:
        command.downgrade(make_alembic_config(conn), "base")
        tables = conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )).scalars().all()
        assert tables == ["alembic_version"]
        command.upgrade(make_alembic_config(conn), "head")
        books = conn.execute(select(Sportsbook.name, Sportsbook.odds_api_key)).all()
    assert sorted(books) == [
        ("BetMGM", "betmgm"),
        ("Caesars", "williamhill_us"),
        ("DraftKings", "draftkings"),
        ("FanDuel", "fanduel"),
    ]
