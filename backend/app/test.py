import psycopg
from .postgres import PostgresClient
import secrets
from datetime import datetime, timedelta, timezone
import hashlib
from sqlalchemy import select
from .db_models import LoginTokenRecord, UserRecord

def create_magic_link_token() -> str:
    """
    Generates a secure, random token for magic link authentication.
    """
    return secrets.token_urlsafe(32)

def hash_token(token: str) -> str:
    """
    Hashes a token using SHA-256 for secure storage.
    """
    return hashlib.sha256(token.encode()).hexdigest()

def main():
    """
    Main function to connect to the database, insert user data,
    and generate and store magic link tokens.
    """
    try:
        # A one-shot script owns its own engine and disposes it in the finally block.
        client = PostgresClient()

        # Define the user and admin documents
        users = [
            {"regno": "user", "name": "Test User", "email": "user@test.com", "class_name": "Test_Class",
             "profile_picture_url": None, "gender": "male", "matchmaking_enabled": True,
             "notifications_enabled": True},
            {"regno": "admin", "name": "Test Admin", "email": "admin@test.com", "class_name": "Test_Class",
             "profile_picture_url": None, "gender": "male", "matchmaking_enabled": True,
             "notifications_enabled": True, "role": "admin"},
        ]

        issued_tokens = []
        with client.sessions.begin() as session:
            for user in users:
                user_row = session.scalar(select(UserRecord).where(UserRecord.email == user["email"]))
                if not user_row:
                    user_row = UserRecord(**user)
                    session.add(user_row)
                    session.flush()
                    print(f"Successfully inserted user: {user['name']}")
                else:
                    print(f"User with email {user['email']} already exists.")

                token = create_magic_link_token()
                issued_at = datetime.now(timezone.utc)
                token_row = LoginTokenRecord(
                    user_id=user_row.id, token_hash=hash_token(token), issued_at=issued_at,
                    expires_at=issued_at + timedelta(hours=1), used=False, revoked=False,
                    request_ip="127.0.0.1", request_user_agent="Test Script", attempt_count=0,
                    session_status="issued",
                )
                session.add(token_row)
                issued_tokens.append((user["name"], user["email"], token))

        for name, email, token in issued_tokens:
            print(f"Successfully stored token for {name}.")
            print(f"\nMagic link token for {name} ({email}):")
            print(token)


    except psycopg.Error as e:
        print(f"Could not connect to PostgreSQL: {e}")
    except Exception as e:
        print(f"An error occurred: {e}")
    finally:
        # Ensure the client connection is closed
        if 'client' in locals() and client:
            client.close()

if __name__ == "__main__":
    main()
