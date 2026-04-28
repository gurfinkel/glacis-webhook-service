"""Admin actions — promote-extraction-to-FK-record correctness."""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from webhooks.admin import WebhookEventAdmin
from webhooks.models import (
    EventType,
    InvoiceRecord,
    ShipmentRecord,
    WebhookEvent,
    WebhookStatus,
)


def _approve_action(queryset, request: MagicMock | None = None):
    """Run the approve_and_promote admin action against a queryset."""
    admin_obj = WebhookEventAdmin(WebhookEvent, MagicMock())
    admin_obj.approve_and_promote(request or MagicMock(), queryset)


def _mark_resolved_action(queryset, request: MagicMock | None = None):
    admin_obj = WebhookEventAdmin(WebhookEvent, MagicMock())
    admin_obj.mark_resolved(request or MagicMock(), queryset)


@pytest.mark.django_db
class TestApproveAndPromote:
    def test_promotes_flagged_shipment_to_fk_record(self):
        """A row in the review queue with a usable extraction can be
        promoted by an operator. The promote logic doesn't depend on
        the review reason — operator inspection precedes the click."""
        e = WebhookEvent.objects.create(
            idempotency_key="fedex:rev-1",
            raw_payload={"x": 1},
            event_type=EventType.SHIPMENT,
            normalized_data={
                "event_type": "shipment",
                "shipment": {
                    "vendor_id": "FEDEX",
                    "tracking_number": "Z123",
                    "status": "TRANSIT",
                    "timestamp": datetime(2026, 1, 15, tzinfo=UTC).isoformat(),
                },
                "invoice": None,
                "review_reason": "ungrounded",
            },
            status=WebhookStatus.COMPLETED,
            requires_review=True,
        )
        _approve_action(WebhookEvent.objects.filter(pk=e.pk))

        e.refresh_from_db()
        assert e.requires_review is False
        assert ShipmentRecord.objects.filter(webhook_event=e).exists()

    def test_promotes_flagged_invoice_to_fk_record(self):
        e = WebhookEvent.objects.create(
            idempotency_key="acme:rev-2",
            raw_payload={"x": 1},
            event_type=EventType.INVOICE,
            normalized_data={
                "event_type": "invoice",
                "shipment": None,
                "invoice": {
                    "vendor_id": "ACME",
                    "invoice_id": "INV-99",
                    "amount": "1234.5600",
                    "currency": "USD",
                },
                "review_reason": "ungrounded",
            },
            status=WebhookStatus.COMPLETED,
            requires_review=True,
        )
        _approve_action(WebhookEvent.objects.filter(pk=e.pk))

        e.refresh_from_db()
        assert e.requires_review is False
        inv = InvoiceRecord.objects.get(webhook_event=e)
        assert inv.invoice_id == "INV-99"
        assert inv.amount == Decimal("1234.5600")

    def test_idempotent_re_approval(self):
        """Re-running the action after a row is already approved is a no-op."""
        e = WebhookEvent.objects.create(
            idempotency_key="fedex:rev-3",
            raw_payload={},
            event_type=EventType.SHIPMENT,
            normalized_data={
                "event_type": "shipment",
                "shipment": {
                    "vendor_id": "FEDEX",
                    "tracking_number": "Y",
                    "status": "DELIVERED",
                    "timestamp": datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
                },
                "invoice": None,
            },
            status=WebhookStatus.COMPLETED,
            requires_review=True,
        )
        _approve_action(WebhookEvent.objects.filter(pk=e.pk))
        _approve_action(WebhookEvent.objects.filter(pk=e.pk))  # second call is no-op

        e.refresh_from_db()
        assert e.requires_review is False
        # Still exactly one shipment record — uniqueness held.
        assert ShipmentRecord.objects.filter(webhook_event=e).count() == 1

    def test_skips_unclassified_or_no_data(self):
        e = WebhookEvent.objects.create(
            idempotency_key="x:rev-4",
            raw_payload={},
            event_type=EventType.UNCLASSIFIED,
            normalized_data={
                "event_type": "unclassified",
                "shipment": None, "invoice": None,
            },
            status=WebhookStatus.COMPLETED,
            requires_review=True,
        )
        _approve_action(WebhookEvent.objects.filter(pk=e.pk))

        e.refresh_from_db()
        assert e.requires_review is True
        assert not ShipmentRecord.objects.filter(webhook_event=e).exists()
        assert not InvoiceRecord.objects.filter(webhook_event=e).exists()

    def test_skips_schema_rejected_with_distinct_message(self):
        """Schema-rejected rows must NOT be promoted (no trustworthy
        extraction). The Approve action skips them and reports a clear
        'use Mark resolved instead' message rather than letting the
        operator guess what went wrong."""
        e = WebhookEvent.objects.create(
            idempotency_key="vendor:bad-shape",
            raw_payload={},
            event_type=EventType.UNCLASSIFIED,
            normalized_data={
                "schema_rejected": True,
                "raw_llm_output": {"truncated": False, "content": {
                    "event_type": "shipment",
                }},
                "validation_message": "shipment data required",
            },
            status=WebhookStatus.COMPLETED,
            requires_review=True,
        )

        # Capture the operator-facing message by stubbing message_user.
        recorded: list[str] = []
        admin_obj = WebhookEventAdmin(WebhookEvent, MagicMock())
        admin_obj.message_user = (  # type: ignore[method-assign]
            lambda req, msg, level=0: recorded.append(str(msg))
        )
        admin_obj.approve_and_promote(MagicMock(), WebhookEvent.objects.filter(pk=e.pk))

        e.refresh_from_db()
        assert e.requires_review is True  # NOT promoted
        assert recorded, "approve_and_promote must call message_user"
        message = recorded[0]
        assert "schema-rejected" in message
        assert "Mark resolved" in message


