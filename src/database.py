"""Database connection and session management."""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

try:
    from .models import Base
except ImportError:
    from models import Base


def get_database_url() -> str:
    """Get database URL from environment or use default SQLite for testing."""
    db_url = os.getenv('DATABASE_URL')
    if db_url:
        return db_url
    # Default to SQLite for local testing
    return "sqlite:///./parlaytracker.db"


def create_db_engine():
    """Create and return database engine."""
    db_url = get_database_url()
    echo = os.getenv('SQL_ECHO', 'false').lower() == 'true'
    return create_engine(db_url, echo=echo)


def init_db(engine=None):
    """Initialize database tables."""
    if engine is None:
        engine = create_db_engine()
    Base.metadata.create_all(engine)
    return engine


def get_session(engine=None) -> Session:
    """Create and return a database session."""
    if engine is None:
        engine = create_db_engine()
    SessionLocal = sessionmaker(bind=engine)
    return SessionLocal()
