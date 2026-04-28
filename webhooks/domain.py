"""Pydantic models for the LLM extraction contract.

Distinct from `webhooks/models.py` (Django ORM): this module is what
the LLM emits and what the activity layer validates before writing to
the DB.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, field_validator, model_validator

ISO_4217_CURRENCIES: frozenset[str] = frozenset({
    "AED", "AFN", "ALL", "AMD", "ANG", "AOA", "ARS", "AUD", "AWG", "AZN",
    "BAM", "BBD", "BDT", "BGN", "BHD", "BIF", "BMD", "BND", "BOB", "BOV",
    "BRL", "BSD", "BTN", "BWP", "BYN", "BZD", "CAD", "CDF", "CHE", "CHF",
    "CHW", "CLF", "CLP", "CNY", "COP", "COU", "CRC", "CUC", "CUP", "CVE",
    "CZK", "DJF", "DKK", "DOP", "DZD", "EGP", "ERN", "ETB", "EUR", "FJD",
    "FKP", "GBP", "GEL", "GHS", "GIP", "GMD", "GNF", "GTQ", "GYD", "HKD",
    "HNL", "HTG", "HUF", "IDR", "ILS", "INR", "IQD", "IRR", "ISK", "JMD",
    "JOD", "JPY", "KES", "KGS", "KHR", "KMF", "KPW", "KRW", "KWD", "KYD",
    "KZT", "LAK", "LBP", "LKR", "LRD", "LSL", "LYD", "MAD", "MDL", "MGA",
    "MKD", "MMK", "MNT", "MOP", "MRU", "MUR", "MVR", "MWK", "MXN", "MXV",
    "MYR", "MZN", "NAD", "NGN", "NIO", "NOK", "NPR", "NZD", "OMR", "PAB",
    "PEN", "PGK", "PHP", "PKR", "PLN", "PYG", "QAR", "RON", "RSD", "RUB",
    "RWF", "SAR", "SBD", "SCR", "SDG", "SEK", "SGD", "SHP", "SLE", "SOS",
    "SRD", "SSP", "STN", "SVC", "SYP", "SZL", "THB", "TJS", "TMT", "TND",
    "TOP", "TRY", "TTD", "TWD", "TZS", "UAH", "UGX", "USD", "USN", "UYI",
    "UYU", "UYW", "UZS", "VED", "VES", "VND", "VUV", "WST", "XAF", "XCD",
    "XOF", "XPF", "XSU", "XUA", "YER", "ZAR", "ZMW", "ZWG",
})


class EventType(StrEnum):
    SHIPMENT = "shipment"
    INVOICE = "invoice"
    UNCLASSIFIED = "unclassified"


class Shipment(BaseModel):
    vendor_id: str
    tracking_number: str
    status: Literal["TRANSIT", "DELIVERED", "EXCEPTION"]
    timestamp: datetime

    @field_validator("vendor_id")
    @classmethod
    def vendor_id_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("vendor_id must not be empty")
        return v

    @field_validator("tracking_number")
    @classmethod
    def tracking_number_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("tracking_number must not be empty")
        return v


class Invoice(BaseModel):
    """Normalized invoice record.

    `amount` is `Decimal`, not `float`. The spec called for `float`, but
    binary floating-point silently bills customers wrong (`0.1 + 0.2`
    is not `0.3`). The DB column round-trips the same precision.
    """

    vendor_id: str
    invoice_id: str
    amount: Decimal
    currency: str

    @field_validator("vendor_id")
    @classmethod
    def vendor_id_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("vendor_id must not be empty")
        return v

    @field_validator("amount")
    @classmethod
    def amount_must_be_positive(cls, v: Decimal) -> Decimal:
        if v < 0:
            raise ValueError("amount must be non-negative")
        return v

    @field_validator("currency")
    @classmethod
    def currency_must_be_iso(cls, v: str) -> str:
        if v not in ISO_4217_CURRENCIES:
            raise ValueError(
                f"currency must be a valid ISO 4217 code; got {v!r}"
            )
        return v


class ClassificationResult(BaseModel):
    event_type: EventType
    shipment: Shipment | None = None
    invoice: Invoice | None = None

    @model_validator(mode="after")
    def check_data_matches_type(self):
        if self.event_type == EventType.SHIPMENT:
            if self.shipment is None:
                raise ValueError("shipment data required when event_type is 'shipment'")
            if self.invoice is not None:
                raise ValueError("invoice must be null when event_type is 'shipment'")
        elif self.event_type == EventType.INVOICE:
            if self.invoice is None:
                raise ValueError("invoice data required when event_type is 'invoice'")
            if self.shipment is not None:
                raise ValueError("shipment must be null when event_type is 'invoice'")
        else:
            if self.shipment is not None or self.invoice is not None:
                raise ValueError(
                    "shipment and invoice must both be null when event_type is 'unclassified'"
                )
        return self


class VerificationResult(BaseModel):
    """Output of the second-pass verifier — confirms that every extracted
    field is grounded in the source payload."""

    grounded: bool
    unsupported_fields: list[str] = []
    """Extracted field paths whose values weren't located in the source
    (e.g. `'shipment.tracking_number'`)."""

    missing_fields: list[str] = []
    """Source field paths that look meaningful for the event type but
    weren't represented in the extraction. Weaker signal than
    unsupported_fields — a partial extraction is still recoverable."""

    notes: str = ""

    @model_validator(mode="after")
    def check_grounded_consistency(self):
        # grounded=True with non-empty unsupported_fields is a logical
        # contradiction. Missing (omitted) fields are tolerated.
        if self.grounded and self.unsupported_fields:
            raise ValueError(
                "grounded=True is incompatible with unsupported_fields — "
                "the verifier said the extraction is grounded but also listed "
                "fields it couldn't locate in the source"
            )
        return self
