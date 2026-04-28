"""Sync ORM helpers used by the API layer (directly) and by Temporal
activities (via `asgiref.sync.sync_to_async`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from django.db import transaction
from django.db.models import Model

from webhooks.models import (
    EventType,
    InvoiceRecord,
    ShipmentRecord,
    WebhookEvent,
    WebhookStatus,
)

TERMINAL_STATUSES = (WebhookStatus.COMPLETED, WebhookStatus.FAILED)


def insert_webhook_event(
    idempotency_key: str,
    raw_payload: dict,
    *,
    source_vendor: str,
) -> str:
    """Insert and return the event UUID as a string. Strings everywhere
    in the workflow boundary because Temporal serializes activity args
    via JSON; passing a `UUID` would coerce to string anyway."""
    event = WebhookEvent.objects.create(
        idempotency_key=idempotency_key,
        raw_payload=raw_payload,
        source_vendor=source_vendor,
    )
    return str(event.id)


def get_event_by_key(idempotency_key: str) -> dict | None:
    try:
        e = WebhookEvent.objects.get(idempotency_key=idempotency_key)
    except WebhookEvent.DoesNotExist:
        return None
    return _serialize_event(e)


def _serialize_event(e: WebhookEvent) -> dict:
    return {
        "id": str(e.id),
        "idempotency_key": e.idempotency_key,
        "source_vendor": e.source_vendor,
        "status": e.status,
        "event_type": e.event_type,
        "normalized_data": e.normalized_data,
        "created_at": e.created_at.isoformat() if e.created_at else None,
        "processed_at": e.processed_at.isoformat() if e.processed_at else None,
        "error_message": e.error_message,
        "requires_review": e.requires_review,
    }


def mark_processing_status(idempotency_key: str) -> None:
    """RECEIVED → PROCESSING. No-op on terminal states (idempotent
    replay safety)."""
    WebhookEvent.objects.filter(
        idempotency_key=idempotency_key,
    ).exclude(
        status__in=[s.value for s in TERMINAL_STATUSES],
    ).update(status=WebhookStatus.PROCESSING)


def mark_terminal_status(
    idempotency_key: str,
    *,
    status: WebhookStatus,
    error: str | None = None,
) -> None:
    """Move to a terminal state and stamp `processed_at`. Never
    overwrites an existing terminal row."""
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"mark_terminal_status received non-terminal {status}")
    values: dict[str, Any] = {"status": status, "processed_at": datetime.now(UTC)}
    if error is not None:
        values["error_message"] = error
    WebhookEvent.objects.filter(
        idempotency_key=idempotency_key,
    ).exclude(
        status__in=[s.value for s in TERMINAL_STATUSES],
    ).update(**values)


@transaction.atomic
def store_and_complete(
    *,
    idempotency_key: str,
    event_id: str,
    event_type: str,
    normalized_data: dict,
    record: Model | None,
    requires_review: bool,
) -> None:
    """Atomically store the FK record (if any) and flip the event to
    COMPLETED. When `requires_review=True`, no FK record is written —
    `shipments`/`invoices` contain only adjudicated data downstream
    consumers can trust."""
    if record is not None:
        record.save()
    WebhookEvent.objects.filter(
        idempotency_key=idempotency_key,
    ).exclude(
        status__in=[s.value for s in TERMINAL_STATUSES],
    ).update(
        status=WebhookStatus.COMPLETED,
        event_type=event_type,
        normalized_data=normalized_data,
        processed_at=datetime.now(UTC),
        requires_review=requires_review,
    )


def list_review_queue(limit: int = 100) -> list[dict]:
    rows = (
        WebhookEvent.objects
        .filter(requires_review=True)
        .order_by("-created_at")[:limit]
    )
    return [
        {
            "idempotency_key": e.idempotency_key,
            "event_type": e.event_type,
            "normalized_data": e.normalized_data,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in rows
    ]


def _list_stuck_by_status(
    status: WebhookStatus, older_than_seconds: int, limit: int,
) -> list[dict]:
    cutoff = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
    rows = (
        WebhookEvent.objects
        .filter(status=status.value, created_at__lt=cutoff)
        .order_by("created_at")[:limit]
    )
    return [
        {
            "id": str(e.id),
            "idempotency_key": e.idempotency_key,
            "raw_payload": e.raw_payload,
        }
        for e in rows
    ]


def list_stuck_received(older_than_seconds: int, limit: int = 100) -> list[dict]:
    """Rows that succeeded the DB insert but never reached Temporal."""
    return _list_stuck_by_status(WebhookStatus.RECEIVED, older_than_seconds, limit)


def list_stuck_processing(older_than_seconds: int, limit: int = 100) -> list[dict]:
    """Rows whose workflow started but never reached a terminal state.

    `older_than_seconds` MUST exceed `WORKFLOW_RUN_TIMEOUT` plus a
    buffer, or the sweeper races live workflows. Enforced at worker
    startup in `workflows.worker.run_worker`."""
    return _list_stuck_by_status(WebhookStatus.PROCESSING, older_than_seconds, limit)


def build_shipment_record(event_id: str, classification: dict) -> ShipmentRecord | None:
    s = classification.get("shipment")
    if s is None:
        return None
    return ShipmentRecord(
        webhook_event_id=event_id,
        vendor_id=s["vendor_id"],
        tracking_number=s["tracking_number"],
        status=s["status"],
        timestamp=s["timestamp"],
    )


def build_invoice_record(event_id: str, classification: dict) -> InvoiceRecord | None:
    i = classification.get("invoice")
    if i is None:
        return None
    return InvoiceRecord(
        webhook_event_id=event_id,
        vendor_id=i["vendor_id"],
        invoice_id=i["invoice_id"],
        amount=i["amount"],
        currency=i["currency"],
    )


# Adding a new event type: add a Pydantic model in domain.py, an ORM
# model in models.py, and a builder here.
RECORD_BUILDERS = {
    EventType.SHIPMENT.value: build_shipment_record,
    EventType.INVOICE.value: build_invoice_record,
}
