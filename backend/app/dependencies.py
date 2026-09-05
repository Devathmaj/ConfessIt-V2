from .postgres import PostgresClient

client = PostgresClient()

def get_db():
    """
    Dependency to get database connection.
    FastAPI will automatically manage the connection lifecycle.
    """
    yield client
