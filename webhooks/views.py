"""DRF views for ingest and health.

The hot path is:
    pre-auth IP throttle (middleware) → HMAC verify → per-vendor rate
    limit → body validate → Redis dedup → DB insert (durability
    boundary) → start_workflow → Redis mark seen → 200.

After the DB insert the event is durably persisted; downstream failures
(start_workflow, mark_as_seen) are recoverable by the sweeper.
"""

from __future__ import annotations

import json
import logging
import re

from django.conf import settings
from django.core.cache import cache as django_cache
from django.db import IntegrityError, OperationalError, connection
from rest_framework.exceptions import Throttled
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from temporalio.service import RPCError, RPCStatusCode

from project.temporal_bridge import BridgeOverloadedError, get_temporal_client, submit
from webhooks import dedup, responses, storage
from webhooks.auth import StandardWebhooksAuthentication
from webhooks.permissions import VendorRateLimitPermission
from workflows.definitions import WORKFLOW_RUN_TIMEOUT, ClassifyWebhookWorkflow

logger = logging.getLogger(__name__)

_ID_PATTERN = re.compile(r"\A[A-Za-z0-9._:\-]{1,256}\Z")

_TYPE_INVALID_BODY = "invalid-body"
_TYPE_PAYLOAD_TOO_LARGE = "payload-too-large"
_TYPE_UNAUTHENTICATED = "unauthenticated"
_TYPE_RATE_LIMITED = "rate-limited"
_TYPE_SERVICE_UNAVAILABLE = "service-unavailable"
_TYPE_INTERNAL_ERROR = "internal-error"


def _service_unavailable(detail: str, instance: str) -> Response:
    return responses.problem(
        status=503,
        title="Service temporarily unavailable",
        detail=detail,
        type_slug=_TYPE_SERVICE_UNAVAILABLE,
        instance=instance,
    )


def _bridge_overloaded_response(instance: str) -> Response:
    response = responses.problem(
        status=503,
        title="Service temporarily unavailable",
        detail="Workflow bridge overloaded. Please retry.",
        type_slug=_TYPE_SERVICE_UNAVAILABLE,
        instance=instance,
        extra={"retry_after_seconds": 1},
    )
    response["Retry-After"] = "1"
    return response


class IngestWebhookView(APIView):
    """POST /webhook — accepts arbitrary JSON, dedups, queues for classification."""

    authentication_classes = [StandardWebhooksAuthentication]
    permission_classes = [IsAuthenticated, VendorRateLimitPermission]

    def post(self, request: Request) -> Response:
        # HMAC was verified over the raw bytes in auth, so we go around DRF's
        # parser to fail uniformly on non-object JSON.
        raw = request.body
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return responses.problem(
                status=422,
                title="Invalid JSON",
                detail="Request body is not valid JSON.",
                type_slug=_TYPE_INVALID_BODY,
                instance=request.path,
            )
        if not isinstance(payload, dict):
            return responses.problem(
                status=422,
                title="Invalid JSON",
                detail="Request body must be a JSON object.",
                type_slug=_TYPE_INVALID_BODY,
                instance=request.path,
            )

        webhook_id: str = request.headers["webhook-id"]
        if not _ID_PATTERN.match(webhook_id):
            return responses.problem(
                status=422,
                title="Invalid webhook-id",
                detail="webhook-id must be 1-256 chars, alphanumerics plus '. _ - :' only.",
                type_slug=_TYPE_INVALID_BODY,
                instance=request.path,
            )

        vendor_id = request.user.vendor_id  # type: ignore[union-attr]

        try:
            is_duplicate, idempotency_key = dedup.check_duplicate(
                webhook_id, vendor_id=vendor_id,
            )
        except Exception as e:
            logger.error("Redis duplicate-check failed: %s", e)
            return _service_unavailable(
                "Dedup cache is unreachable. Please retry.", request.path,
            )
        if is_duplicate:
            return responses.already_received(idempotency_key)

        event_id: str | None = None
        try:
            event_id = storage.insert_webhook_event(
                idempotency_key, payload, source_vendor=vendor_id,
            )
        except IntegrityError:
            existing = storage.get_event_by_key(idempotency_key)
            if existing and existing["status"] != "RECEIVED":
                return responses.already_received(idempotency_key)
            if existing:
                event_id = existing["id"]
        except OperationalError as e:
            logger.error("DB insert failed for %s: %s", idempotency_key, e)
            return _service_unavailable(
                "Database is unreachable. Please retry.", request.path,
            )

        if event_id is None:
            return responses.problem(
                status=500,
                title="Internal error",
                detail="Failed to resolve event after duplicate-key collision.",
                type_slug=_TYPE_INTERNAL_ERROR,
                instance=request.path,
            )

        try:
            client = get_temporal_client()
            submit(
                client.start_workflow(
                    ClassifyWebhookWorkflow.run,
                    args=[idempotency_key, event_id, payload],
                    id=idempotency_key,
                    task_queue=settings.TEMPORAL_TASK_QUEUE,
                    run_timeout=WORKFLOW_RUN_TIMEOUT,
                ),
            )
        except BridgeOverloadedError as e:
            logger.warning("Bridge overloaded, shedding request: %s", e)
            return _bridge_overloaded_response(request.path)
        except RPCError as e:
            if e.status == RPCStatusCode.ALREADY_EXISTS:
                return responses.already_received(idempotency_key)
            logger.error("Temporal start_workflow failed for %s: %s", idempotency_key, e)
            return _service_unavailable(
                "Failed to queue event for classification. Please retry.",
                request.path,
            )

        try:
            dedup.mark_as_seen(idempotency_key, webhook_id, vendor_id=vendor_id)
        except Exception:
            logger.warning("Redis mark_as_seen failed for %s — data is safe", idempotency_key)

        logger.info("Webhook accepted: %s", idempotency_key)
        return responses.accepted(idempotency_key)


