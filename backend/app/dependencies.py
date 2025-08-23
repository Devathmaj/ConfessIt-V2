import logging
from pymongo import MongoClient
from .config import settings

def get_db():
    client = MongoClient(settings.MONGO_URI)
    db = client[settings.MONGO_DB_NAME]
    try:
        yield db
    finally:
        client.close()