@pytest.mark.django_db
class TestMarkResolved:
    """`mark_resolved` is scoped to schema-rejected rows only. A naive
    `queryset.update` would let an operator who selects the wrong row
    silently lose a promotable FK record (e.g. ungrounded)."""

    def test_clears_review_flag_for_schema_rejected_row(self):
        e = WebhookEvent.objects.create(
            idempotency_key="vendor:resolved-1",
            raw_payload={},
            event_type=EventType.UNCLASSIFIED,
            normalized_data={
                "schema_rejected": True,
                "raw_llm_output": {"truncated": False, "content": {}},
                "validation_message": "x",
            },
            status=WebhookStatus.COMPLETED,
            requires_review=True,
        )
        _mark_resolved_action(WebhookEvent.objects.filter(pk=e.pk))

        e.refresh_from_db()
        assert e.requires_review is False
        assert not ShipmentRecord.objects.filter(webhook_event=e).exists()
        assert not InvoiceRecord.objects.filter(webhook_event=e).exists()

    def test_does_not_clear_ungrounded_row(self):
        """The accidental-misclick guard: an operator who selects an
        ungrounded row and clicks Mark resolved must NOT clear it —
        Approve (after inspection) is the correct path so they can
        decide whether the verifier was right. Without this scoping,
        the FK record would never be written and `normalized_data`
        would orphan."""
        e = WebhookEvent.objects.create(
            idempotency_key="vendor:ungrounded",
            raw_payload={},
            event_type=EventType.SHIPMENT,
            normalized_data={
                "event_type": "shipment",
                "shipment": {
                    "vendor_id": "F", "tracking_number": "T1",
                    "status": "TRANSIT", "timestamp": "2026-01-15T14:30:00Z",
                },
                "verification": {
                    "grounded": False,
                    "unsupported_fields": ["shipment.tracking_number"],
                    "missing_fields": [], "notes": "",
                },
                "review_reason": "ungrounded",
            },
            status=WebhookStatus.COMPLETED,
            requires_review=True,
        )

        recorded: list[str] = []
        admin_obj = WebhookEventAdmin(WebhookEvent, MagicMock())
        admin_obj.message_user = (  # type: ignore[method-assign]
            lambda req, msg, level=0: recorded.append(str(msg))
        )
        admin_obj.mark_resolved(MagicMock(), WebhookEvent.objects.filter(pk=e.pk))

        e.refresh_from_db()
        assert e.requires_review is True  # NOT cleared
        assert recorded
        message = recorded[0]
        assert "0 schema-rejected" in message
        assert "1 not eligible" in message
        assert "Approve" in message

    def test_idempotent_on_already_resolved_rows(self):
        """Already-resolved rows have requires_review=False and the
        update filter excludes them, so re-running is a no-op."""
        e = WebhookEvent.objects.create(
            idempotency_key="vendor:resolved-2",
            raw_payload={},
            event_type=EventType.UNCLASSIFIED,
            normalized_data={"schema_rejected": True},
            status=WebhookStatus.COMPLETED,
            requires_review=False,
        )
        _mark_resolved_action(WebhookEvent.objects.filter(pk=e.pk))

        e.refresh_from_db()
        assert e.requires_review is False  # unchanged

    def test_handles_mixed_queryset(self):
        """A queryset of (schema_rejected pending, ungrounded pending,
        already-resolved) flips only the schema_rejected one. The
        accidental-misclick on ungrounded doesn't trash the row."""
        e_sr = WebhookEvent.objects.create(
            idempotency_key="vendor:mix-sr",
            raw_payload={},
            event_type=EventType.UNCLASSIFIED,
            normalized_data={"schema_rejected": True, "raw_llm_output": {}},
            status=WebhookStatus.COMPLETED,
            requires_review=True,
        )
        e_ug = WebhookEvent.objects.create(
            idempotency_key="vendor:mix-ungrounded",
            raw_payload={},
            event_type=EventType.SHIPMENT,
            normalized_data={
                "event_type": "shipment",
                "shipment": {
                    "vendor_id": "F", "tracking_number": "T",
                    "status": "TRANSIT", "timestamp": "2026-01-15T14:30:00Z",
                },
                "verification": {
                    "grounded": False,
                    "unsupported_fields": ["shipment.tracking_number"],
                    "missing_fields": [], "notes": "",
                },
                "review_reason": "ungrounded",
            },
            status=WebhookStatus.COMPLETED,
            requires_review=True,
        )
        e_done = WebhookEvent.objects.create(
            idempotency_key="vendor:mix-done",
            raw_payload={},
            event_type=EventType.UNCLASSIFIED,
            normalized_data={"schema_rejected": True},
            status=WebhookStatus.COMPLETED,
            requires_review=False,
        )

        _mark_resolved_action(
            WebhookEvent.objects.filter(idempotency_key__startswith="vendor:mix-"),
        )

        e_sr.refresh_from_db()
        e_ug.refresh_from_db()
        e_done.refresh_from_db()
        assert e_sr.requires_review is False  # cleared
        assert e_ug.requires_review is True   # protected from accidental clear
        assert e_done.requires_review is False  # was already

    def test_message_splits_already_resolved_from_misclick(self):
        """The operator-facing count distinguishes 'already-resolved row
        in selection' (benign, no warning needed) from 'pending
        non-schema-rejected row' (genuine misclick — warn operator to
        use Approve). Lumping both under a single 'skipped' would tell
        the operator to use Approve on rows that need no action."""
        # 1 cleared + 1 already-done + 1 pending misclick.
        WebhookEvent.objects.create(
            idempotency_key="vendor:msg-sr",
            raw_payload={},
            event_type=EventType.UNCLASSIFIED,
            normalized_data={"schema_rejected": True},
            status=WebhookStatus.COMPLETED,
            requires_review=True,
        )
        WebhookEvent.objects.create(
            idempotency_key="vendor:msg-done",
            raw_payload={},
            event_type=EventType.UNCLASSIFIED,
            normalized_data={"schema_rejected": True},
            status=WebhookStatus.COMPLETED,
            requires_review=False,
        )
        WebhookEvent.objects.create(
            idempotency_key="vendor:msg-misclick",
            raw_payload={},
            event_type=EventType.SHIPMENT,
            normalized_data={
                "event_type": "shipment",
                "shipment": {
                    "vendor_id": "F", "tracking_number": "T",
                    "status": "TRANSIT", "timestamp": "2026-01-15T14:30:00Z",
                },
                "verification": {
                    "grounded": False,
                    "unsupported_fields": ["shipment.tracking_number"],
                    "missing_fields": [], "notes": "",
                },
                "review_reason": "ungrounded",
            },
            status=WebhookStatus.COMPLETED,
            requires_review=True,
        )

        recorded: list[str] = []
        admin_obj = WebhookEventAdmin(WebhookEvent, MagicMock())
        admin_obj.message_user = (  # type: ignore[method-assign]
            lambda req, msg, level=0: recorded.append(str(msg))
        )
        admin_obj.mark_resolved(
            MagicMock(),
            WebhookEvent.objects.filter(idempotency_key__startswith="vendor:msg-"),
        )

        assert recorded
        message = recorded[0]
        assert "Marked 1" in message
        assert "1 already resolved" in message
        assert "1 not eligible" in message
        assert "Approve" in message


