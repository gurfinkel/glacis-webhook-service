"""Workflow tests using Temporal's in-memory test environment.

`WorkflowEnvironment.start_time_skipping()` boots an embedded Temporal server
that fast-forwards through retry backoffs — so we verify the full retry
topology in milliseconds instead of waiting for real exponential delays.
"""

import uuid
from datetime import datetime
from unittest.mock import patch

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from webhooks.domain import ClassificationResult, EventType, Shipment
from workflows.definitions import ClassifyWebhookWorkflow
from workflows.llm_client import LLMError

TEST_TASK_QUEUE = "test-webhook-classification"


def _shipment_classification() -> dict:
    return ClassificationResult(
        event_type=EventType.SHIPMENT,
        shipment=Shipment(
            vendor_id="FEDEX",
            tracking_number="794644790132",
            status="TRANSIT",
            timestamp=datetime(2026, 1, 15, 14, 30),
        ),
    ).model_dump(mode="json")


@pytest.mark.asyncio
class TestClassifyWebhookWorkflow:
    """End-to-end orchestration tests with mocked side-effecting activities."""

    async def _run_with_mocked_activities(
        self,
        *,
        classify_side_effects: list,
        verify_side_effects: list | None = None,
        persist_side_effects: list | None = None,
    ) -> tuple[list, list, list, list, list]:
        classify_calls: list = []
        verify_calls: list = []
        persist_calls: list = []
        mark_processing_calls: list = []
        mark_failed_calls: list = []

        @activity.defn(name="mark_processing")
        async def fake_mark_processing(idempotency_key: str) -> None:
            mark_processing_calls.append(idempotency_key)

        @activity.defn(name="classify_payload")
        async def fake_classify_payload(payload: dict) -> dict:
            classify_calls.append(payload)
            effect = classify_side_effects[min(len(classify_calls) - 1, len(classify_side_effects) - 1)]
            if isinstance(effect, Exception):
                raise effect
            return effect

        @activity.defn(name="verify_extraction")
        async def fake_verify_extraction(payload: dict, classification_dict: dict) -> dict:
            verify_calls.append((payload, classification_dict))
            if verify_side_effects:
                effect = verify_side_effects[min(len(verify_calls) - 1, len(verify_side_effects) - 1)]
                if isinstance(effect, Exception):
                    raise effect
                return effect
            # Default: a "grounded" verification result so the happy path
            # in tests doesn't have to spell it out.
            return {
                "grounded": True,
                "unsupported_fields": [],
                "missing_fields": [],
                "notes": "",
            }

        @activity.defn(name="persist_classification")
        async def fake_persist_classification(
            idempotency_key: str,
            event_id: str,
            classification_dict: dict,
            verification_dict: dict | None = None,
        ) -> None:
            persist_calls.append(
                (idempotency_key, event_id, classification_dict, verification_dict),
            )
            if persist_side_effects:
                effect = persist_side_effects[min(len(persist_calls) - 1, len(persist_side_effects) - 1)]
                if isinstance(effect, Exception):
                    raise effect

        @activity.defn(name="mark_failed")
        async def fake_mark_failed(idempotency_key: str, error: str) -> None:
            mark_failed_calls.append((idempotency_key, error))

        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(
                env.client,
                task_queue=TEST_TASK_QUEUE,
                workflows=[ClassifyWebhookWorkflow],
                activities=[
                    fake_mark_processing,
                    fake_classify_payload,
                    fake_verify_extraction,
                    fake_persist_classification,
                    fake_mark_failed,
                ],
            ):
                workflow_id = f"test-{uuid.uuid4()}"
                try:
                    await env.client.execute_workflow(
                        ClassifyWebhookWorkflow.run,
                        args=[workflow_id, "evt-1", {"TrackingNumber": "X"}],
                        id=workflow_id,
                        task_queue=TEST_TASK_QUEUE,
                    )
                except WorkflowFailureError:
                    pass

        return (
            mark_processing_calls, classify_calls, verify_calls,
            persist_calls, mark_failed_calls,
        )

    async def test_happy_path(self):
        """Workflow runs classify → verify → persist in order. Persist
        receives the verification dict as its 4th arg."""
        mp, classify, verify, persist, mf = await self._run_with_mocked_activities(
            classify_side_effects=[{
                "kind": "ok", "result": _shipment_classification(),
            }],
        )
        assert len(mp) == 1
        assert len(classify) == 1
        assert len(verify) == 1
        assert len(persist) == 1
        assert len(mf) == 0
        # Persist got the verifier's result in its 4th arg.
        _, _, _, verification_dict = persist[0]
        assert verification_dict is not None
        assert verification_dict["grounded"] is True

    async def test_retries_classify_on_transient_error(self):
        mp, classify, verify, persist, mf = await self._run_with_mocked_activities(
            classify_side_effects=[
                LLMError("rate limit"),
                LLMError("connection reset"),
                {"kind": "ok", "result": _shipment_classification()},
            ],
        )
        assert len(classify) == 3
        assert len(verify) == 1
        assert len(persist) == 1
        assert len(mf) == 0

    @patch("django.conf.settings.MAX_RETRIES", 2)
    async def test_marks_failed_after_classify_exhausts_retries(self):
        mp, classify, verify, persist, mf = await self._run_with_mocked_activities(
            classify_side_effects=[
                LLMError("rate limit"),
                LLMError("rate limit"),
                LLMError("rate limit"),
            ],
        )
        assert len(classify) == 2
        assert len(verify) == 0  # never reached
        assert len(persist) == 0
        assert len(mf) == 1
        assert "rate limit" in mf[0][1]

    async def test_skips_verifier_on_unclassified(self):
        """UNCLASSIFIED has no extracted fields to verify — workflow
        skips the verifier and persists with verification=None."""
        mp, classify, verify, persist, mf = await self._run_with_mocked_activities(
            classify_side_effects=[{
                "kind": "ok",
                "result": {
                    "event_type": "unclassified",
                    "shipment": None, "invoice": None,
                },
            }],
        )
        assert len(classify) == 1
        assert len(verify) == 0
        assert len(persist) == 1
        _, _, _, verification_dict = persist[0]
        assert verification_dict is None

    async def test_skips_verifier_on_schema_rejected(self):
        """Schema-rejected extractions go straight to review — no need
        to verify a malformed extraction."""
        mp, classify, verify, persist, mf = await self._run_with_mocked_activities(
            classify_side_effects=[{
                "kind": "schema_rejected",
                "raw_llm_output": {"event_type": "shipment"},
                "validation_message": "shipment data required",
            }],
        )
        assert len(classify) == 1
        assert len(verify) == 0  # schema-rejected → skip verifier
        assert len(persist) == 1

    async def test_retries_verifier_on_transient_error(self):
        """Verifier is on the same retry policy as classify — transient
        failures retry, and a successful retry reaches persist."""
        mp, classify, verify, persist, mf = await self._run_with_mocked_activities(
            classify_side_effects=[{
                "kind": "ok", "result": _shipment_classification(),
            }],
            verify_side_effects=[
                LLMError("verifier 503"),
                {
                    "grounded": True, "unsupported_fields": [],
                    "missing_fields": [], "notes": "",
                },
            ],
        )
        assert len(classify) == 1
        assert len(verify) == 2
        assert len(persist) == 1
        assert len(mf) == 0
