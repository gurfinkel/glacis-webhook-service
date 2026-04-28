"""Dedup helpers — pure-function unit tests + integration tests against Redis."""

import pytest

from webhooks.dedup import check_duplicate, compute_idempotency_key, mark_as_seen


class TestComputeIdempotencyKey:
    def test_format_is_vendor_colon_webhook_id(self):
        assert compute_idempotency_key("evt-abc-123", vendor_id="fedex") == "fedex:evt-abc-123"

    def test_namespaces_by_vendor(self):
        a = compute_idempotency_key("evt-1", vendor_id="fedex")
        b = compute_idempotency_key("evt-1", vendor_id="ups")
        assert a == "fedex:evt-1"
        assert b == "ups:evt-1"
        assert a != b


@pytest.fixture
def clean_redis():
    """Provide a clean Redis instance for each test."""
    from webhooks.dedup import get_redis

    r = get_redis()
    keys = list(r.scan_iter("webhook:*"))  # type: ignore[arg-type]
    if keys:
        r.delete(*keys)
    yield r
    keys = list(r.scan_iter("webhook:*"))  # type: ignore[arg-type]
    if keys:
        r.delete(*keys)


@pytest.mark.integration
class TestCheckAndMark:
    def test_new_event_not_duplicate(self, clean_redis):
        is_dup, key = check_duplicate("evt-NEW-001", vendor_id="fedex")
        assert is_dup is False
        assert key == "fedex:evt-NEW-001"

    def test_marked_event_is_duplicate(self, clean_redis):
        original_key = compute_idempotency_key("evt-DUP-001", vendor_id="fedex")
        mark_as_seen(original_key, "evt-DUP-001", vendor_id="fedex")
        is_dup, key = check_duplicate("evt-DUP-001", vendor_id="fedex")
        assert is_dup is True
        assert key == original_key

    def test_different_webhook_ids_not_duplicate(self, clean_redis):
        """Different `webhook-id` headers from the same vendor are
        treated as different events — even with identical bodies upstream.
        Standard Webhooks contract: the message ID is the dedup token."""
        mark_as_seen("fedex:evt-1", "evt-1", vendor_id="fedex")
        is_dup, _ = check_duplicate("evt-2", vendor_id="fedex")
        assert is_dup is False

    def test_same_webhook_id_different_vendors_not_duplicate(self, clean_redis):
        """Vendor-namespacing: two vendors emitting `webhook-id: evt-42`
        are independent events."""
        mark_as_seen("fedex:evt-42", "evt-42", vendor_id="fedex")
        is_dup, key = check_duplicate("evt-42", vendor_id="ups")
        assert is_dup is False
        assert key == "ups:evt-42"

    def test_check_is_read_only(self, clean_redis):
        """check_duplicate doesn't mark — calling it twice on a fresh
        event still reports not-duplicate."""
        is_dup1, _ = check_duplicate("evt-CHECK-ONLY", vendor_id="fedex")
        is_dup2, _ = check_duplicate("evt-CHECK-ONLY", vendor_id="fedex")
        assert is_dup1 is False
        assert is_dup2 is False
