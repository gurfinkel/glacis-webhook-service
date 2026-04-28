"""Pydantic LLM-contract tests. No DB."""

from datetime import datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from webhooks.domain import (
    ClassificationResult,
    EventType,
    Invoice,
    Shipment,
    VerificationResult,
)


class TestShipment:
    def test_valid_shipment(self):
        s = Shipment(
            vendor_id="FEDEX",
            tracking_number="794644790132",
            status="TRANSIT",
            timestamp=datetime(2026, 1, 15, 14, 30, 0),
        )
        assert s.vendor_id == "FEDEX"
        assert s.status == "TRANSIT"

    def test_invalid_status_rejected(self):
        with pytest.raises(ValidationError):
            Shipment(
                vendor_id="FEDEX",
                tracking_number="X",
                status="UNKNOWN",  # type: ignore[arg-type]
                timestamp=datetime(2026, 1, 15),
            )

    def test_whitespace_vendor_id_stripped(self):
        s = Shipment(
            vendor_id="  FEDEX  ",
            tracking_number="123",
            status="TRANSIT",
            timestamp=datetime(2026, 1, 1),
        )
        assert s.vendor_id == "FEDEX"

    def test_empty_vendor_id_rejected(self):
        with pytest.raises(ValidationError, match="vendor_id must not be empty"):
            Shipment(
                vendor_id="  ",
                tracking_number="123",
                status="TRANSIT",
                timestamp=datetime(2026, 1, 1),
            )

    def test_empty_tracking_number_rejected(self):
        with pytest.raises(ValidationError, match="tracking_number must not be empty"):
            Shipment(
                vendor_id="FEDEX",
                tracking_number="",
                status="TRANSIT",
                timestamp=datetime(2026, 1, 1),
            )

    def test_all_valid_statuses(self):
        for status in ("TRANSIT", "DELIVERED", "EXCEPTION"):
            s = Shipment(
                vendor_id="TEST",
                tracking_number="123",
                status=status,
                timestamp=datetime(2026, 1, 1),
            )
            assert s.status == status


class TestInvoice:
    def test_valid_invoice(self):
        inv = Invoice(
            vendor_id="ACME", invoice_id="INV-001", amount=Decimal("1500.00"), currency="USD",
        )
        assert inv.amount == Decimal("1500.00")

    def test_amount_preserves_precision(self):
        inv = Invoice(
            vendor_id="ACME", invoice_id="INV-001",
            amount=Decimal("0.1") + Decimal("0.2"),
            currency="USD",
        )
        assert inv.amount == Decimal("0.3")

    def test_amount_accepts_string(self):
        inv = Invoice(
            vendor_id="ACME", invoice_id="INV-001",
            amount="1500.0001",  # type: ignore[arg-type]
            currency="USD",
        )
        assert inv.amount == Decimal("1500.0001")

    def test_negative_amount_rejected(self):
        with pytest.raises(ValidationError, match="non-negative"):
            Invoice(vendor_id="ACME", invoice_id="A", amount=Decimal("-50.0"), currency="USD")

    def test_invalid_currency_rejected(self):
        with pytest.raises(ValidationError, match="ISO 4217"):
            Invoice(vendor_id="ACME", invoice_id="A", amount=Decimal("100.0"), currency="us")

    def test_currency_too_long_rejected(self):
        with pytest.raises(ValidationError, match="ISO 4217"):
            Invoice(vendor_id="ACME", invoice_id="A", amount=Decimal("100.0"), currency="USDX")

    def test_invalid_iso_code_rejected(self):
        """Garbage three-letter codes that aren't on the ISO 4217 list
        are rejected at the model layer — they reach the schema-rejected
        review path."""
        with pytest.raises(ValidationError, match="ISO 4217"):
            Invoice(
                vendor_id="ACME", invoice_id="A",
                amount=Decimal("100.0"), currency="ZZZ",
            )

    def test_zero_amount_accepted(self):
        inv = Invoice(vendor_id="ACME", invoice_id="A", amount=Decimal("0.0"), currency="EUR")
        assert inv.amount == 0


