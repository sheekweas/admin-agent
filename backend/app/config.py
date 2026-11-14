from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_JWT_SECRET = "dev-only-insecure-secret-change-me-in-production-0123456789"
PRODUCTION_ENVS = {"production", "prod"}
# Values shipped in code / .env.example: never acceptable in production.
_PLACEHOLDERS = {
    DEFAULT_JWT_SECRET,
    "change-me-to-a-long-random-string",
    "dev-app-secret",
    "dev-verify-token",
    "dev-telegram-secret",
    "choose-a-verify-token",
    "choose-a-secret",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "dev"
    database_url: str = "postgresql+psycopg://crm:crm@localhost:5432/crm"
    redis_url: str = "redis://localhost:6379/0"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    jwt_secret: str = DEFAULT_JWT_SECRET
    # Key for integration secrets at rest (defaults to one derived from jwt_secret). Old keys stay readable
    # during rotation: SECRETS_ENCRYPTION_OLD_KEYS=k1,k2 then `python -m app.rotate_secrets`.
    secrets_encryption_key: str | None = None
    secrets_encryption_old_keys: str = ""
    jwt_ttl_minutes: int = 60 * 12
    public_base_url: str = "http://localhost:8000"
    admin_panel_url: str = "http://localhost:3000"
    cors_origins: str = "http://localhost:3000"

    # LLM
    llm_provider: str = "mock"  # "anthropic" | "mock"
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-opus-5-5"
    # Platform-wide emergency AI off-switch (ТЗ §23.1): every AI turn (WhatsApp/Telegram text, voice) is
    # skipped -> the tenant's fallback message + an admin task, conversation handed to humans. Restart needed.
    ai_globally_disabled: bool = False

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
    # WhatsApp Business Calling API (ТЗ §13.1, §27): not implemented — needs Meta approval per account/country.
    # When true, requests for the WhatsApp calling channel reach WhatsAppCallingProvider, which still raises
    # VoiceNotConfigured with an explanation (the flag only exists to wire a future BSP integration).
    whatsapp_calling_enabled: bool = False

    # Outbound webhooks (Zapier etc.)
    webhook_max_attempts: int = 8

    # HTTP hardening (app.middleware)
    max_webhook_body_bytes: int = 1_000_000
    max_request_body_bytes: int = 10_000_000
    rate_limit_enabled: bool = True
    rate_limit_backend: str = "auto"  # "auto" (Redis if reachable, else memory) | "redis" | "memory"
    rate_limit_login_per_minute: int = 20
    rate_limit_webhook_per_minute: int = 600

    # Prometheus /metrics (app.metrics). With METRICS_TOKEN set a matching "Authorization: Bearer" is required;
    # without it only loopback/private-network clients are served.
    metrics_enabled: bool = True
    metrics_token: str | None = None

    @property
    def is_production(self) -> bool:
        return self.env.lower() in PRODUCTION_ENVS


def production_problems(s: Settings) -> list[str]:
    """Configuration that must never reach production (checked at API/worker start-up)."""
    if not s.is_production:
        return []
    problems = []
    if s.jwt_secret in _PLACEHOLDERS or len(s.jwt_secret) < 32:
        problems.append("JWT_SECRET is the default/placeholder or shorter than 32 characters")
    if s.secrets_encryption_key and len(s.secrets_encryption_key) < 32:
        problems.append("SECRETS_ENCRYPTION_KEY is shorter than 32 characters")
    if s.telegram_webhook_secret in _PLACEHOLDERS or len(s.telegram_webhook_secret) < 16:
        problems.append("TELEGRAM_WEBHOOK_SECRET is the default/placeholder or too short")
    if s.metrics_token is not None and 0 < len(s.metrics_token) < 24:
        problems.append("METRICS_TOKEN is shorter than 24 characters")
    if s.whatsapp_mode == "live":
        if s.whatsapp_app_secret in _PLACEHOLDERS:
            problems.append("WHATSAPP_APP_SECRET is the default/placeholder")
        if s.whatsapp_verify_token in _PLACEHOLDERS or len(s.whatsapp_verify_token) < 12:
            problems.append("WHATSAPP_VERIFY_TOKEN is the default/placeholder or too short")
    return problems


def validate_settings(s: Settings) -> None:
    problems = production_problems(s)
    if problems:
        raise RuntimeError("Refusing to start with insecure production settings: " + "; ".join(problems))


@lru_cache
def get_settings() -> Settings:
    return Settings()
