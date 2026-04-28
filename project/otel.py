"""OpenTelemetry SDK setup — distributed tracing.

`init_otel()` builds the tracer SDK based on
`settings.OTEL_EXPORTER_OTLP_ENDPOINT`. When the endpoint is empty
(default in dev), OTel is a no-op — no exporter, no extra threads, no
overhead. When set, spans ship to any OTLP-compatible backend (Jaeger,
Tempo, Honeycomb).

## What gets instrumented

- **Django** views (request span, status, route).
- **psycopg** queries (DB span per statement).
- **redis-py** commands (Redis span per command).
- **httpx** outbound requests (LLM call span).

The Temporal worker layers
`temporalio.contrib.opentelemetry.TracingInterceptor` on top, so the
trace started in the Django ingest view propagates through
`start_workflow`, into the workflow run, and into each activity. The
correlation key is the `workflow_id` (= idempotency_key).

Metrics + structured logs SDKs are intentionally not wired — the
production roadmap adds them once the trace baseline is calibrated.
"""

from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger(__name__)

_initialized = False


def init_otel() -> None:
    """Idempotent. Called once from `webhooks/apps.py` `ready()` (web side)
    and from worker bootstrap (worker side)."""
    global _initialized
    if _initialized:
        return
    _initialized = True

    endpoint = settings.OTEL_EXPORTER_OTLP_ENDPOINT
    if not endpoint:
        logger.info("OTEL_EXPORTER_OTLP_ENDPOINT empty — OpenTelemetry disabled")
        return

    # Imports gated so OTel deps aren't required when telemetry is off.
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.django import DjangoInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({SERVICE_NAME: settings.OTEL_SERVICE_NAME})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)

    DjangoInstrumentor().instrument()
    PsycopgInstrumentor().instrument()
    RedisInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()

    logger.info("OpenTelemetry initialized — exporting to %s", endpoint)
