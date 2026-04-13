from typing import Literal

from pydantic import Field, PostgresDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    telegram_bot_token: str

    telegram_webhook_url: str = ""

    telegram_webhook_secret_check: bool = True

    google_client_id: str
    google_client_secret: str
    google_redirect_uri: str

    oauth_state_secret: str

    google_webhook_base_url: str = ""

    encryption_key: str

    mistral_api_key: str
    mistral_model: str = "mistral-large-latest"
    mistral_base_url: str = "https://api.mistral.ai/v1"
    llm_max_tokens: int = Field(default=512, gt=0)

    postgres_url: str

    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "http://localhost:3000"

    whisper_model: Literal["base", "small", "medium"] = "base"
    whisper_device: Literal["cpu", "cuda"] = "cpu"
    whisper_compute_type: Literal["int8", "float16", "float32"] = "int8"

    confirmation_timeout_seconds: int = Field(default=300, gt=0)
    agent_iteration_timeout_seconds: int = Field(default=60, gt=0)
    max_tool_calls_per_iteration: int = Field(default=5, gt=0)
    search_window_hours: int = Field(default=24, gt=0)
    cron_interval_minutes: int = Field(default=60, gt=0)
    webhook_renewal_interval_days: int = Field(default=6, gt=0)
    orphan_session_timeout_minutes: int = Field(default=10, gt=0)
    conversation_timeout_minutes: int = Field(default=30, gt=0)

    heartbeat_interval_seconds: int = Field(default=30, gt=0)

    recovery_min_downtime_seconds: int = Field(default=120, gt=0)

    rate_limit_msg_per_minute: int = Field(default=5, gt=0)

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    max_text_length: int = Field(default=4000, gt=0)
    max_audio_size_mb: int = Field(default=20, gt=0)
    audio_retention_days: int = Field(default=14, gt=0)

    llm_proxy_url: str = ""
    llm_proxy_api_key: str = "dev-key-1"
    llm_proxy_enabled: bool = False

    @field_validator("postgres_url")
    @classmethod
    def validate_postgres_url(cls, v: str) -> str:
        if not v.startswith(("postgresql+asyncpg://", "postgresql://")):
            raise ValueError("POSTGRES_URL must start with postgresql+asyncpg:// or postgresql://")
        return v

    @field_validator("encryption_key")
    @classmethod
    def validate_encryption_key(cls, v: str) -> str:
        import base64

        try:
            decoded = base64.urlsafe_b64decode(v + "==")
            if len(decoded) != 32:
                raise ValueError("Fernet key must be 32 bytes after base64 decoding")
        except Exception as e:
            raise ValueError(f"Invalid ENCRYPTION_KEY: {e}") from e
        return v

    @property
    def postgres_url_sync(self) -> str:
        """Синхронный URL для Alembic (без asyncpg драйвера)."""
        return self.postgres_url.replace("postgresql+asyncpg://", "postgresql://")


settings = Settings()
