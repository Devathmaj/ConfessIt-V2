"""PostgreSQL engine and SQLAlchemy session lifecycle."""
import uuid
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import settings
from .db_models import Base


def new_id() -> str:
    """Create a canonical UUID string for API models that need client-side IDs."""
    return str(uuid.uuid4())


def is_valid_id(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


class PostgresClient:
    """Own the PostgreSQL engine and provide SQLAlchemy sessions."""

    def __init__(self):
        self.engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False, class_=Session)

    def initialize(self) -> None:
        Base.metadata.create_all(self.engine)

    def close(self) -> None:
        self.engine.dispose()


# Existing service annotations use this name; it now denotes only the engine/session provider.
Database = PostgresClient
