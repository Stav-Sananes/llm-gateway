"""Runtime settings, read from GATEWAY_* environment variables."""

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class RouteTarget(BaseModel):
    provider: str
    model: str


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GATEWAY_")

    host: str = "0.0.0.0"  # noqa: S104 - container listens on all interfaces
    port: int = 8080

    upstream_name: str = "ollama"
    upstream_base_url: str = "http://localhost:11434/v1"
    # Read timeout applies per chunk read: it bounds the gap between tokens,
    # not the total generation time.
    upstream_connect_timeout_s: float = 2.0
    upstream_read_timeout_s: float = 60.0
    upstream_max_connections: int = 100

    # Anthropic is enabled when a key is set. The base URL is only overridden in tests.
    anthropic_api_key: SecretStr | None = None
    anthropic_base_url: str | None = None
    # Let Anthropic re-run a refused request on a fallback model (supported models only).
    anthropic_refusal_fallbacks: bool = True

    # Model name → ordered provider targets, as JSON, e.g.
    # {"smart": [{"provider": "anthropic", "model": "claude-opus-5-5"},
    #            {"provider": "ollama", "model": "qwen2.5:0.5b"}]}
    # Any other model name goes to `upstream_name` unchanged.
    routes: dict[str, list[RouteTarget]] = Field(default_factory=dict)
    # A provider's circuit opens after this many failures in a row, and is retried after reset.
    breaker_failure_threshold: int = Field(default=5, gt=0)
    breaker_reset_s: float = Field(default=30.0, gt=0)

    # Cedar policies for MCP tool calls; unset uses the packaged demo policies.
    policy_file: str | None = None

    database_url: str = "postgresql+asyncpg://gateway:gateway@localhost:5433/gateway"
    database_connect_timeout_s: float = 2.0
    database_statement_timeout_s: float = 5.0
    database_pool_size: int = 10

    redis_url: str = "redis://localhost:6379/0"
    # Short: when Redis hangs, auth falls back to Postgres instead of waiting.
    redis_timeout_s: float = 0.25
    # Upper bound on how long a revoked key can keep working if its cache entry wasn't cleared.
    key_cache_ttl_s: int = 60

    # Per tenant. The bucket holds `rate_limit_burst` requests and refills at the given rate.
    rate_limit_burst: int = Field(default=20, gt=0)
    rate_limit_per_s: float = Field(default=5.0, gt=0)
    # Tokens (prompt + completion) per tenant per UTC day; None turns budgets off.
    token_budget_per_day: int | None = Field(default=1_000_000, gt=0)
    # Sent upstream as max_tokens when a request sets none, and reserved for the completion.
    default_max_tokens: int = Field(default=4096, gt=0)

    tracing_enabled: bool = False

    @field_validator("anthropic_api_key", mode="before")
    @classmethod
    def _blank_key_means_none(cls, value: object) -> object:
        # Compose passes `${ANTHROPIC_API_KEY:-}` as an empty string when it's unset.
        return None if value == "" else value
