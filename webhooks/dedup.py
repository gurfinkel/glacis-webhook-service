"""Vendor-namespaced Redis dedup, sync edition.

Three layers of dedup defend against vendor retries:
- this Redis fast-path
- the DB unique constraint on `WebhookEvent.idempotency_key` (authoritative)
- the Temporal `workflow_id` (third defense — duplicate `start_workflow`
  raises `WorkflowAlreadyStarted`)

Vendors are required to send a per-event `webhook-id` header (Standard
Webhooks contract). The auth layer rejects requests without it, so the
dedup key is always `{vendor_id}:{webhook-id}` — no content-hash fallback
and no unauthenticated path.

Rationale for not also dedup'ing on payload hash: two events with
byte-identical bodies but different `webhook-id`s are treated as
distinct events. The vendor's declaration of identity wins. A vendor
SDK that regenerates `webhook-id` on retry would burn one duplicate
LLM call before the OneToOne FK constraint catches it — observable in
metrics, fixable operationally by talking to the vendor, not worth
defensive coding here.
"""

import threading

from django.conf import settings
from redis import Redis

DEDUP_TTL_SECONDS = 604_800  # 7 days

_redis: Redis | None = None
_redis_lock = threading.Lock()


def get_redis() -> Redis:
    """Process-singleton Redis client. Double-checked locking so two
    threads on the first call don't each construct and leak a client."""
    global _redis
    if _redis is not None:
        return _redis
    with _redis_lock:
        if _redis is not None:
            return _redis
        _redis = Redis.from_url(settings.REDIS_URL)
        return _redis


def compute_idempotency_key(webhook_id: str, *, vendor_id: str) -> str:
    return f"{vendor_id}:{webhook_id}"


def _redis_key(vendor_id: str, webhook_id: str) -> str:
    return f"webhook:external_id:{vendor_id}:{webhook_id}"


def check_duplicate(webhook_id: str, *, vendor_id: str) -> tuple[bool, str]:
    """Return `(is_duplicate, idempotency_key)`. Idempotency key is the
    same on hit or miss — a deterministic function of `(vendor_id,
    webhook_id)`."""
    r = get_redis()
    key = compute_idempotency_key(webhook_id, vendor_id=vendor_id)
    cached = r.get(_redis_key(vendor_id, webhook_id))
    if cached is not None:
        return True, cached.decode()  # type: ignore[union-attr]
    return False, key


def mark_as_seen(idempotency_key: str, webhook_id: str, *, vendor_id: str) -> None:
    """Called AFTER DB insert + Temporal start_workflow succeed."""
    r = get_redis()
    r.set(
        _redis_key(vendor_id, webhook_id),
        idempotency_key,
        ex=DEDUP_TTL_SECONDS,
    )


def close_redis() -> None:
    global _redis
    if _redis is not None:
        _redis.close()
        _redis = None
