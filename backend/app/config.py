import logging
import os
from pydantic_settings import BaseSettings
import secrets

class Settings(BaseSettings):
    MONGO_USERNAME: str = "ConfessIt"
    MONGO_PASSWORD: str
    DATABASE_NAME: str = "ConfessDB"
    MONGO_HOST: str = "mongo"
    MONGO_PORT: int = 27017
    MONGODB_URI: str = ""

    CLOUDINARY_CLOUD_NAME: str | None = None
    CLOUDINARY_API_KEY: str | None = None
    CLOUDINARY_API_SECRET: str | None = None
    CLOUDINARY_URL: str | None = None
    CLOUDINARY_PROFILE_FOLDER: str | None = None
    CLOUDINARY_EVENTS_FOLDER: str | None = None
    CLOUDINARY_GALLERY_FOLDER: str | None = None
    MAX_IMAGE_SIZE_MB: int | None = None
    PUBLIC_BASE_URL: str | None = None
    CLOUDINARY_USE_ADVANCED_TRANSFORMS: str | None = None
    CLOUDINARY_DEFAULT_VIEW_TTL: int | None = None
    CLOUDINARY_ALLOWED_FORMATS: str | None = None

    SECRET_KEY: str = secrets.token_hex(32)  # Generate a random secret key
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30

    # Redis settings
    REDIS_URL: str = "redis://redis:6379"

    CLOUD: bool = False

    @property
    def MONGO_URI(self):
        if self.CLOUD:
            return self.MONGODB_URI
        else:
            # Add authSource=admin so root user authenticates correctly
            return (
                f"mongodb://{self.MONGO_USERNAME}:{self.MONGO_PASSWORD}"
                f"@{self.MONGO_HOST}:{self.MONGO_PORT}/{self.DATABASE_NAME}"
                f"?authSource=admin"
            )

    class Config:
        env_file = ".env"
        secrets_dir = "/run/secrets"
        extra = "allow"

# Read Docker secret manually if present, only if not cloud
CLOUD = os.getenv("CLOUD", "false").lower() == "true"
if not CLOUD and os.path.exists("/run/secrets/db_password"):
    os.environ["MONGO_PASSWORD"] = open("/run/secrets/db_password").read().strip()

settings = Settings()
