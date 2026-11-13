from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "dev"
    database_url: str = "postgresql+psycopg://crm:crm@localhost:5432/crm"
    redis_url: str = "redis://localhost:6379/0"
    jwt_secret: str = "dev-only-insecure-secret-change-me-in-production-0123456789"
    jwt_ttl_minutes: int = 60 * 12
    public_base_url: str = "http://localhost:8000"
    admin_panel_url: str = "http://localhost:3000"
    cors_origins: str = "http://localhost:3000"

    # LLM
    llm_provider: str = "mock"  # "anthropic" | "mock"
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-opus-5-5"

    # WhatsApp Cloud API (platform-level app secret; per-tenant tokens live in integrations table)
    whatsapp_app_secret: str = "dev-app-secret"
    whatsapp_verify_token: str = "dev-verify-token"
    whatsapp_api_base: str = "https://graph.facebook.com/v21.0"
    whatsapp_mode: str = "mock"  # "live" | "mock"

    # Telegram
    telegram_mode: str = "mock"  # "live" | "mock"
    telegram_webhook_secret: str = "dev-telegram-secret"

    # Voice
    voice_provider: str = "mock"  # "twilio" | "mock"
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_from_number: str | None = None

    # Outbound webhooks (Zapier etc.)
    webhook_max_attempts: int = 8


@lru_cache
def get_settings() -> Settings:
    return Settings()
