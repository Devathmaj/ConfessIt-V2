import logging
import pymongo
from .config import settings
from .models import UserDetails, LoginToken
import secrets
from datetime import datetime, timedelta
import hashlib

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
        # Establish a connection to the MongoDB server
        client = pymongo.MongoClient(settings.MONGO_URI)
        db = client[settings.DATABASE_NAME]
        user_details_collection = db["UserDetails"]
        login_tokens_collection = db["LoginTokens"]

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

        # Insert the user and admin into the database
        for user in users:
            # Check if a user with the same email already exists
            if not user_details_collection.find_one({"email": user.email}):
                user_details_collection.insert_one(user.dict(by_alias=True))
                print(f"Successfully inserted user: {user.Name}")
            else:
                print(f"User with email {user.email} already exists.")

        # Generate, store, and print magic link tokens for the users
        for user_data in users:
            user_in_db = user_details_collection.find_one({"email": user_data.email})
            if user_in_db:
                user_id = str(user_in_db["_id"])
                
                # Generate a new token
                token = create_magic_link_token()
                
                # Create and store the LoginToken document
                token_hash = hash_token(token)
                issued_at = datetime.utcnow()
                expires_at = issued_at + timedelta(hours=1)
                
                login_token_entry = LoginToken(
                    user_id=user_id,
                    token_hash=token_hash,
                    issued_at=issued_at,
                    expires_at=expires_at,
                    consumed_at=None,
                    used=False,
                    revoked=False,
                    request_ip="127.0.0.1", # Dummy IP
                    request_user_agent="Test Script", # Dummy User Agent
                    consume_ip=None,
                    consume_user_agent=None,
                    attempt_count=0,
                    metadata=None
                )
                
                login_tokens_collection.insert_one(login_token_entry.dict(by_alias=True))
                print(f"Successfully stored token for {user_data.Name}.")

                # Print the unhashed token for testing purposes
                print(f"\nMagic link token for {user_data.Name} ({user_data.email}):")
                print(token)


    except pymongo.errors.ConnectionFailure as e:
        print(f"Could not connect to MongoDB: {e}")
    except Exception as e:
        print(f"An error occurred: {e}")
    finally:
        # Ensure the client connection is closed
        if 'client' in locals() and client:
            client.close()

if __name__ == "__main__":
    main()
