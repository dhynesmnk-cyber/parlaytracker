"""Engine and session factory."""
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from parlaytracker.core.config import get_settings, normalize_database_url


def make_engine(url: str | None = None) -> Engine:
    url = normalize_database_url(url) if url else get_settings().database_url
    return create_engine(url, pool_pre_ping=True)


@lru_cache
def session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=make_engine(), expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session] | None = None) -> Iterator[Session]:
    """A session that commits on success and rolls back on any exception.

    Service functions only flush; the caller's session_scope owns the transaction.
    """
    session = (factory or session_factory())()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
