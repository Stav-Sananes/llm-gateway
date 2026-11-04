"""Runtime settings, read from GATEWAY_* environment variables."""

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class RouteTarget(BaseModel):
    provider: str
    model: str


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GATEWAY_")

    host: str = "0.0.0.0"  # noqa: S104 - container listens on all interfaces
    port: int = 8080
    # On SIGTERM, requests still running after this are cancelled (held approvals are then
    # abandoned, cleanly). Keep it below the orchestrator's grace period (Kubernetes: 30 s).
    shutdown_timeout_s: int = Field(default=20, gt=0)

    upstream_name: str = "ollama"
    upstream_base_url: str = "http://localhost:11434/v1"
    # Read timeout applies per chunk read: it bounds the gap between tokens,
    # not the total generation time.
    upstream_connect_timeout_s: float = 2.0
    upstream_read_timeout_s: float = 60.0
    upstream_max_connections: int = 100
    # Chat requests one process streams at once; more are refused with 503 (load shedding).
    # Below upstream_max_connections, so the connection pool isn't what runs out first.
    max_concurrent_chats: int = Field(default=64, gt=0)

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
    # Upstream MCP servers behind /mcp, as JSON: {"sql": "http://sql:8000/mcp", ...}.
    # Names become tool prefixes (`sql__query`), so they must not contain "__" or end in "_".
    mcp_servers: dict[str, str] = Field(default_factory=dict)
    # One deadline for each upstream MCP operation (connect, initialize, call).
    mcp_timeout_s: float = Field(default=30.0, gt=0)
    # Tool-call arguments over this are refused before the policy runs (parsing them is slow);
    # results over the second limit are withheld from the client.
    mcp_max_argument_bytes: int = Field(default=64 * 1024, gt=0)
    mcp_max_result_bytes: int = Field(default=1024 * 1024, gt=0)
    # A call a policy holds for approval waits this long for a human. The MCP client's own
    # request timeout must be longer (60 s in the TypeScript SDK), or the client gives up
    # first and the hold is abandoned. Each tenant may have this many waiting at once.
    approval_timeout_s: float = Field(default=50.0, gt=0, le=3600)
    approval_poll_s: float = Field(default=0.5, gt=0)
    approval_max_pending_per_tenant: int = Field(default=10, ge=0)

    # Semantic cache (Qdrant + an embedding model). Off unless enabled; when Qdrant or the
    # embedder is down, requests go on uncached.
    cache_enabled: bool = False
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: SecretStr | None = None
    # Embeddings come from an OpenAI-compatible /embeddings endpoint; unset uses upstream_base_url.
    cache_embedding_base_url: str | None = None
    cache_embedding_model: str = "nomic-embed-text"
    cache_embedding_dims: int = Field(default=768, gt=0)
    cache_similarity_threshold: float = Field(default=0.95, gt=0, le=1)
    cache_ttl_s: float = Field(default=3600.0, gt=0)
    # Embedding the question and searching Qdrant, together, before going uncached.
    cache_timeout_s: float = Field(default=1.0, gt=0)

    # Request bodies over this get a 413 before they're read (chat and /mcp alike).
    max_request_bytes: int = Field(default=4 * 1024 * 1024, gt=0)
    # Usage events wait in a Redis Stream for the usage worker; past this length the oldest
    # are trimmed (the worker has been down too long).
    usage_stream_max_len: int = Field(default=100_000, gt=0)
    usage_worker_metrics_port: int = 9101

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

    @model_validator(mode="after")
    def _approvals_poll_within_their_timeout(self) -> "Settings":
        if self.approval_poll_s >= self.approval_timeout_s:
            raise ValueError("approval_poll_s must be shorter than approval_timeout_s")
        return self

    @field_validator("anthropic_api_key", mode="before")
    @classmethod
    def _blank_key_means_none(cls, value: object) -> object:
        # Compose passes `${ANTHROPIC_API_KEY:-}` as an empty string when it's unset.
        return None if value == "" else value
