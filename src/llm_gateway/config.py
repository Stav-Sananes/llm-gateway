"""Runtime settings, read from GATEWAY_* environment variables."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
