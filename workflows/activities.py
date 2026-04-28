"""Temporal activities — side-effecting steps the workflow orchestrates.

Activities are async; they reach into the sync Django ORM via
`asgiref.sync.sync_to_async`. Each activity is independently retried by
Temporal under a workflow-defined RetryPolicy. Workflows themselves are
deterministic and side-effect-free.
"""

from __future__ import annotations

import asyncio
import json
import logging

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import IntegrityError
from temporalio import activity
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

from webhooks import storage
from webhooks.domain import (
    ClassificationResult,
    EventType,
    VerificationResult,
)
from webhooks.models import WebhookStatus
from workflows.llm_client import LLMSchemaRejectedError, classify, verify

logger = logging.getLogger(__name__)

_MAX_RAW_OUTPUT_LEN = 8192
_MAX_ERROR_MESSAGE_LEN = 2048
_TRUNCATION_SUFFIX = "...[truncated]"

# Event types we trust the LLM's *claim* for when its extraction failed
# schema validation. Adding a new EventType variant deliberately
# requires opting in here, after teaching `RECORD_BUILDERS`, the
# prompts, and the admin `review_reason` what the new variant means.
_CLAIMABLE_EVENT_TYPES: frozenset[str] = frozenset({
    EventType.SHIPMENT.value,
    EventType.INVOICE.value,
})


def _review_reason(
    result: ClassificationResult, verification: VerificationResult | None,
) -> str | None:
    """Returns the review reason, or None when the row is safe to
    promote to FK records. The verifier is the primary protection
    against hallucinated extractions."""
    if (
        verification is not None
        and not verification.grounded
        and result.event_type != EventType.UNCLASSIFIED
    ):
        return "ungrounded"
    return None


def _needs_review(
    result: ClassificationResult, verification: VerificationResult | None = None,
) -> bool:
    return _review_reason(result, verification) is not None


@activity.defn(name="mark_processing")
async def mark_processing(idempotency_key: str) -> None:
    """RECEIVED → PROCESSING. Idempotent (terminal states are skipped)."""
    await sync_to_async(storage.mark_processing_status, thread_sensitive=True)(idempotency_key)


@activity.defn(name="classify_payload")
async def classify_payload(payload: dict) -> dict:
    """Returns a JSON-serializable discriminated-union dict:

        {"kind": "ok",              "result": <ClassificationResult dump>}
        {"kind": "schema_rejected", "raw_llm_output": ..., "validation_message": ...}

    Schema-rejected output is NOT re-raised — it's deterministic for a
    given prompt+payload+model so retries burn LLM cost without changing
    the outcome. The `persist_classification` activity routes it to the
    review queue with the raw output preserved.
    """
    try:
        result = await classify(payload)
    except LLMSchemaRejectedError as e:
        return {
            "kind": "schema_rejected",
            "raw_llm_output": e.raw_output,
            "validation_message": e.validation_message,
        }
    return {"kind": "ok", "result": result.model_dump(mode="json")}


@activity.defn(name="verify_extraction")
async def verify_extraction(payload: dict, classification_dict: dict) -> dict:
    """Second-pass groundedness check. Skipped by the workflow for
    UNCLASSIFIED and schema-rejected paths."""
    inner = classification_dict.get("result", classification_dict)
    result = await verify(payload, inner)
    return result.model_dump(mode="json")


