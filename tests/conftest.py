import pytest


@pytest.fixture
def sample_shipment_payload():
    return {
        "TrackingNumber": "794644790132",
        "EventType": "IN_TRANSIT",
        "CarrierCode": "FEDEX",
        "EventTimestamp": "2026-01-15T14:30:00Z",
    }


@pytest.fixture
def sample_invoice_payload():
    return {
        "doc_type": "INV",
        "invoice_number": "INV-2026-0042",
        "vendor_code": "ACME_LOGISTICS",
        "total": 1402.50,
        "curr": "USD",
    }


@pytest.fixture
def sample_garbage_payload():
    return {
        "heartbeat": True,
        "server": "vendor-gateway-03",
        "uptime_seconds": 864000,
    }
