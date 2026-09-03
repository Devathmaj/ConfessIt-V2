import logging
from datetime import datetime

# UUID conversion functions removed - Supabase now uses text fields for Regno and ObjectId


def isoformat_datetime(value):
    """Serialize PostgreSQL ISO timestamp strings and Python datetimes uniformly."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
        except ValueError:
            return value
    return None

