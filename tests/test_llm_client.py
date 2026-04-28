"""LLM client error taxonomy + defensive response unwrapping."""

import json
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from webhooks.domain import EventType
from workflows.llm_client import (
    LLMError,
    LLMPermanentError,
    LLMSchemaRejectedError,
    classify,
    verify,
)


def _mock_response(result: dict, status_code: int = 200) -> httpx.Response:
    content = json.dumps(result)
    return httpx.Response(
        status_code=status_code,
        json={"choices": [{"message": {"content": content}}]},
        request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
    )


@pytest.mark.asyncio
class TestClassify:
    @patch("workflows.llm_client.get_http_client")
    async def test_shipment_classified(self, mock_get_client):
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({
            "event_type": "shipment",
            "shipment": {
                "vendor_id": "FEDEX", "tracking_number": "794644790132",
                "status": "TRANSIT", "timestamp": "2026-01-15T14:30:00Z",
            },
            "invoice": None,
        })
        mock_get_client.return_value = mock_client

        result = await classify({"TrackingNumber": "794644790132"})
        assert result.event_type == EventType.SHIPMENT
        assert result.shipment is not None
        assert result.shipment.tracking_number == "794644790132"

    @patch("workflows.llm_client.get_http_client")
    async def test_invalid_json_raises_llm_error(self, mock_get_client):
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = httpx.Response(
            status_code=200,
            json={"choices": [{"message": {"content": "not valid json {{"}}]},
            request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
        )
        mock_get_client.return_value = mock_client

        with pytest.raises(LLMError, match="invalid JSON"):
            await classify({"test": 1})

    @patch("workflows.llm_client.get_http_client")
    async def test_http_error_raises_llm_error(self, mock_get_client):
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.side_effect = httpx.HTTPError("connection refused")
        mock_get_client.return_value = mock_client

        with pytest.raises(LLMError, match="API call failed"):
            await classify({"test": 1})

    @patch("workflows.llm_client.get_http_client")
    async def test_malformed_result_raises_schema_rejected(self, mock_get_client):
        """Used to silently degrade to UNCLASSIFIED — invisible to operators
        if a model/prompt drift breaks 30% of payloads. Now raises a distinct
        exception so the activity layer can route to the review queue with
        the raw output preserved."""
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response(
            {"event_type": "shipment"}
        )
        mock_get_client.return_value = mock_client

        with pytest.raises(LLMSchemaRejectedError) as exc_info:
            await classify({"test": 1})

        assert exc_info.value.raw_output == {"event_type": "shipment"}
        assert "shipment data required" in exc_info.value.validation_message

    @patch("workflows.llm_client.get_http_client")
    async def test_wrapped_response_handled(self, mock_get_client):
        """Defensive unwrap: some models wrap result in `{"results": [...]}`."""
        mock_client = AsyncMock()
        mock_client.is_closed = False
        content = json.dumps({
            "results": [
                {"event_type": "unclassified", "shipment": None, "invoice": None},
            ]
        })
        mock_client.post.return_value = httpx.Response(
            status_code=200,
            json={"choices": [{"message": {"content": content}}]},
            request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
        )
        mock_get_client.return_value = mock_client

        result = await classify({"test": 1})
        assert result.event_type == EventType.UNCLASSIFIED

    @patch("workflows.llm_client.get_http_client")
    async def test_bare_array_response_handled(self, mock_get_client):
        """Defensive unwrap: bare array despite the prompt asking for an object."""
        mock_client = AsyncMock()
        mock_client.is_closed = False
        content = json.dumps([{
            "event_type": "shipment",
            "shipment": {
                "vendor_id": "UPS", "tracking_number": "1Z999",
                "status": "DELIVERED", "timestamp": "2026-01-15T14:30:00Z",
            },
            "invoice": None,
        }])
        mock_client.post.return_value = httpx.Response(
            status_code=200,
            json={"choices": [{"message": {"content": content}}]},
            request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
        )
        mock_get_client.return_value = mock_client

        result = await classify({"test": 1})
        assert result.event_type == EventType.SHIPMENT
        assert result.shipment is not None
        assert result.shipment.tracking_number == "1Z999"

    @patch("workflows.llm_client.get_http_client")
    async def test_empty_array_falls_back_to_unclassified(self, mock_get_client):
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = httpx.Response(
            status_code=200,
            json={"choices": [{"message": {"content": "[]"}}]},
            request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
        )
        mock_get_client.return_value = mock_client

        result = await classify({"test": 1})
        assert result.event_type == EventType.UNCLASSIFIED

    @patch("workflows.llm_client.get_http_client")
    async def test_invoice_classified(self, mock_get_client):
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _mock_response({
            "event_type": "invoice",
            "shipment": None,
            "invoice": {
                "vendor_id": "ACME", "invoice_id": "INV-001",
                "amount": 1500.00, "currency": "USD",
            },
        })
        mock_get_client.return_value = mock_client

        result = await classify({"doc_type": "INV"})
        assert result.event_type == EventType.INVOICE
        assert result.invoice is not None
        assert result.invoice.amount == Decimal("1500.00")

    @patch("workflows.llm_client.get_http_client")
    @pytest.mark.parametrize("status_code", [400, 401, 403, 404, 422])
    async def test_4xx_misconfig_raises_permanent_error(self, mock_get_client, status_code):
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = httpx.Response(
            status_code=status_code,
            json={"error": {"message": "auth failed"}},
            request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
        )
        mock_get_client.return_value = mock_client

        with pytest.raises(LLMPermanentError):
            await classify({"x": 1})

    @patch("workflows.llm_client.get_http_client")
    @pytest.mark.parametrize("status_code", [429, 500, 502, 503])
    async def test_transient_status_raises_retryable_error(self, mock_get_client, status_code):
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = httpx.Response(
            status_code=status_code,
            json={"error": {"message": "transient"}},
            request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
        )
        mock_get_client.return_value = mock_client

        with pytest.raises(LLMError):
            await classify({"x": 1})


