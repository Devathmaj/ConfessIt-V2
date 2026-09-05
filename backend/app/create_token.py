# app/create_token.py

import logging
import secrets
import hashlib
from datetime import datetime, timedelta, timezone
from .postgres import Database
from .db_models import LoginTokenRecord
from fastapi import Request

from .models import LoginToken

def hash_token(token: str) -> str:
    """
    Used to hash a token using SHA-256 for secure storage.
    """
    return hashlib.sha256(token.encode()).hexdigest()

def create_and_store_magic_token(db: Database, user_id: str, request: Request) -> str:
    """
    Used to generate a magic link token, store its hashed version in the database,
    and return the unhashed token.
    """
    # 1. Generate a new, secure token
    token = secrets.token_urlsafe(32)
    
    # 2. Hash the token for database storage
    token_hash = hash_token(token)
    
    # 3. Set the token's issuance and expiration timestamps
    issued_at = datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(hours=1)
    
    # 4. Create the LoginToken document
    login_token_entry = LoginToken(
        user_id=user_id,
        token_hash=token_hash,
        issued_at=issued_at,
        expires_at=expires_at,
        consumed_at=None,
        used=False,
        revoked=False,
        request_ip=request.client.host,
        request_user_agent=request.headers.get("user-agent", "unknown"),
        consume_ip=None,
        consume_user_agent=None,
        attempt_count=0,
        session_status="issued",
        last_seen_at=None,
        device=None,
    )
    
    # 5. Insert the new token document into the database
    with db.sessions.begin() as session:
        session.add(LoginTokenRecord(
            id=login_token_entry.id,
            user_id=login_token_entry.user_id,
            token_hash=login_token_entry.token_hash,
            issued_at=login_token_entry.issued_at,
            expires_at=login_token_entry.expires_at,
            consumed_at=login_token_entry.consumed_at,
            used=login_token_entry.used,
            revoked=login_token_entry.revoked,
            request_ip=login_token_entry.request_ip,
            request_user_agent=login_token_entry.request_user_agent,
            consume_ip=login_token_entry.consume_ip,
            consume_user_agent=login_token_entry.consume_user_agent,
            attempt_count=login_token_entry.attempt_count,
            session_status=login_token_entry.session_status,
            last_seen_at=login_token_entry.last_seen_at,
            device=login_token_entry.device,
        ))
    
    # 6. Return the unhashed token to be used in the magic link
    return token
