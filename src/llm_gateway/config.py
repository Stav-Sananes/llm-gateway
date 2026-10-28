"""Runtime settings, read from GATEWAY_* environment variables."""

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

    tracing_enabled: bool = False
