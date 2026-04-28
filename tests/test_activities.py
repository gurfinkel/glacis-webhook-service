"""Activity unit tests — call activities directly with patched DB helpers."""

from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from webhooks.domain import (
    ClassificationResult,
    EventType,
    Invoice,
    Shipment,
    VerificationResult,
)
from webhooks.models import WebhookStatus
from workflows.activities import (
    _needs_review,
    classify_payload,
    mark_failed,
    mark_processing,
    persist_classification,
    verify_extraction,
)


def _shipment_result() -> ClassificationResult:
    return ClassificationResult(
        event_type=EventType.SHIPMENT,
        shipment=Shipment(
            vendor_id="FEDEX",
            tracking_number="794644790132",
            status="TRANSIT",
            timestamp=datetime(2026, 1, 15, 14, 30),
        ),
    )


def _unclassified_result() -> ClassificationResult:
    return ClassificationResult(event_type=EventType.UNCLASSIFIED)


def _grounded() -> "VerificationResult":
    from webhooks.domain import VerificationResult
    return VerificationResult(
        grounded=True, unsupported_fields=[], missing_fields=[], notes="",
    )


def _ungrounded() -> "VerificationResult":
    from webhooks.domain import VerificationResult
    return VerificationResult(
        grounded=False,
        unsupported_fields=["shipment.tracking_number"],
        missing_fields=[],
        notes="not in source",
    )


class TestNeedsReview:
    def test_grounded_supported_currency_does_not_need_review(self):
        result = ClassificationResult(
            event_type=EventType.INVOICE,
            invoice=Invoice(vendor_id="A", invoice_id="I-1", amount=Decimal("10"), currency="USD"),
        )
        assert _needs_review(result, _grounded()) is False

    def test_ungrounded_needs_review(self):
        """Verifier flagged extracted values not present in the source
        — strongest hallucination signal, route to review."""
        assert _needs_review(_shipment_result(), _ungrounded()) is True

    def test_unclassified_grounded_does_not_need_review(self):
        """UNCLASSIFIED is the verifier-skipped path; passing
        verification=None is the workflow's signal here."""
        assert _needs_review(_unclassified_result(), None) is False


@pytest.mark.asyncio
class TestMarkProcessing:
    @patch("workflows.activities.sync_to_async")
    async def test_writes_processing_status(self, mock_sync):
        mock_inner = AsyncMock()
        mock_sync.return_value = mock_inner
        await mark_processing("test-key")
        mock_inner.assert_awaited_once_with("test-key")


@pytest.mark.asyncio
class TestClassifyPayload:
    @patch("workflows.activities.classify")
    async def test_returns_classified_kind_dict(self, mock_classify):
        mock_classify.return_value = _shipment_result()
        wire = await classify_payload({"TrackingNumber": "X"})
        assert wire["kind"] == "ok"
        assert wire["result"]["event_type"] == "shipment"
        assert wire["result"]["shipment"]["tracking_number"] == "794644790132"

    @patch("workflows.activities.classify")
    async def test_propagates_llm_errors_for_temporal_retry(self, mock_classify):
        from workflows.llm_client import LLMError
        mock_classify.side_effect = LLMError("rate limited")
        with pytest.raises(LLMError):
            await classify_payload({"x": 1})

    @patch("workflows.activities.classify")
    async def test_schema_rejected_returned_as_discriminated_union(self, mock_classify):
        """When the LLM emits valid JSON in the wrong shape, classify_payload
        must NOT propagate the exception (which would burn Temporal retries
        on a deterministic failure). It returns a `kind=schema_rejected` dict
        carrying the raw output, which the persist activity then routes to
        review."""
        from workflows.llm_client import LLMSchemaRejectedError
        mock_classify.side_effect = LLMSchemaRejectedError(
            raw_output={"event_type": "shipment"},
            validation_message="shipment data required when event_type is 'shipment'",
        )
        wire = await classify_payload({"x": 1})
        assert wire["kind"] == "schema_rejected"
        assert wire["raw_llm_output"] == {"event_type": "shipment"}
        assert "shipment data required" in wire["validation_message"]


