import os
import sys

# Add the current directory to sys.path so we can import 'app' module
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.config import settings
from app.postgres import PostgresClient
from app.models import UserDetails

def seed_database():
    print("Connecting to database...")
    db = PostgresClient()[settings.DATABASE_NAME]
    users = db["UserDetails"]

    class_distributions = [
        ("1st year", 12, 13),
        ("2nd year", 13, 12),
        ("3rd year", 12, 13),
        ("4th year", 13, 12),
    ]
    
    seeds = []
    m_count = 1
    f_count = 1
    for cls_name, m_limit, f_limit in class_distributions:
        for _ in range(m_limit):
            regno = f"MA{m_count}"
            seeds.append(UserDetails(
                Regno=regno, Name=f"Test User {regno}", email=f"{regno.lower()}@test.com", 
                bio="This is a test user.", which_class=cls_name, profile_picture_id="random_string_1", 
                gender="male", interests=[], isMatchmaking=True, isNotifications=True, 
                isLovenotesRecieve=True, isLovenotesSend=False, user_role="user"
            ))
            m_count += 1
        for _ in range(f_limit):
            regno = f"FE{f_count}"
            seeds.append(UserDetails(
                Regno=regno, Name=f"Test User {regno}", email=f"{regno.lower()}@test.com", 
                bio="This is a test user.", which_class=cls_name, profile_picture_id="random_string_1", 
                gender="female", interests=[], isMatchmaking=True, isNotifications=True, 
                isLovenotesRecieve=True, isLovenotesSend=False, user_role="user"
            ))
            f_count += 1

    print(f"Prepared {len(seeds)} users for insertion. Checking if they exist...")
    inserted_count = 0
    for user in seeds:
        if not users.find_one({"Regno": user.Regno}):
            users.insert_one(user.model_dump(by_alias=True))
            inserted_count += 1

    print(f"Successfully inserted {inserted_count} new test users.")

if __name__ == "__main__":
    seed_database()