def _verifier_response(result: dict, status_code: int = 200) -> httpx.Response:
    content = json.dumps(result)
    return httpx.Response(
        status_code=status_code,
        json={"choices": [{"message": {"content": content}}]},
        request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
    )


@pytest.mark.asyncio
class TestVerify:
    """The verifier's contract is much narrower than the extractor's:
    given (payload, extraction), report whether each extracted value can
    be located in the source. Same error taxonomy as classify."""

    _EXTRACTION = {
        "event_type": "shipment",
        "shipment": {
            "vendor_id": "FEDEX", "tracking_number": "794644790132",
            "status": "TRANSIT", "timestamp": "2026-01-15T14:30:00Z",
        },
        "invoice": None,
    }

    @patch("workflows.llm_client.get_http_client")
    async def test_grounded_extraction_returns_grounded_true(self, mock_get_client):
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _verifier_response({
            "grounded": True,
            "unsupported_fields": [],
            "missing_fields": [],
            "notes": "all fields located in source",
        })
        mock_get_client.return_value = mock_client

        result = await verify(
            {"TrackingNumber": "794644790132"}, self._EXTRACTION,
        )
        assert result.grounded is True
        assert result.unsupported_fields == []

    @patch("workflows.llm_client.get_http_client")
    async def test_ungrounded_extraction_returns_grounded_false(self, mock_get_client):
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _verifier_response({
            "grounded": False,
            "unsupported_fields": ["shipment.tracking_number"],
            "missing_fields": [],
            "notes": "tracking_number 794644790132 not in source",
        })
        mock_get_client.return_value = mock_client

        result = await verify({"foo": "bar"}, self._EXTRACTION)
        assert result.grounded is False
        assert result.unsupported_fields == ["shipment.tracking_number"]

    @patch("workflows.llm_client.get_http_client")
    async def test_verifier_schema_rejection_falls_back_to_grounded_false(self, mock_get_client):
        """Verifier emits malformed output (e.g. wrong field names). We
        do NOT propagate the schema failure — we treat it as 'we can't
        trust the verifier, route to review'. A noisy verifier should
        be conservative, not pipeline-stopping."""
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = _verifier_response({
            "wrong_field_names": True,  # not the contract
        })
        mock_get_client.return_value = mock_client

        result = await verify({"foo": "bar"}, self._EXTRACTION)
        assert result.grounded is False
        assert "verifier output failed schema validation" in result.notes

    @patch("workflows.llm_client.get_http_client")
    async def test_http_error_raises_llm_error(self, mock_get_client):
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.side_effect = httpx.HTTPError("connection refused")
        mock_get_client.return_value = mock_client

        with pytest.raises(LLMError, match="Verifier API call failed"):
            await verify({"x": 1}, self._EXTRACTION)

    @patch("workflows.llm_client.get_http_client")
    @pytest.mark.parametrize("status_code", [400, 401, 403, 404, 422])
    async def test_4xx_raises_permanent_error(self, mock_get_client, status_code):
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = httpx.Response(
            status_code=status_code,
            json={"error": {"message": "auth failed"}},
            request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
        )
        mock_get_client.return_value = mock_client

        with pytest.raises(LLMPermanentError):
            await verify({"x": 1}, self._EXTRACTION)

    @patch("workflows.llm_client.get_http_client")
    @pytest.mark.parametrize("status_code", [429, 500, 502, 503])
    async def test_transient_status_raises_retryable_error(self, mock_get_client, status_code):
        mock_client = AsyncMock()
        mock_client.is_closed = False
        mock_client.post.return_value = httpx.Response(
            status_code=status_code,
            json={"error": {"message": "transient"}},
            request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
        )
        mock_get_client.return_value = mock_client

        with pytest.raises(LLMError):
            await verify({"x": 1}, self._EXTRACTION)
