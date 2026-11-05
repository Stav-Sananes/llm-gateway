# The fake LLM on its own, for load and chaos runs. Build from the repo root:
#   docker build -f harness/Dockerfile -t llm-gateway-fake-llm .
FROM ghcr.io/astral-sh/uv:0.11.7 AS uv

FROM python:3.12-slim
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY harness ./harness
RUN useradd --create-home --uid 10001 fake
USER fake
ENV PATH=/app/.venv/bin:$PATH
EXPOSE 8000
CMD ["python", "-m", "harness.fake_llm"]