@activity.defn(name="persist_classification")
async def persist_classification(
    idempotency_key: str,
    event_id: str,
    classification_dict: dict,
    verification_dict: dict | None = None,
) -> None:
    """Build the normalized record, store atomically, and route to
    review on schema-rejection or ungrounded extraction.

    Idempotent: a redelivered activity hits the OneToOne constraint on
    shipments/invoices and the IntegrityError is swallowed.
    """
    if classification_dict.get("kind") == "schema_rejected":
        await _persist_schema_rejected(idempotency_key, event_id, classification_dict)
        return

    inner = classification_dict.get("result", classification_dict)
    result = ClassificationResult.model_validate(inner)

    verification = (
        VerificationResult.model_validate(verification_dict)
        if verification_dict is not None else None
    )
    reason = _review_reason(result, verification)

    # Defensive guard: a non-UNCLASSIFIED extraction without a verifier
    # result means the workflow skipped verification when it shouldn't
    # have. Flag rather than silently auto-promote.
    if (
        reason is None
        and verification is None
        and result.event_type != EventType.UNCLASSIFIED
    ):
        reason = "verifier_skipped"

    needs_review = reason is not None
    normalized_data = result.model_dump(mode="json")
    if verification is not None:
        normalized_data["verification"] = verification.model_dump(mode="json")
    if reason is not None:
        normalized_data["review_reason"] = reason

    if needs_review:
        logger.warning(
            "Routing %s to review queue (reason=%s, type=%s)",
            idempotency_key, reason, result.event_type.value,
        )
        record = None
    else:
        builder = storage.RECORD_BUILDERS.get(result.event_type.value)
        record = builder(event_id, inner) if builder else None

    try:
        await sync_to_async(storage.store_and_complete, thread_sensitive=True)(
            idempotency_key=idempotency_key,
            event_id=event_id,
            event_type=result.event_type.value,
            normalized_data=normalized_data,
            record=record,
            requires_review=needs_review,
        )
    except IntegrityError:
        logger.info("Duplicate insert skipped (idempotent activity replay): %s", idempotency_key)

    logger.info(
        "Persisted %s as %s (requires_review=%s, reason=%s)",
        idempotency_key, result.event_type.value, needs_review, reason,
    )


def _bounded_raw_output(raw: dict | list) -> dict:
    """Always returns `{"truncated": bool, "content": ...}`. Small
    payloads pass through; oversized payloads are JSON-serialized,
    truncated, and flagged so consumers know `content` won't json-parse."""
    serialized = json.dumps(raw)
    if len(serialized) <= _MAX_RAW_OUTPUT_LEN:
        return {"truncated": False, "content": raw}
    return {
        "truncated": True,
        "content": serialized[:_MAX_RAW_OUTPUT_LEN] + "...[truncated]",
    }


def _llm_claimed_event_type(raw_output: dict | list) -> str:
    """Best-effort recovery of the LLM's claimed event_type from a
    schema-rejected response. Falls back to UNCLASSIFIED for anything
    not in `_CLAIMABLE_EVENT_TYPES` so a future EventType variant
    doesn't auto-extend trust."""
    if not isinstance(raw_output, dict):
        return EventType.UNCLASSIFIED.value
    claimed = raw_output.get("event_type")
    if isinstance(claimed, str) and claimed in _CLAIMABLE_EVENT_TYPES:
        return claimed
    return EventType.UNCLASSIFIED.value


async def _persist_schema_rejected(
    idempotency_key: str, event_id: str, classification_dict: dict,
) -> None:
    """Persist a schema-rejected row with `requires_review=True` and the
    raw LLM output preserved. `event_type` reflects the LLM's claim
    when present and recognized so the review queue's reason column
    reads e.g. "shipment + schema_rejected" rather than "unclassified"."""
    raw_output = classification_dict.get("raw_llm_output", {})
    validation_message = classification_dict.get("validation_message", "")
    normalized_data = {
        "schema_rejected": True,
        "raw_llm_output": _bounded_raw_output(raw_output),
        "validation_message": validation_message[:_MAX_RAW_OUTPUT_LEN],
    }
    event_type = _llm_claimed_event_type(raw_output)
    try:
        await sync_to_async(storage.store_and_complete, thread_sensitive=True)(
            idempotency_key=idempotency_key,
            event_id=event_id,
            event_type=event_type,
            normalized_data=normalized_data,
            record=None,
            requires_review=True,
        )
    except IntegrityError:
        logger.info("Duplicate insert skipped (idempotent activity replay): %s", idempotency_key)
    logger.warning(
        "Schema-rejected LLM output routed to review (claimed=%s): %s — %s",
        event_type, idempotency_key, validation_message[:200],
    )


