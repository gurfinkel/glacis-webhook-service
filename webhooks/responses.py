"""Response builders for the ingest API.

Conventions (centralized so every code path emits the same shape):
- Success body: `{status, idempotency_key}`.
- Error body: RFC 7807 Problem Details (`application/problem+json`).
- `Idempotent-Replayed: true` on every replay response.
- `Cache-Control: no-store` on every response.
"""

from __future__ import annotations

from typing import Any

from rest_framework.response import Response

from webhooks.serializers import WebhookResponseSerializer

PROBLEM_TYPE_BASE = "/errors"


def _no_store(response: Response) -> Response:
    response["Cache-Control"] = "no-store"
    return response


def accepted(idempotency_key: str) -> Response:
    return _no_store(Response(
        WebhookResponseSerializer({
            "status": "accepted",
            "idempotency_key": idempotency_key,
        }).data,
    ))


def already_received(idempotency_key: str) -> Response:
    response = Response(
        WebhookResponseSerializer({
            "status": "already_received",
            "idempotency_key": idempotency_key,
        }).data,
    )
    response["Idempotent-Replayed"] = "true"
    return _no_store(response)


def problem(
    *,
    status: int,
    title: str,
    detail: str,
    type_slug: str,
    instance: str | None = None,
    extra: dict[str, Any] | None = None,
) -> Response:
    """RFC 7807 Problem Details response. `extra` adds domain-specific
    fields (e.g. `retry_after_seconds`) — RFC 7807 explicitly allows this."""
    body: dict[str, Any] = {
        "type": f"{PROBLEM_TYPE_BASE}/{type_slug}",
        "title": title,
        "status": status,
        "detail": detail,
    }
    if instance is not None:
        body["instance"] = instance
    if extra:
        body.update(extra)
    response = Response(body, status=status, content_type="application/problem+json")
    return _no_store(response)