_GROUNDED = {
    "grounded": True, "unsupported_fields": [],
    "missing_fields": [], "notes": "",
}
_UNGROUNDED = {
    "grounded": False,
    "unsupported_fields": ["shipment.tracking_number"],
    "missing_fields": [],
    "notes": "tracking_number not found in source payload",
}


@pytest.mark.asyncio
class TestPersistClassification:
    @patch("workflows.activities.sync_to_async")
    async def test_grounded_extraction_promotes_to_fk_record(self, mock_sync):
        """Grounded extraction promotes to FK record; verification result
        is stamped into normalized_data so the admin UI can surface it."""
        mock_inner = AsyncMock()
        mock_sync.return_value = mock_inner

        await persist_classification(
            idempotency_key="test-key",
            event_id="evt-1",
            classification_dict=_shipment_result().model_dump(mode="json"),
            verification_dict=_GROUNDED,
        )

        mock_inner.assert_awaited_once()
        assert mock_inner.await_args is not None
        call_kwargs = mock_inner.await_args.kwargs
        assert call_kwargs["event_type"] == "shipment"
        assert call_kwargs["record"] is not None
        assert call_kwargs["requires_review"] is False
        assert call_kwargs["normalized_data"]["verification"]["grounded"] is True

    @patch("workflows.activities.sync_to_async")
    async def test_ungrounded_routes_to_review_with_reason(self, mock_sync):
        """Verifier flagged the extraction as ungrounded — the row goes
        to review with reason='ungrounded' and the FK record is NOT
        written. This is the strongest 'model hallucinated' signal."""
        mock_inner = AsyncMock()
        mock_sync.return_value = mock_inner

        await persist_classification(
            idempotency_key="test-key",
            event_id="evt-1",
            classification_dict=_shipment_result().model_dump(mode="json"),
            verification_dict=_UNGROUNDED,
        )

        assert mock_inner.await_args is not None
        call_kwargs = mock_inner.await_args.kwargs
        assert call_kwargs["requires_review"] is True
        assert call_kwargs["record"] is None
        normalized = call_kwargs["normalized_data"]
        assert normalized["review_reason"] == "ungrounded"
        assert normalized["verification"]["grounded"] is False
        assert "tracking_number" in normalized["verification"]["unsupported_fields"][0]

    @patch("workflows.activities.sync_to_async")
    async def test_unclassified_persisted_without_review_flag(self, mock_sync):
        """UNCLASSIFIED has no extracted fields to verify, so the workflow
        passes verification_dict=None. Persist accepts the None and
        does NOT flag the row for review."""
        mock_inner = AsyncMock()
        mock_sync.return_value = mock_inner

        await persist_classification(
            idempotency_key="test-key",
            event_id="evt-1",
            classification_dict=_unclassified_result().model_dump(mode="json"),
            verification_dict=None,
        )

        assert mock_inner.await_args is not None
        call_kwargs = mock_inner.await_args.kwargs
        assert call_kwargs["event_type"] == "unclassified"
        assert call_kwargs["record"] is None
        assert call_kwargs["requires_review"] is False

    @patch("workflows.activities.sync_to_async")
    async def test_missing_verification_for_classified_routes_to_review(self, mock_sync):
        """Defensive: a non-UNCLASSIFIED extraction without a verifier
        result means the workflow skipped verification when it shouldn't
        have. Surface it with reason='verifier_skipped' rather than
        auto-promoting to FK records."""
        mock_inner = AsyncMock()
        mock_sync.return_value = mock_inner

        await persist_classification(
            idempotency_key="test-key",
            event_id="evt-1",
            classification_dict=_shipment_result().model_dump(mode="json"),
            verification_dict=None,
        )

        assert mock_inner.await_args is not None
        call_kwargs = mock_inner.await_args.kwargs
        assert call_kwargs["requires_review"] is True
        assert call_kwargs["record"] is None
        assert call_kwargs["normalized_data"]["review_reason"] == "verifier_skipped"

    @patch("workflows.activities.sync_to_async")
    async def test_classified_kind_wrapped_dict_persists_normally(self, mock_sync):
        """The wire format `{"kind":"ok","result":<dump>}` must produce
        the same persistence call as the bare-dump path used by
        replayed Temporal histories."""
        mock_inner = AsyncMock()
        mock_sync.return_value = mock_inner

        await persist_classification(
            idempotency_key="test-key",
            event_id="evt-1",
            classification_dict={
                "kind": "ok",
                "result": _shipment_result().model_dump(mode="json"),
            },
            verification_dict=_GROUNDED,
        )

        assert mock_inner.await_args is not None
        call_kwargs = mock_inner.await_args.kwargs
        assert call_kwargs["event_type"] == "shipment"
        assert call_kwargs["record"] is not None
        assert call_kwargs["requires_review"] is False

    @patch("workflows.activities.sync_to_async")
    async def test_schema_rejected_routed_to_review_with_raw_output(self, mock_sync):
        """A Pydantic ValidationError on the LLM output must NOT silently
        degrade to UNCLASSIFIED. It lands in the review queue with the
        raw output and the validation message preserved so an operator
        can see what the model actually emitted.

        `raw_llm_output` is wrapped in `{"truncated": bool, "content": ...}`
        so downstream consumers don't have to handle a dict-or-string union.
        """
        mock_inner = AsyncMock()
        mock_sync.return_value = mock_inner

        await persist_classification(
            idempotency_key="test-key",
            event_id="evt-1",
            classification_dict={
                "kind": "schema_rejected",
                "raw_llm_output": {
                    "event_type": "shipment",
                    "shipment": {
                        "vendor_id": "FEDEX",
                        "tracking_number": "X",
                        "status": "CANCELLED",
                        "timestamp": "2026-01-15T14:30:00Z",
                    },
                },
                "validation_message": (
                    "1 validation error for ClassificationResult: status — "
                    "Input should be 'TRANSIT', 'DELIVERED' or 'EXCEPTION'"
                ),
            },
        )

        assert mock_inner.await_args is not None
        call_kwargs = mock_inner.await_args.kwargs
        # event_type reflects the LLM's claim; `schema_rejected=True`
        # in normalized_data is what prevents promotion.
        assert call_kwargs["event_type"] == "shipment"
        assert call_kwargs["record"] is None
        assert call_kwargs["requires_review"] is True
        normalized = call_kwargs["normalized_data"]
        assert normalized["schema_rejected"] is True
        wrapped = normalized["raw_llm_output"]
        assert wrapped["truncated"] is False
        assert wrapped["content"]["shipment"]["tracking_number"] == "X"
        assert "TRANSIT" in normalized["validation_message"]

    @patch("workflows.activities.sync_to_async")
    async def test_schema_rejected_falls_back_to_unclassified_when_claim_missing(self, mock_sync):
        """When the LLM emits no recognizable event_type (or an invalid
        one), schema-rejected persistence falls back to UNCLASSIFIED so
        the row still lands in the review queue with a neutral label."""
        mock_inner = AsyncMock()
        mock_sync.return_value = mock_inner

        await persist_classification(
            idempotency_key="test-key",
            event_id="evt-1",
            classification_dict={
                "kind": "schema_rejected",
                "raw_llm_output": {"random": "garbage"},  # no event_type field
                "validation_message": "missing event_type",
            },
        )

        assert mock_inner.await_args is not None
        assert mock_inner.await_args.kwargs["event_type"] == "unclassified"

    @patch("workflows.activities.sync_to_async")
    async def test_schema_rejected_invalid_event_type_falls_back(self, mock_sync):
        """LLM-claimed event_type that isn't one of {shipment, invoice,
        unclassified} must NOT be trusted into the database — fall back
        to UNCLASSIFIED."""
        mock_inner = AsyncMock()
        mock_sync.return_value = mock_inner

        await persist_classification(
            idempotency_key="test-key",
            event_id="evt-1",
            classification_dict={
                "kind": "schema_rejected",
                "raw_llm_output": {"event_type": "<script>alert(1)</script>"},
                "validation_message": "bad type",
            },
        )

        assert mock_inner.await_args is not None
        assert mock_inner.await_args.kwargs["event_type"] == "unclassified"

    @patch("workflows.activities.sync_to_async")
    async def test_schema_rejected_truncates_oversize_raw_output(self, mock_sync):
        """A pathologically long LLM response can't bloat the row.

        Oversize → `{"truncated": True, "content": "<json prefix>...[truncated]"}`.
        Consumers see the consistent shape and the boolean tells them whether
        `content` is structured JSON or a string fragment."""
        mock_inner = AsyncMock()
        mock_sync.return_value = mock_inner

        oversize_payload = {"junk": "x" * 20000, "event_type": "invoice"}
        await persist_classification(
            idempotency_key="test-key",
            event_id="evt-1",
            classification_dict={
                "kind": "schema_rejected",
                "raw_llm_output": oversize_payload,
                "validation_message": "y" * 20000,
            },
        )

        assert mock_inner.await_args is not None
        normalized = mock_inner.await_args.kwargs["normalized_data"]
        wrapped = normalized["raw_llm_output"]
        assert wrapped["truncated"] is True
        assert isinstance(wrapped["content"], str)
        assert wrapped["content"].endswith("...[truncated]")
        assert len(wrapped["content"]) <= 8192 + len("...[truncated]")
        assert len(normalized["validation_message"]) <= 8192
        assert mock_inner.await_args.kwargs["event_type"] == "invoice"


