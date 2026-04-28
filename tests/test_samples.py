"""Smoke + live-LLM regression tests over `samples/*.json`. The live
tests are gated on `RUN_LLM_TESTS=1` and `pytest -m llm` so a stray
key doesn't trigger billed calls."""

import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from webhooks.domain import EventType
from webhooks.prompts import (
    PAYLOAD_DELIM_CLOSE,
    PAYLOAD_DELIM_OPEN,
    build_user_prompt,
)

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"


def _all_sample_files() -> list[Path]:
    return sorted(SAMPLES_DIR.glob("*.json"))


@dataclass(frozen=True)
class SampleExpectation:
    """What the live LLM should emit for a sample.

    `event_type=None` means "ambiguous": the model should either return
    UNCLASSIFIED (it knows it can't extract) OR an extraction that the
    verifier flags as ungrounded so the activity layer routes it to
    review. Both outcomes protect downstream from a hallucinated
    extraction landing in `shipments`/`invoices`."""

    event_type: EventType | None


SAMPLE_EXPECTATIONS: dict[str, SampleExpectation] = {
    "shipment_dhl.json": SampleExpectation(EventType.SHIPMENT),
    "shipment_fedex.json": SampleExpectation(EventType.SHIPMENT),
    "invoice_vendor_a.json": SampleExpectation(EventType.INVOICE),
    "invoice_vendor_b.json": SampleExpectation(EventType.INVOICE),
    "garbage_payload.json": SampleExpectation(EventType.UNCLASSIFIED),
    # Ambiguous inventory-notification payload — looks shipment-adjacent
    # (items + status="processing") but lacks tracking_number / vendor_id.
    "ambiguous_payload.json": SampleExpectation(None),
}


@pytest.mark.parametrize("sample_path", _all_sample_files(), ids=lambda p: p.name)
class TestSamplePayloads:
    def test_sample_is_valid_json_object(self, sample_path: Path):
        data = json.loads(sample_path.read_text())
        assert isinstance(data, dict)

    def test_sample_can_be_wrapped_in_user_prompt(self, sample_path: Path):
        data = json.loads(sample_path.read_text())
        prompt = build_user_prompt(data)
        assert PAYLOAD_DELIM_OPEN in prompt
        assert PAYLOAD_DELIM_CLOSE in prompt


def test_samples_directory_is_not_empty():
    assert _all_sample_files()


_RUN_LLM_TESTS = os.environ.get("RUN_LLM_TESTS") == "1"


@pytest.mark.llm
@pytest.mark.skipif(not _RUN_LLM_TESTS, reason="Set RUN_LLM_TESTS=1 to enable live LLM tests")
@pytest.mark.parametrize(
    "sample_name,expectation",
    list(SAMPLE_EXPECTATIONS.items()),
    ids=lambda v: v if isinstance(v, str) else (
        v.event_type.value if v.event_type else "ambiguous"
    ),
)
@pytest.mark.asyncio
async def test_llm_classifies_sample(sample_name: str, expectation: SampleExpectation):
    """Live-LLM regression: each bundled sample must produce the documented
    outcome. Catches prompt drift that structural-contract tests can't see.
    Ambiguous samples (`expectation.event_type is None`) pass on either
    UNCLASSIFIED or an extraction the verifier flags ungrounded — both
    route safely."""
    from workflows.llm_client import classify, verify

    payload = json.loads((SAMPLES_DIR / sample_name).read_text())
    result = await classify(payload)

    if expectation.event_type is not None:
        assert result.event_type == expectation.event_type, (
            f"{sample_name}: expected {expectation.event_type.value}, "
            f"got {result.event_type.value}"
        )
    else:
        if result.event_type == EventType.UNCLASSIFIED:
            return  # Safe by classification.
        # Otherwise the verifier must reject the extraction.
        verification = await verify(payload, result.model_dump(mode="json"))
        assert not verification.grounded, (
            f"{sample_name}: ambiguous payload produced a grounded "
            f"{result.event_type.value} extraction. Either the prompt "
            f"is hallucinating fields, or the verifier missed it."
        )


@pytest.mark.llm
@pytest.mark.skipif(not _RUN_LLM_TESTS, reason="Set RUN_LLM_TESTS=1 to enable live LLM tests")
@pytest.mark.parametrize(
    "sample_name",
    [n for n, e in SAMPLE_EXPECTATIONS.items() if e.event_type in
     (EventType.SHIPMENT, EventType.INVOICE)],
)
@pytest.mark.asyncio
async def test_verifier_grounds_classifier_extractions(sample_name: str):
    """The verifier must agree with the classifier on samples that have
    a ground-truth extraction available — i.e. shipments and invoices.
    A failure here means either:
      - The classifier is hallucinating fields that aren't in the source.
      - The verifier is over-strict and flags legitimate normalizations
        (status-mapping, vendor-id derivation).
    Either is a real regression; both deserve operator visibility."""
    from workflows.llm_client import classify, verify

    payload = json.loads((SAMPLES_DIR / sample_name).read_text())
    classification = await classify(payload)
    verification = await verify(payload, classification.model_dump(mode="json"))
    assert verification.grounded, (
        f"{sample_name}: classifier extracted but verifier disagreed. "
        f"unsupported={verification.unsupported_fields} "
        f"notes={verification.notes}"
    )
