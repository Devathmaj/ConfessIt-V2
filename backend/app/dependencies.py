from .postgres import Database, get_postgres_client


def get_db() -> Database:
    """Dependency yielding the process-wide engine/session provider.

    The client is shared, so this must not close it: its pool outlives any single
    request. Shutdown disposal is handled by ``close_postgres_client``.
    """
    return get_postgres_client()
