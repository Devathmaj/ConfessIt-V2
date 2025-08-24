import logging
from pymongo import MongoClient
from .config import settings

client = MongoClient(settings.MONGO_URI)

def get_db():
    """
    Dependency to get database connection.
    FastAPI will automatically manage the connection lifecycle.
    """
    try:
        db = client[settings.MONGO_DB_NAME]
        yield db
    finally:
        # Connection will be returned to the pool
        # No explicit close needed as pymongo handles connection pooling
        pass
