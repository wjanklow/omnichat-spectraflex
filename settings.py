"""
settings.py
~~~~~~~~~~~
Runtime configuration for Omnichat.

• Loads values from environment variables (with .env fallback on dev)
• Validates types at startup
• Exposes a singleton `settings` you can import anywhere
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import (
    Field,
    PositiveInt,
    RedisDsn,
    SecretStr,
    ValidationError,
    PostgresDsn,
    AnyHttpUrl,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

# Auto-load .env when running locally
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)


class _Settings(BaseSettings):
    # ── GENERAL ──────────────────────────────────────────────
    env: Literal["dev", "staging", "prod"] = Field("dev", validation_alias="ENV")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    app_host: str = "0.0.0.0"
    app_port: PositiveInt = 8000

    # ── OPENAI ───────────────────────────────────────────────
    openai_api_key: SecretStr = Field(..., alias="OPENAI_API_KEY")
    openai_model_chat: str = Field("gpt-4o-mini", alias="OPENAI_MODEL_CHAT")
    openai_model_embed: str = Field("text-embedding-3-small", alias="OPENAI_MODEL_EMBED")

    # ── PINECONE ─────────────────────────────────────────────
    pinecone_api_key: SecretStr = Field(..., alias="PINECONE_API_KEY")
    pinecone_region  : str       = Field("us-east-1", alias="PINECONE_REGION")
    pinecone_env: str = Field("us-east-1-aws", alias="PINECONE_ENV")
    pinecone_index: str = Field("spectraflex-prod", alias="PINECONE_INDEX")

    # ── SHOPIFY ──────────────────────────────────────────────
    shop_url: AnyHttpUrl = Field(..., alias="SHOP_URL")
    shop_admin_token: SecretStr = Field(..., alias="SHOP_ADMIN_TOKEN")
    storefront_token: SecretStr | None = Field(None, alias="STOREFRONT_TOKEN")

    # ── REDIS ────────────────────────────────────────────────
    redis_url: RedisDsn = Field("redis://localhost:6379/0", alias="REDIS_URL")

    # ── GUARD-RAILS / RAG TUNING ──────────────────────────────────────────────
    off_topic_threshold: float = Field(0.60, alias="OFF_TOPIC_THRESHOLD")


    # ── OPTIONALS ────────────────────────────────────────────
    sentry_dsn: str | None = Field(None, alias="SENTRY_DSN")
    db_dsn: PostgresDsn | None = Field(None, alias="DATABASE_URL")

    # Pydantic-settings config
    model_config = SettingsConfigDict(
        case_sensitive=False,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def _get_settings() -> _Settings:
    try:
        return _Settings()  # validates on first call
    except ValidationError as e:
        print("❌ Invalid configuration:")
        for err in e.errors():
            print(f"   • {err['loc'][0]} – {err['msg']}")
        raise SystemExit(1)


# Singleton instance you import elsewhere
settings: _Settings = _get_settings()
