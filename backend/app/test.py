import psycopg
from .postgres import PostgresClient
from .models import UserDetails
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
        client = PostgresClient()

        # Define the user and admin documents
        users = [
            UserDetails(
                Regno="user",
                Name="Test User",
                email="user@test.com",
                which_class="Test_Class",
                profile_picture_id="random_string_1",
                gender="male",
                isMatchmaking=True,
                isNotifications=True,
            ),
            UserDetails(
                Regno="admin",
                Name="Test Admin",
                email="admin@test.com",
                which_class="Test_Class",
                profile_picture_id="random_string_2",
                gender="male",
                isMatchmaking=True,
                isNotifications=True,
            ),
        ]

        issued_tokens = []
        with client.sessions.begin() as session:
            for user in users:
                user_row = session.scalar(select(UserRecord).where(UserRecord.email == str(user.email)))
                if not user_row:
                    user_row = UserRecord(Regno=user.Regno, Name=user.Name, email=str(user.email),
                        which_class=user.which_class, profile_picture_id=user.profile_picture_id,
                        gender=user.gender, isMatchmaking=user.isMatchmaking,
                        isNotifications=user.isNotifications)
                    session.add(user_row)
                    session.flush()
                    print(f"Successfully inserted user: {user.Name}")
                else:
                    print(f"User with email {user.email} already exists.")

                token = create_magic_link_token()
                issued_at = datetime.now(timezone.utc)
                token_row = LoginTokenRecord(
                    user_id=user_row.id, token_hash=hash_token(token), issued_at=issued_at,
                    expires_at=issued_at + timedelta(hours=1), used=False, revoked=False,
                    request_ip="127.0.0.1", request_user_agent="Test Script", attempt_count=0,
                    session_status="issued",
                )
                session.add(token_row)
                issued_tokens.append((user.Name, str(user.email), token))

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