class HealthView(APIView):
    """GET /health — readiness probe.

    DB is the only hard prerequisite. Redis and Temporal degrade
    gracefully because the dedup cache is best-effort and the sweeper
    re-issues stuck workflows; failing readiness on either would
    unnecessarily bounce a pod that can still durably ingest.

    First call to `get_temporal_client()` lazily bootstraps the bridge
    thread, so this probe — typically the first HTTP traffic a pod
    sees — is what initializes it. The soft-degraded response on a slow
    handshake keeps probes from flapping the pod out of rotation.
    """

    authentication_classes = []

    def get(self, request: Request) -> Response:
        checks = {}

        try:
            with connection.cursor() as cur:
                cur.execute("SELECT 1")
            checks["postgres"] = "ok"
        except Exception:
            checks["postgres"] = "error"

        try:
            django_cache.set("__health__", "1", 1)
            checks["redis"] = "ok"
        except Exception:
            checks["redis"] = "error"

        try:
            get_temporal_client()
            checks["temporal"] = "ok"
        except Exception:
            checks["temporal"] = "error"

        db_ok = checks["postgres"] == "ok"
        all_ok = all(v == "ok" for v in checks.values())

        if not db_ok:
            return Response({"status": "unhealthy", "checks": checks}, status=503)
        if not all_ok:
            return Response({"status": "degraded", "checks": checks})
        return Response({"status": "healthy", "checks": checks})


class LivenessView(APIView):
    """GET /health/live — process-up only, no dependency checks."""

    authentication_classes = []

    def get(self, request: Request) -> Response:
        return Response({"status": "alive"})


def custom_exception_handler(exc, context):
    """Render DRF / Django exceptions as RFC 7807 Problem Details with
    `Cache-Control: no-store` and `Retry-After` where applicable.

    `RequestDataTooBig` is raised by Django before our view body runs;
    Django would otherwise render it as 400, so we surface 413 here.
    """
    from django.core.exceptions import RequestDataTooBig
    from rest_framework.exceptions import AuthenticationFailed, NotAuthenticated
    from rest_framework.views import exception_handler

    request = context.get("request") if context else None
    instance = request.path if request is not None else None

    if isinstance(exc, RequestDataTooBig):
        return responses.problem(
            status=413,
            title="Payload too large",
            detail="Request body exceeds the configured maximum.",
            type_slug=_TYPE_PAYLOAD_TOO_LARGE,
            instance=instance,
        )

    drf_response = exception_handler(exc, context)
    if drf_response is None:
        return drf_response

    detail = drf_response.data.get("detail") if isinstance(drf_response.data, dict) else None
    detail_str = str(detail) if detail else "Request failed."

    if isinstance(exc, Throttled):
        retry_after = int(exc.wait) if exc.wait is not None else None  # type: ignore[attr-defined]
        extra = {"retry_after_seconds": retry_after} if retry_after is not None else None
        response = responses.problem(
            status=drf_response.status_code,
            title="Too many requests",
            detail=detail_str,
            type_slug=_TYPE_RATE_LIMITED,
            instance=instance,
            extra=extra,
        )
        if retry_after is not None:
            response["Retry-After"] = str(retry_after)
        return response

    if isinstance(exc, AuthenticationFailed | NotAuthenticated):
        return responses.problem(
            status=drf_response.status_code,
            title="Unauthenticated",
            detail=detail_str,
            type_slug=_TYPE_UNAUTHENTICATED,
            instance=instance,
        )

    return responses.problem(
        status=drf_response.status_code,
        title=detail_str,
        detail=detail_str,
        type_slug=_TYPE_INTERNAL_ERROR,
        instance=instance,
    )
