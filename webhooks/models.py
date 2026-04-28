"""ORM-backed records of the webhook ingestion pipeline.

Status transitions (RECEIVED → PROCESSING → COMPLETED | FAILED) are
driven by the Temporal workflow; retry / backoff / DLQ semantics live
in Temporal, not in this table.

`EventType` is re-exported from `webhooks.domain` so the Pydantic LLM
contract and the Django field share a single enum identity.
"""

import uuid

from django.db import models
from django.db.models.functions import Now

from webhooks.domain import EventType

__all__ = [
    "EventType",
    "InvoiceRecord",
    "ShipmentRecord",
    "VendorCredential",
    "WebhookEvent",
    "WebhookStatus",
]


class WebhookStatus(models.TextChoices):
    RECEIVED = "RECEIVED", "received"
    PROCESSING = "PROCESSING", "processing"
    COMPLETED = "COMPLETED", "completed"
    FAILED = "FAILED", "failed"


EVENT_TYPE_CHOICES: list[tuple[str, str]] = [(e.value, e.value) for e in EventType]


class VendorCredential(models.Model):
    """Per-vendor HMAC secret used by `StandardWebhooksAuthentication`.

    Production note: `hmac_secret` should live in a secrets manager;
    storing it plaintext in this table is a submission-scope shortcut.
    """

    vendor_id = models.CharField(max_length=256, unique=True, db_index=True)
    hmac_secret = models.CharField(max_length=512)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "vendor_credentials"

    def __str__(self) -> str:
        return self.vendor_id

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def is_anonymous(self) -> bool:
        return False


class WebhookEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    idempotency_key = models.CharField(max_length=512, unique=True, db_index=True)
    source_vendor = models.CharField(max_length=256, null=True, db_index=True)
    raw_payload = models.JSONField()
    status = models.CharField(
        max_length=16,
        choices=WebhookStatus.choices,
        default=WebhookStatus.RECEIVED,
    )
    event_type = models.CharField(max_length=32, choices=EVENT_TYPE_CHOICES, null=True)
    normalized_data = models.JSONField(null=True)
    # Server-side `NOW()` rather than Python `auto_now_add`: under clock
    # skew between API replicas, host-side timestamps make
    # `ORDER BY created_at` unstable.
    created_at = models.DateTimeField(db_default=Now())
    processed_at = models.DateTimeField(null=True)
    error_message = models.TextField(null=True)
    requires_review = models.BooleanField(default=False, db_index=True)

    class Meta:
        db_table = "webhook_events"
        # Composite index serves both the sweeper
        # (`status=X AND created_at < cutoff ORDER BY created_at LIMIT N`)
        # AND status-only queries via the leftmost B-tree prefix.
        indexes = [
            models.Index(fields=["status", "created_at"], name="webhook_status_created_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.idempotency_key} ({self.status})"


class ShipmentRecord(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    webhook_event = models.OneToOneField(
        WebhookEvent,
        on_delete=models.CASCADE,
        related_name="shipment_record",
        db_column="webhook_event_id",
    )
    vendor_id = models.CharField(max_length=256)
    tracking_number = models.CharField(max_length=256)
    status = models.CharField(max_length=16)
    timestamp = models.DateTimeField()

    class Meta:
        db_table = "shipments"

    def __str__(self) -> str:
        return f"{self.vendor_id} {self.tracking_number} ({self.status})"


class InvoiceRecord(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    webhook_event = models.OneToOneField(
        WebhookEvent,
        on_delete=models.CASCADE,
        related_name="invoice_record",
        db_column="webhook_event_id",
    )
    vendor_id = models.CharField(max_length=256)
    invoice_id = models.CharField(max_length=256)
    # Numeric(20, 4) round-trips Pydantic Decimal exactly. Float would
    # truncate sub-cent precision and accumulate rounding when summed.
    amount = models.DecimalField(max_digits=20, decimal_places=4)
    currency = models.CharField(max_length=3)

    class Meta:
        db_table = "invoices"

    def __str__(self) -> str:
        return f"{self.vendor_id} {self.invoice_id} {self.amount} {self.currency}"
