"""Alembic environment. Online migrations only.

The database comes from, in order: a connection passed in config.attributes["connection"]
(used by the tests), then DATABASE_URL via parlaytracker.core.config.
"""
from alembic import context

from parlaytracker.core.db import make_engine
from parlaytracker.core.models import Base

target_metadata = Base.metadata


def run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


connection = context.config.attributes.get("connection")
if connection is not None:
    run_migrations(connection)
else:
    engine = make_engine()
    with engine.connect() as conn:
        run_migrations(conn)
    engine.dispose()
