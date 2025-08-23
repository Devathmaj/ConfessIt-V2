import logging
import os
from pydantic_settings import BaseSettings
import secrets

class Settings(BaseSettings):
    MONGO_USERNAME: str = "ConfessIt"
    MONGO_PASSWORD: str
    MONGO_DB_NAME: str = "ConfessDB"
    MONGO_HOST: str = "mongo"
    MONGO_PORT: int = 27017

    SECRET_KEY: str = secrets.token_hex(32)  # Generate a random secret key
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30

    @property
    def MONGO_URI(self):
        # Add authSource=admin so root user authenticates correctly
        return (
            f"mongodb://{self.MONGO_USERNAME}:{self.MONGO_PASSWORD}"
            f"@{self.MONGO_HOST}:{self.MONGO_PORT}/{self.MONGO_DB_NAME}"
            f"?authSource=admin"
        )

    class Config:
        env_file = ".env"
        secrets_dir = "/run/secrets"

# Read Docker secret manually if present
secret_path = "/run/secrets/db_password"
if os.path.exists(secret_path):
    os.environ["MONGO_PASSWORD"] = open(secret_path).read().strip()

settings = Settings()
