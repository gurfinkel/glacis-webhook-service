"""OpenRouter LLM client.

Async — runs inside Temporal activities. Network errors and 5xx are
raised as `LLMError` (Temporal retries). 4xx misconfigurations are
raised as `LLMPermanentError` and listed in the workflow's
`non_retryable_error_types` to fail fast.
"""

from __future__ import annotations

import json
import logging

import httpx
from django.conf import settings
from pydantic import ValidationError

from webhooks.domain import ClassificationResult, VerificationResult
from webhooks.prompts import (
    SYSTEM_PROMPT,
    VERIFIER_SYSTEM_PROMPT,
    build_user_prompt,
    build_verifier_prompt,
)

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
TIMEOUT_SECONDS = 30

_http_client: httpx.AsyncClient | None = None


class LLMError(Exception):
    """Transient LLM failure — Temporal should retry."""


class LLMPermanentError(Exception):
    """Non-retryable LLM failure (auth, malformed request, model not found)."""


class LLMSchemaRejectedError(Exception):
    """LLM returned valid JSON that didn't match `ClassificationResult`.

    Distinct from a real UNCLASSIFIED extraction. Examples: invoice
    with no `invoice_id`, shipment with `status="CANCELLED"`, mixed
    shipment+invoice fields. The activity layer routes these to the
    review queue with the raw output preserved so a sudden spike fires
    alarms instead of vanishing into the unclassified bucket.
    """

    def __init__(self, raw_output: dict | list, validation_message: str):
        self.raw_output = raw_output
        self.validation_message = validation_message
        super().__init__(f"LLM output failed schema validation: {validation_message}")


_NON_RETRYABLE_STATUSES = frozenset({400, 401, 403, 404, 422})


async def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
    return _http_client


async def close_http_client() -> None:
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None


async def _post_to_openrouter(system_prompt: str, user_prompt: str, *, label: str) -> dict | list:
    """Call OpenRouter and return the parsed JSON content. `label`
    distinguishes classifier from verifier in error messages.

    `response_format: json_object` is OpenAI's JSON-mode flag.
    OpenRouter passes it through but not every model honors it; the
    defensive unwrap below + Pydantic validation are what enforce
    shape.
    """
    try:
        client = await get_http_client()
        response = await client.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.OPENROUTER_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.0,
                "max_tokens": 1024,
                "response_format": {"type": "json_object"},
            },
        )
    except httpx.HTTPError as e:
        raise LLMError(f"{label} API call failed: {e}") from e

    if response.status_code in _NON_RETRYABLE_STATUSES:
        raise LLMPermanentError(
            f"{label} API rejected request with {response.status_code}: {response.text[:500]}"
        )
    if response.status_code >= 400:
        raise LLMError(f"{label} API returned {response.status_code}: {response.text[:500]}")

    raw_content = response.json()["choices"][0]["message"]["content"]
    try:
        parsed = json.loads(raw_content)
    except json.JSONDecodeError as e:
        raise LLMError(f"{label} returned invalid JSON: {e}") from e

    return _unwrap_response(parsed)


def _unwrap_response(parsed: dict | list) -> dict | list:
    """Some models wrap a single object in `{"results": [...]}` or
    return a bare array despite the prompt asking for one object. We
    accept either shape rather than retry."""
    if isinstance(parsed, dict) and "results" in parsed and isinstance(parsed["results"], list):
        return parsed["results"][0] if parsed["results"] else parsed
    if isinstance(parsed, list):
        return parsed[0] if parsed else {"event_type": "unclassified"}
    return parsed


async def classify(payload: dict) -> ClassificationResult:
    """One LLM call per payload. Raises LLMError / LLMPermanentError per
    the error taxonomy; the activity's RetryPolicy decides retry vs
    fail-fast. A schema-validation failure raises
    `LLMSchemaRejectedError` so the activity can route to review with
    the raw output preserved instead of degrading to UNCLASSIFIED."""
    parsed = await _post_to_openrouter(
        SYSTEM_PROMPT, build_user_prompt(payload), label="LLM",
    )
    try:
        return ClassificationResult.model_validate(parsed)
    except ValidationError as e:
        logger.error("LLM output failed schema validation: %s", e)
        raise LLMSchemaRejectedError(
            raw_output=parsed if isinstance(parsed, (dict, list)) else {"raw": str(parsed)},
            validation_message=str(e),
        ) from e


async def verify(payload: dict, extraction: dict) -> VerificationResult:
    """Second-pass groundedness check. A verifier output that fails its
    own schema is converted to `grounded=False` rather than re-raised:
    when the verifier itself can't be trusted, route the row to review
    rather than fail the pipeline."""
    parsed = await _post_to_openrouter(
        VERIFIER_SYSTEM_PROMPT,
        build_verifier_prompt(payload, extraction),
        label="Verifier",
    )
    try:
        return VerificationResult.model_validate(parsed)
    except ValidationError as e:
        logger.error("Verifier output failed schema validation: %s", e)
        return VerificationResult(
            grounded=False,
            unsupported_fields=[],
            missing_fields=[],
            notes=f"verifier output failed schema validation: {str(e)[:300]}",
        )
