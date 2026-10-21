from .postgres import Database, get_postgres_client


def get_db() -> Database:
    """Dependency yielding the process-wide engine/session provider."""
    return get_postgres_client()
