import warnings
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings

_INSECURE_DEFAULT_KEY = "dev-secret-key-change-in-production"


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
    SEED_DEMO_DATA: bool = True

    model_config = {"env_prefix": "AUDIO_MGMT_"}

    @model_validator(mode="after")
    def _warn_insecure_secret(self) -> "Settings":
        if self.SECRET_KEY == _INSECURE_DEFAULT_KEY:
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
