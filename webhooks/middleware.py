"""Pre-auth IP rate limit middleware (layer 1).

Runs before DRF auth so a flood of forged signatures from a single IP
gets 429'd without paying the HMAC verify + DB lookup cost. The
per-vendor rate limit (layer 2) lives in `webhooks/permissions.py` and
runs after authentication.

Not a substitute for an LB / WAF — those are the real edge DDoS layer.
This middleware closes the per-pod gap when the LB is mid-deploy or
absent (dev / single-host).
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django_ratelimit.core import is_ratelimited

# Imported from a constants-only module rather than `webhooks.urls` so
# Django doesn't pull in the view stack (DRF + Temporal SDK + httpx +
# Pydantic) at settings-init time when this middleware is registered.
from webhooks.paths import INGEST_PATH

logger = logging.getLogger(__name__)


def _is_ingest_path(path: str) -> bool:
    # Exact match (with optional trailing slash) — `startswith("/webhook")`
    # would also throttle future routes like `/webhook-info`.
    return path == INGEST_PATH or path == INGEST_PATH + "/"


def _build_rate_limit_response(request: HttpRequest, rate: str) -> JsonResponse:
    count, period = rate.split("/")
    retry_after_seconds = int(_period_to_seconds(period))
    response = JsonResponse(
        {
            "type": "/errors/rate-limited",
            "title": "Too many requests",
            "status": 429,
            "detail": (
                f"More than {count} unauthenticated requests "
                f"per {period}; retry later."
            ),
            "instance": request.path,
            "retry_after_seconds": retry_after_seconds,
        },
        status=429,
        content_type="application/problem+json",
    )
    response["Retry-After"] = str(retry_after_seconds)
    response["Cache-Control"] = "no-store"
    return response


class PreAuthIPRateLimitMiddleware:
    """Buckets by client IP (`REMOTE_ADDR`, the direct TCP peer).

    We don't honor `X-Forwarded-For` because blindly trusting it is its
    own DoS — an attacker varies the header to bypass per-IP limits.
    Production deployments rely on the LB / WAF for the X-Forwarded-For
    handling.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if not _is_ingest_path(request.path):
            return self.get_response(request)

        limited = is_ratelimited(
            request,
            group="webhook-ingest-preauth-ip",
            key="ip",
            rate=settings.PRE_AUTH_IP_RATE_LIMIT,
            method=request.method,
            increment=True,
        )
        if limited:
            logger.warning(
                "Pre-auth IP rate-limit tripped: ip=%s path=%s rate=%s",
                request.META.get("REMOTE_ADDR", "?"),
                request.path,
                settings.PRE_AUTH_IP_RATE_LIMIT,
            )
            return _build_rate_limit_response(request, settings.PRE_AUTH_IP_RATE_LIMIT)

        return self.get_response(request)


_PERIOD_MULTIPLIERS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _period_to_seconds(period: str) -> str:
    """`10s` → `10`, `1m` → `60`, `1h` → `3600`. Strict — only positive
    integer counts with a single `s/m/h/d` suffix. Anything else raises
    so the operator notices at boot instead of silently using the raw
    string as a Retry-After header."""
    if len(period) < 2:
        raise ValueError(f"rate-limit period {period!r} must be N<unit>, e.g. '10s'")
    suffix = period[-1].lower()
    multiplier = _PERIOD_MULTIPLIERS.get(suffix)
    if multiplier is None:
        raise ValueError(
            f"rate-limit period {period!r} has unknown unit {period[-1]!r}; "
            f"use one of {sorted(_PERIOD_MULTIPLIERS)}"
        )
    try:
        n = int(period[:-1])
    except ValueError as e:
        raise ValueError(
            f"rate-limit period {period!r} count must be a positive integer"
        ) from e
    if n <= 0:
        raise ValueError(f"rate-limit period {period!r} count must be > 0")
    return str(n * multiplier)
