"""DRF permission classes — per-vendor rate limit (layer 2)."""

from __future__ import annotations

import logging

from django.conf import settings
from django_ratelimit.core import is_ratelimited
from rest_framework import permissions
from rest_framework.exceptions import Throttled

logger = logging.getLogger(__name__)


class VendorRateLimitPermission(permissions.BasePermission):
    """Per-vendor rate limit, keyed by `request.user.vendor_id` from
    `StandardWebhooksAuthentication`. Runs after auth via DRF's
    permission hook. Throttled responses include `Retry-After`."""

    rate_group = "webhook-ingest"

    def has_permission(self, request, view) -> bool:
        vendor_id = getattr(getattr(request, "user", None), "vendor_id", None)
        if not vendor_id:
            return True

        rate = settings.DEFAULT_RATE_LIMIT
        limited = is_ratelimited(
            request,
            group=self.rate_group,
            key=lambda group, request: vendor_id,
            rate=rate,
            method=request.method,
            increment=True,
        )
        if limited:
            count, period = rate.split("/")
            logger.warning(
                "Rate-limited vendor_id=%s rate=%s method=%s",
                vendor_id, rate, request.method,
            )
            raise Throttled(detail=f"vendor exceeded {count} requests per {period}")
        return True