class TestClassificationResult:
    def test_shipment_result(self):
        result = ClassificationResult(
            event_type=EventType.SHIPMENT,
            shipment=Shipment(
                vendor_id="DHL", tracking_number="DHL-123",
                status="DELIVERED", timestamp=datetime(2026, 1, 15),
            ),
        )
        assert result.event_type == EventType.SHIPMENT
        assert result.invoice is None

    def test_invoice_result(self):
        result = ClassificationResult(
            event_type=EventType.INVOICE,
            invoice=Invoice(
                vendor_id="A", invoice_id="I-1", amount=Decimal("2500.00"), currency="EUR",
            ),
        )
        assert result.event_type == EventType.INVOICE
        assert result.shipment is None

    def test_unclassified_result(self):
        result = ClassificationResult(event_type=EventType.UNCLASSIFIED)
        assert result.shipment is None
        assert result.invoice is None

    def test_shipment_type_without_data_rejected(self):
        with pytest.raises(ValidationError, match="shipment data required"):
            ClassificationResult(event_type=EventType.SHIPMENT)

    def test_invoice_type_without_data_rejected(self):
        with pytest.raises(ValidationError, match="invoice data required"):
            ClassificationResult(event_type=EventType.INVOICE)

    def test_from_dict_invalid_llm_output(self):
        raw = {
            "event_type": "shipment",
            "shipment": {
                "vendor_id": "FEDEX",
                # missing tracking_number
                "status": "TRANSIT",
                "timestamp": "2026-01-15T10:00:00Z",
            },
        }
        with pytest.raises(ValidationError):
            ClassificationResult.model_validate(raw)

    def test_shipment_event_with_invoice_data_rejected(self):
        with pytest.raises(ValidationError, match="invoice must be null"):
            ClassificationResult(
                event_type=EventType.SHIPMENT,
                shipment=Shipment(
                    vendor_id="FEDEX", tracking_number="1Z",
                    status="TRANSIT", timestamp=datetime(2026, 1, 1),
                ),
                invoice=Invoice(
                    vendor_id="ACME", invoice_id="I-1",
                    amount=Decimal("10.0"), currency="USD",
                ),
            )

    def test_invoice_event_with_shipment_data_rejected(self):
        with pytest.raises(ValidationError, match="shipment must be null"):
            ClassificationResult(
                event_type=EventType.INVOICE,
                shipment=Shipment(
                    vendor_id="FEDEX", tracking_number="1Z",
                    status="TRANSIT", timestamp=datetime(2026, 1, 1),
                ),
                invoice=Invoice(
                    vendor_id="ACME", invoice_id="I-1",
                    amount=Decimal("10.0"), currency="USD",
                ),
            )

    def test_unclassified_with_data_rejected(self):
        with pytest.raises(ValidationError, match="must both be null"):
            ClassificationResult(
                event_type=EventType.UNCLASSIFIED,
                shipment=Shipment(
                    vendor_id="FEDEX", tracking_number="1Z",
                    status="TRANSIT", timestamp=datetime(2026, 1, 1),
                ),
            )


class TestVerificationResult:
    def test_grounded_with_no_findings(self):
        v = VerificationResult(grounded=True)
        assert v.grounded is True
        assert v.unsupported_fields == []
        assert v.missing_fields == []
        assert v.notes == ""

    def test_ungrounded_with_unsupported_fields(self):
        v = VerificationResult(
            grounded=False,
            unsupported_fields=["shipment.tracking_number"],
            notes="not in source",
        )
        assert v.grounded is False
        assert v.unsupported_fields == ["shipment.tracking_number"]

    def test_grounded_true_with_unsupported_fields_rejected(self):
        """Internal consistency: grounded=True is incompatible with
        unsupported_fields being non-empty. The verifier saying both
        'this is grounded' and 'I couldn't locate field X' is a logical
        contradiction; the model layer rejects it."""
        with pytest.raises(ValidationError, match="grounded=True is incompatible"):
            VerificationResult(
                grounded=True,
                unsupported_fields=["shipment.tracking_number"],
            )

    def test_grounded_true_with_missing_fields_allowed(self):
        """Missing (omitted) source fields are a *weaker* signal than
        unsupported (hallucinated) extracted fields. A grounded
        extraction may still legitimately omit some source fields the
        verifier flags as notable — tolerated."""
        v = VerificationResult(
            grounded=True,
            missing_fields=["delivery_address"],
        )
        assert v.grounded is True
        assert v.missing_fields == ["delivery_address"]
