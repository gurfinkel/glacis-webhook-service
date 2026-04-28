"""Django ORM layer — uniqueness, defaults, FK round-trips."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from django.db import IntegrityError

from webhooks.models import (
    EventType,
    InvoiceRecord,
    ShipmentRecord,
    VendorCredential,
    WebhookEvent,
    WebhookStatus,
)


@pytest.mark.django_db
class TestWebhookEvent:
    def test_minimal_create_defaults_to_received(self):
        e = WebhookEvent.objects.create(
            idempotency_key="fedex:evt-1",
            raw_payload={"trackingNumber": "X"},
        )
        assert e.status == WebhookStatus.RECEIVED
        assert e.requires_review is False
        assert e.event_type is None
        assert e.normalized_data is None
        assert e.created_at is not None

    def test_idempotency_key_unique(self):
        WebhookEvent.objects.create(
            idempotency_key="fedex:dup",
            raw_payload={"x": 1},
        )
        with pytest.raises(IntegrityError):
            WebhookEvent.objects.create(
                idempotency_key="fedex:dup",
                raw_payload={"x": 2},
            )

    def test_status_choices_enforced(self):
        """Pins the canonical status strings the storage helpers'
        terminal-state guard depends on."""
        assert WebhookStatus.RECEIVED == "RECEIVED"
        assert WebhookStatus.PROCESSING == "PROCESSING"
        assert WebhookStatus.COMPLETED == "COMPLETED"
        assert WebhookStatus.FAILED == "FAILED"

    def test_event_type_choices(self):
        assert EventType.SHIPMENT == "shipment"
        assert EventType.INVOICE == "invoice"
        assert EventType.UNCLASSIFIED == "unclassified"


@pytest.mark.django_db
class TestShipmentRecord:
    def test_create_with_fk_to_event(self):
        e = WebhookEvent.objects.create(idempotency_key="fedex:s1", raw_payload={})
        s = ShipmentRecord.objects.create(
            webhook_event=e,
            vendor_id="FEDEX",
            tracking_number="794644790132",
            status="TRANSIT",
            timestamp=datetime(2026, 1, 15, 14, 30, tzinfo=UTC),
        )
        assert s.webhook_event_id == e.id  # type: ignore[attr-defined]
        assert s.tracking_number == "794644790132"

    def test_one_shipment_per_event(self):
        e = WebhookEvent.objects.create(idempotency_key="fedex:s2", raw_payload={})
        ShipmentRecord.objects.create(
            webhook_event=e,
            vendor_id="FEDEX",
            tracking_number="A",
            status="TRANSIT",
            timestamp=datetime(2026, 1, 15, tzinfo=UTC),
        )
        with pytest.raises(IntegrityError):
            ShipmentRecord.objects.create(
                webhook_event=e,
                vendor_id="FEDEX",
                tracking_number="B",
                status="DELIVERED",
                timestamp=datetime(2026, 1, 15, tzinfo=UTC),
            )


@pytest.mark.django_db
class TestInvoiceRecord:
    def test_amount_preserves_precision(self):
        """The whole point of Decimal-over-float: 0.1 + 0.2 must equal 0.3
        when round-tripped through the DB column."""
        e = WebhookEvent.objects.create(idempotency_key="acme:i1", raw_payload={})
        InvoiceRecord.objects.create(
            webhook_event=e,
            vendor_id="ACME",
            invoice_id="INV-001",
            amount=Decimal("0.1") + Decimal("0.2"),
            currency="USD",
        )
        loaded = InvoiceRecord.objects.get(webhook_event=e)
        assert loaded.amount == Decimal("0.3")

    def test_sub_cent_precision_roundtrips(self):
        e = WebhookEvent.objects.create(idempotency_key="acme:i2", raw_payload={})
        InvoiceRecord.objects.create(
            webhook_event=e,
            vendor_id="ACME",
            invoice_id="INV-002",
            amount=Decimal("1500.0001"),
            currency="USD",
        )
        loaded = InvoiceRecord.objects.get(webhook_event=e)
        assert loaded.amount == Decimal("1500.0001")

    def test_one_invoice_per_event(self):
        e = WebhookEvent.objects.create(idempotency_key="acme:i3", raw_payload={})
        InvoiceRecord.objects.create(
            webhook_event=e,
            vendor_id="ACME",
            invoice_id="A",
            amount=Decimal("10.00"),
            currency="USD",
        )
        with pytest.raises(IntegrityError):
            InvoiceRecord.objects.create(
                webhook_event=e,
                vendor_id="ACME",
                invoice_id="B",
                amount=Decimal("20.00"),
                currency="USD",
            )


@pytest.mark.django_db
class TestVendorCredential:
    def test_vendor_id_unique(self):
        VendorCredential.objects.create(vendor_id="fedex", hmac_secret="whsec_a")
        with pytest.raises(IntegrityError):
            VendorCredential.objects.create(vendor_id="fedex", hmac_secret="whsec_b")

    def test_active_default_true(self):
        c = VendorCredential.objects.create(vendor_id="ups", hmac_secret="whsec_x")
        assert c.active is True
