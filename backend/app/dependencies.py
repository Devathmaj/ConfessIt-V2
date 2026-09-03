import logging
from .postgres import PostgresClient
from .config import settings

client = PostgresClient()

def get_db():
    """
    Dependency to get database connection.
    FastAPI will automatically manage the connection lifecycle.
    """
    try:
        db = client[settings.DATABASE_NAME]
        yield db
    finally:
        # Connection will be returned to the pool
        # Connections are scoped to each operation and returned to PostgreSQL.
        pass