@pytest.mark.django_db
class TestReviewReasonColumn:
    """Triage at scale needs to distinguish review-queue causes at a glance —
    schema-rejected rows need the prompt fixed, ungrounded rows are likely
    hallucinations."""

    def _admin(self) -> WebhookEventAdmin:
        return WebhookEventAdmin(WebhookEvent, MagicMock())

    def test_schema_rejected_row(self):
        e = WebhookEvent.objects.create(
            idempotency_key="vendor:bad-shape-rr",
            raw_payload={},
            event_type=EventType.UNCLASSIFIED,
            normalized_data={
                "schema_rejected": True,
                "raw_llm_output": {
                    "truncated": False,
                    "content": {"event_type": "shipment"},
                },
                "validation_message": "shipment data required",
            },
            status=WebhookStatus.COMPLETED,
            requires_review=True,
        )
        assert self._admin().review_reason(e) == "schema_rejected"

    def test_ungrounded_row(self):
        """The verifier flagged extracted values that aren't in the
        source. The reason column reads `ungrounded` — the authoritative
        hallucination signal."""
        e = WebhookEvent.objects.create(
            idempotency_key="vendor:ungrounded",
            raw_payload={},
            event_type=EventType.SHIPMENT,
            normalized_data={
                "event_type": "shipment",
                "shipment": {
                    "vendor_id": "F", "tracking_number": "FAKE-1Z9",
                    "status": "TRANSIT", "timestamp": "2026-01-15T14:30:00Z",
                },
                "verification": {
                    "grounded": False,
                    "unsupported_fields": ["shipment.tracking_number"],
                    "missing_fields": [],
                    "notes": "tracking_number not in source",
                },
                "review_reason": "ungrounded",
            },
            status=WebhookStatus.COMPLETED,
            requires_review=True,
        )
        assert self._admin().review_reason(e) == "ungrounded"

    def test_no_review_row(self):
        e = WebhookEvent.objects.create(
            idempotency_key="vendor:done",
            raw_payload={},
            event_type=EventType.SHIPMENT,
            status=WebhookStatus.COMPLETED,
            requires_review=False,
        )
        assert self._admin().review_reason(e) == ""
