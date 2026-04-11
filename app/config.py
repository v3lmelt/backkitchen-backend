import os
import warnings
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings

_INSECURE_DEFAULT_KEY = "dev-secret-key-change-in-production"

# Upload size limits (bytes)
MAX_AUDIO_UPLOAD_SIZE = 200 * 1024 * 1024  # 200 MB
MAX_IMAGE_UPLOAD_SIZE = 10 * 1024 * 1024   # 10 MB


class Settings(BaseSettings):
    APP_NAME: str = "Audio Management API"
    DATABASE_URL: str = "sqlite:///./backkitchen.db"
    UPLOAD_DIR: str = "./uploads"
    CORS_ORIGINS: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]
    SECRET_KEY: str = _INSECURE_DEFAULT_KEY
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 1 week
    SEED_DEMO_DATA: bool = False
    RESEND_API_KEY: str = ""
    RESEND_FROM_EMAIL: str = "noreply@backkitchen.app"
    FRONTEND_URL: str = "http://localhost:5173"
    INITIAL_ADMIN_EMAIL: str = ""

    # Cloudflare R2 (S3-compatible) storage
    R2_ENABLED: bool = False
    R2_ACCOUNT_ID: str = ""
    R2_ACCESS_KEY_ID: str = ""
    R2_SECRET_ACCESS_KEY: str = ""
    R2_BUCKET_NAME: str = ""
    R2_PUBLIC_URL: str = ""  # e.g. https://data.back-kitchen.net
    R2_PRESIGNED_UPLOAD_EXPIRY: int = 3600    # seconds

    # Auto-cleanup: days to keep old source versions after track completion
    OLD_VERSION_RETENTION_DAYS: int = 7

    model_config = {"env_prefix": "AUDIO_MGMT_", "env_file": ".env", "env_file_encoding": "utf-8"}

    @model_validator(mode="after")
    def _warn_insecure_secret(self) -> "Settings":
        if self.SECRET_KEY == _INSECURE_DEFAULT_KEY:
            env = os.environ.get("AUDIO_MGMT_ENV", "development")
            if env == "production":
                raise ValueError(
                    "SECRET_KEY must be set to a secure random value in production. "
                    "Set AUDIO_MGMT_SECRET_KEY environment variable."
                )
            warnings.warn(
                "SECRET_KEY is using the insecure default value. "
                "Set AUDIO_MGMT_SECRET_KEY in production.",
                UserWarning,
                stacklevel=2,
            )
        return self

    def get_upload_path(self) -> Path:
        p = Path(self.UPLOAD_DIR)
        p.mkdir(parents=True, exist_ok=True)
        return p


settings = Settings()
