import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import select

from app.postgres import PostgresClient
from app.db_models import UserRecord


def seed_database() -> None:
    db = PostgresClient()
    distributions = [
        ("1st year", 12, 13),
        ("2nd year", 13, 12),
        ("3rd year", 12, 13),
        ("4th year", 13, 12),
    ]
    seeds = []
    male_number = female_number = 1
    for class_name, male_count, female_count in distributions:
        for _ in range(male_count):
            regno = f"MA{male_number}"
            seeds.append({
                "regno": regno, "name": f"Test User {regno}", "email": f"{regno.lower()}@test.com",
                "bio": "This is a test user.", "class_name": class_name,
                "profile_picture_url": "random_string_1", "gender": "male", "interests": [],
                "matchmaking_enabled": True, "notifications_enabled": True,
                "love_notes_receive_enabled": True, "love_notes_send_enabled": False, "role": "user",
            })
            male_number += 1
        for _ in range(female_count):
            regno = f"FE{female_number}"
            seeds.append({
                "regno": regno, "name": f"Test User {regno}", "email": f"{regno.lower()}@test.com",
                "bio": "This is a test user.", "class_name": class_name,
                "profile_picture_url": "random_string_1", "gender": "female", "interests": [],
                "matchmaking_enabled": True, "notifications_enabled": True,
                "love_notes_receive_enabled": True, "love_notes_send_enabled": False, "role": "user",
            })
            female_number += 1

    inserted_count = 0
    with db.sessions.begin() as session:
        existing_regnos = set(session.scalars(select(UserRecord.regno)).all())
        for seed in seeds:
            if seed["regno"] not in existing_regnos:
                session.add(UserRecord(**seed))
                inserted_count += 1

    print(f"Successfully inserted {inserted_count} new test users.")
    db.close()


if __name__ == "__main__":
    seed_database()