@activity.defn(name="mark_failed")
async def mark_failed(idempotency_key: str, error_message: str) -> None:
    """Final terminal state for events that exhausted all retries."""
    if len(error_message) > _MAX_ERROR_MESSAGE_LEN:
        keep = _MAX_ERROR_MESSAGE_LEN - len(_TRUNCATION_SUFFIX)
        error_message = error_message[:keep] + _TRUNCATION_SUFFIX
    await sync_to_async(storage.mark_terminal_status, thread_sensitive=True)(
        idempotency_key,
        status=WebhookStatus.FAILED,
        error=error_message,
    )
    logger.error("Marked FAILED after exhausted retries: %s — %s", idempotency_key, error_message)


@activity.defn(name="list_stuck_received")
async def list_stuck_received(older_than_seconds: int, limit: int) -> list[dict]:
    return await sync_to_async(storage.list_stuck_received, thread_sensitive=True)(
        older_than_seconds=older_than_seconds, limit=limit,
    )


@activity.defn(name="list_stuck_processing")
async def list_stuck_processing(older_than_seconds: int, limit: int) -> list[dict]:
    """PROCESSING rows whose workflow died before reaching a terminal
    state — typically run-timeout firings during a long retry loop on
    the LLM activity, or worker pod deaths between mark_processing and
    mark_failed."""
    return await sync_to_async(storage.list_stuck_processing, thread_sensitive=True)(
        older_than_seconds=older_than_seconds, limit=limit,
    )


_reissue_client: Client | None = None
# `asyncio.Lock` binds to the first event loop that acquires it. Only
# the Temporal worker process invokes `_get_reissue_client`, and the
# worker has exactly one loop, so this binds to the worker loop on
# first sweep.
_reissue_client_lock = asyncio.Lock()


async def _get_reissue_client() -> Client:
    """Module-cached `Client` shared by every reissue call. Connecting
    per call would mean ~100 fresh gRPC handshakes/min for a 100-row
    sweep batch firing every minute."""
    global _reissue_client
    if _reissue_client is not None:
        return _reissue_client
    async with _reissue_client_lock:
        if _reissue_client is not None:
            return _reissue_client
        _reissue_client = await Client.connect(
            settings.TEMPORAL_ADDRESS,
            namespace=settings.TEMPORAL_NAMESPACE,
        )
    return _reissue_client


@activity.defn(name="reissue_classify_workflow")
async def reissue_classify_workflow(idempotency_key: str, event_id: str, payload: dict) -> None:
    """Re-issue start_workflow for a stuck row. ALREADY_EXISTS is benign.

    TOCTOU guard: a row listed as stuck in one activity may have
    transitioned to a terminal state by the time we get here. Skip
    re-issue in that case to avoid burning an LLM call that
    store_and_complete would drop on the terminal-state filter."""
    from workflows.definitions import WORKFLOW_RUN_TIMEOUT, ClassifyWebhookWorkflow

    event = await sync_to_async(storage.get_event_by_key, thread_sensitive=True)(idempotency_key)
    if event is None or event["status"] in (
        WebhookStatus.COMPLETED.value, WebhookStatus.FAILED.value,
    ):
        logger.info(
            "Sweep: %s already terminal (%s) — skipping reissue",
            idempotency_key, event["status"] if event else "missing",
        )
        return

    client = await _get_reissue_client()
    try:
        await client.start_workflow(
            ClassifyWebhookWorkflow.run,
            args=[idempotency_key, event_id, payload],
            id=idempotency_key,
            task_queue=settings.TEMPORAL_TASK_QUEUE,
            run_timeout=WORKFLOW_RUN_TIMEOUT,
        )
    except RPCError as e:
        if e.status == RPCStatusCode.ALREADY_EXISTS:
            logger.info("Sweep: workflow already running for %s — no-op", idempotency_key)
            return
        raise
