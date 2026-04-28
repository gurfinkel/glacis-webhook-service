"""Temporal worker bootstrap.

The worker process is async-first (workflows + activities are async).
The sync Django web layer is the *other* process; only the ingest path
uses the bridge thread.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import timedelta

from django.conf import settings
from temporalio.client import Client
from temporalio.worker import Worker

from workflows.activities import (
    classify_payload,
    list_stuck_processing,
    list_stuck_received,
    mark_failed,
    mark_processing,
    persist_classification,
    reissue_classify_workflow,
    verify_extraction,
)
from workflows.definitions import (
    WORKFLOW_RUN_TIMEOUT,
    ClassifyWebhookWorkflow,
    SweepStuckEventsWorkflow,
)
from workflows.llm_client import close_http_client

logger = logging.getLogger(__name__)

PROCESSING_GRACE_SAFETY_BUFFER = timedelta(minutes=5)


def _validate_processing_grace(processing_grace_seconds: int) -> None:
    """The sweeper grace MUST exceed `WORKFLOW_RUN_TIMEOUT` plus a
    buffer or the sweeper races live workflows."""
    minimum = WORKFLOW_RUN_TIMEOUT + PROCESSING_GRACE_SAFETY_BUFFER
    if processing_grace_seconds < minimum.total_seconds():
        raise ValueError(
            f"SWEEPER_PROCESSING_GRACE_SECONDS={processing_grace_seconds} "
            f"is less than WORKFLOW_RUN_TIMEOUT ({WORKFLOW_RUN_TIMEOUT}) + "
            f"safety buffer ({PROCESSING_GRACE_SAFETY_BUFFER}) = {minimum}. "
            f"Setting it lower would race live workflows: the sweeper would "
            f"re-issue payloads to runs that have not yet timed out."
        )


async def _connect_client() -> Client:
    """Connect with OTel tracing interceptor wired in so the
    workflow_id (= idempotency_key) becomes the trace correlation key
    across `start_workflow → workflow → activity`."""
    interceptors = []
    if settings.OTEL_EXPORTER_OTLP_ENDPOINT:
        from project.otel import init_otel
        init_otel()
        from temporalio.contrib.opentelemetry import TracingInterceptor
        interceptors.append(TracingInterceptor())

    return await Client.connect(
        settings.TEMPORAL_ADDRESS,
        namespace=settings.TEMPORAL_NAMESPACE,
        interceptors=interceptors,
    )


def _install_shutdown_signal_handlers(shutdown_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, shutdown_event.set)
        except NotImplementedError:
            pass


async def run_worker() -> None:
    _validate_processing_grace(settings.SWEEPER_PROCESSING_GRACE_SECONDS)

    client = await _connect_client()

    from workflows.schedules import ensure_sweep_schedule
    await ensure_sweep_schedule(
        client,
        task_queue=settings.TEMPORAL_TASK_QUEUE,
        interval_seconds=settings.SWEEPER_INTERVAL_SECONDS,
        received_grace_seconds=settings.SWEEPER_GRACE_SECONDS,
        processing_grace_seconds=settings.SWEEPER_PROCESSING_GRACE_SECONDS,
        limit=settings.SWEEPER_BATCH_LIMIT,
    )

    worker = Worker(
        client,
        task_queue=settings.TEMPORAL_TASK_QUEUE,
        workflows=[ClassifyWebhookWorkflow, SweepStuckEventsWorkflow],
        activities=[
            mark_processing,
            classify_payload,
            verify_extraction,
            persist_classification,
            mark_failed,
            list_stuck_received,
            list_stuck_processing,
            reissue_classify_workflow,
        ],
    )

    logger.info(
        "Temporal worker started — task_queue=%s, namespace=%s, max_retries=%d",
        settings.TEMPORAL_TASK_QUEUE,
        settings.TEMPORAL_NAMESPACE,
        settings.MAX_RETRIES,
    )

    shutdown_event = asyncio.Event()
    _install_shutdown_signal_handlers(shutdown_event)
    worker_task = asyncio.create_task(worker.run())

    try:
        done, _ = await asyncio.wait(
            {worker_task, asyncio.create_task(shutdown_event.wait())},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if worker_task not in done:
            logger.info("Shutdown signal received — draining worker")
            await worker.shutdown()
            await worker_task
    finally:
        try:
            await close_http_client()
        except Exception:
            logger.warning("close_http_client failed during shutdown", exc_info=True)
