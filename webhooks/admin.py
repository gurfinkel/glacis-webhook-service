"""Django admin — review queue UI for flagged extractions."""

from django.contrib import admin, messages
from django.db import transaction

from webhooks.models import (
    EventType,
    InvoiceRecord,
    ShipmentRecord,
    VendorCredential,
    WebhookEvent,
)


def _create_record_from_extraction(event: WebhookEvent) -> bool:
    """Build the FK record from `normalized_data`. Returns True if a
    record was created (or already existed), False if the row has no
    promotable extraction."""
    normalized = event.normalized_data or {}
    if event.event_type == EventType.SHIPMENT and normalized.get("shipment"):
        s = normalized["shipment"]
        ShipmentRecord.objects.get_or_create(
            webhook_event=event,
            defaults={
                "vendor_id": s["vendor_id"],
                "tracking_number": s["tracking_number"],
                "status": s["status"],
                "timestamp": s["timestamp"],
            },
        )
        return True
    if event.event_type == EventType.INVOICE and normalized.get("invoice"):
        i = normalized["invoice"]
        InvoiceRecord.objects.get_or_create(
            webhook_event=event,
            defaults={
                "vendor_id": i["vendor_id"],
                "invoice_id": i["invoice_id"],
                "amount": i["amount"],
                "currency": i["currency"],
            },
        )
        return True
    return False


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    list_display = (
        "idempotency_key", "source_vendor", "event_type",
        "status", "requires_review", "review_reason", "created_at",
    )
    list_filter = ("requires_review", "status", "event_type", "source_vendor")
    search_fields = ("idempotency_key", "source_vendor")
    readonly_fields = (
        "id", "idempotency_key", "source_vendor", "raw_payload",
        "created_at", "processed_at", "error_message",
    )
    actions = ("approve_and_promote", "mark_resolved")
    ordering = ("-created_at",)

    @admin.display(description="reason")
    def review_reason(self, obj: WebhookEvent) -> str:
        if not obj.requires_review:
            return ""
        normalized = obj.normalized_data or {}
        if normalized.get("schema_rejected"):
            return "schema_rejected"
        stamped = normalized.get("review_reason")
        if isinstance(stamped, str) and stamped:
            return stamped
        return ""

    def get_queryset(self, request):
        # Default the changelist to the review queue; landing on
        # every-event-ever is noise. Operators flip the filter on the
        # right rail to see all events.
        qs = super().get_queryset(request)
        if "requires_review__exact" not in request.GET:
            return qs.filter(requires_review=True)
        return qs

    @admin.action(description="Approve: promote extraction to FK record")
    def approve_and_promote(self, request, queryset):
        """Promote the preserved extraction to a FK record and clear the
        review flag. Idempotent — re-running on already-promoted rows
        is a no-op via the unique constraint on `webhook_event_id`.

        Schema-rejected rows are not promotable (no trustworthy
        extraction); they're counted separately so the operator sees a
        clear "use Mark resolved instead" message."""
        promoted = 0
        skipped_already_done = 0
        skipped_schema_rejected = 0
        skipped_unclassified = 0
        with transaction.atomic():
            for event in queryset.select_for_update():
                if not event.requires_review:
                    skipped_already_done += 1
                    continue
                if (event.normalized_data or {}).get("schema_rejected"):
                    skipped_schema_rejected += 1
                    continue
                if not _create_record_from_extraction(event):
                    skipped_unclassified += 1
                    continue
                event.requires_review = False
                event.save(update_fields=["requires_review"])
                promoted += 1

        parts = [f"Promoted {promoted} event(s)"]
        if skipped_already_done:
            parts.append(f"{skipped_already_done} already approved")
        if skipped_unclassified:
            parts.append(f"{skipped_unclassified} unclassified (no extraction to promote)")
        if skipped_schema_rejected:
            parts.append(
                f"{skipped_schema_rejected} schema-rejected — not promotable; "
                f"fix the prompt/schema and re-ingest, or use 'Mark resolved' "
                f"to clear from the queue"
            )
        level = messages.WARNING if skipped_schema_rejected else messages.SUCCESS
        self.message_user(request, " — ".join(parts), level)

    @admin.action(description="Mark resolved (schema-rejected)")
    def mark_resolved(self, request, queryset):
        """Clear `requires_review` without writing an FK record. Scoped
        to schema-rejected rows only — a naive `queryset.update` would
        let an operator who selects a promotable row silently lose the
        FK record they should have written."""
        total = queryset.count()
        already_done = queryset.filter(requires_review=False).count()
        candidates = queryset.filter(
            requires_review=True,
            normalized_data__schema_rejected=True,
        )
        cleared = candidates.update(requires_review=False)
        not_eligible = total - already_done - cleared

        parts = [f"Marked {cleared} schema-rejected event(s) resolved (no FK record written)"]
        if already_done:
            parts.append(f"{already_done} already resolved")
        if not_eligible:
            parts.append(
                f"{not_eligible} not eligible — non-schema-rejected rows must "
                f"go through 'Approve' (ungrounded)"
            )
        level = messages.WARNING if not_eligible else messages.SUCCESS
        self.message_user(request, " — ".join(parts), level)


@admin.register(VendorCredential)
class VendorCredentialAdmin(admin.ModelAdmin):
    list_display = ("vendor_id", "active", "created_at", "updated_at")
    list_filter = ("active",)
    search_fields = ("vendor_id",)
    readonly_fields = ("created_at", "updated_at")
    fields = ("vendor_id", "hmac_secret", "active", "created_at", "updated_at")