@pytest.mark.asyncio
class TestMarkFailed:
    @patch("workflows.activities.sync_to_async")
    async def test_writes_failed_status_with_error(self, mock_sync):
        mock_inner = AsyncMock()
        mock_sync.return_value = mock_inner

        await mark_failed("test-key", "LLM timeout after 3 attempts")

        mock_inner.assert_awaited_once_with(
            "test-key",
            status=WebhookStatus.FAILED,
            error="LLM timeout after 3 attempts",
        )

    @patch("workflows.activities.sync_to_async")
    async def test_truncates_long_error_message(self, mock_sync):
        mock_inner = AsyncMock()
        mock_sync.return_value = mock_inner

        long_error = "x" * 5000
        await mark_failed("test-key", long_error)

        assert mock_inner.await_args is not None
        call_kwargs = mock_inner.await_args.kwargs
        assert len(call_kwargs["error"]) <= 2048
        assert call_kwargs["error"].endswith("...[truncated]")


@pytest.mark.asyncio
class TestVerifyExtraction:
    """The verify_extraction activity is a thin wrapper around the LLM
    verify() call — it unwraps the discriminated-union shape of the
    classification dict and dumps the VerificationResult to the wire."""

    @patch("workflows.activities.verify")
    async def test_returns_verification_result_dump(self, mock_verify):
        mock_verify.return_value = VerificationResult(
            grounded=True, unsupported_fields=[], missing_fields=[], notes="",
        )
        wire = await verify_extraction(
            payload={"TrackingNumber": "X"},
            classification_dict={"kind": "ok", "result": _shipment_result().model_dump(mode="json")},
        )
        assert wire["grounded"] is True
        assert wire["unsupported_fields"] == []

    @patch("workflows.activities.verify")
    async def test_unwraps_kind_ok_before_passing_to_verifier(self, mock_verify):
        """The classification dict the workflow threads in carries the
        `{"kind":"ok","result":...}` wrapper. verify_extraction must
        pass the inner result to the LLM verify call, not the wrapper."""
        mock_verify.return_value = VerificationResult(
            grounded=True, unsupported_fields=[], missing_fields=[], notes="",
        )
        inner = _shipment_result().model_dump(mode="json")
        await verify_extraction(
            payload={"TrackingNumber": "X"},
            classification_dict={"kind": "ok", "result": inner},
        )
        # verify() was called with the inner result, not the wrapper.
        called_extraction = mock_verify.call_args.args[1]
        assert called_extraction == inner
        assert "kind" not in called_extraction

    @patch("workflows.activities.verify")
    async def test_propagates_llm_errors_for_temporal_retry(self, mock_verify):
        """Transient verifier errors must propagate so Temporal's
        RetryPolicy decides retry vs fail-fast — same contract as
        classify_payload."""
        from workflows.llm_client import LLMError
        mock_verify.side_effect = LLMError("verifier 503")
        with pytest.raises(LLMError):
            await verify_extraction(
                payload={"x": 1},
                classification_dict={"kind": "ok", "result": _shipment_result().model_dump(mode="json")},
            )
