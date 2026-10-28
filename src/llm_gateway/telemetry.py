"""OpenTelemetry wiring. Exporter endpoint and service name come from standard OTEL_* env vars."""

import httpx
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def install_tracer_provider() -> TracerProvider:
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: "llm-gateway"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    return provider


def instrument(app: FastAPI, upstream_client: httpx.AsyncClient) -> None:
    # Without exclude_spans, ASGI emits one "http send" span per streamed chunk.
    FastAPIInstrumentor.instrument_app(
        app, excluded_urls="healthz", exclude_spans=["receive", "send"]
    )
    HTTPXClientInstrumentor.instrument_client(upstream_client)
