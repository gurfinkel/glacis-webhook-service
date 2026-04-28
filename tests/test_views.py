"""End-to-end view tests with mocked downstream (Redis/Temporal/DB).

These exercise the full DRF view stack — auth, permission, serializer,
view body — but stub the I/O modules. True integration tests live behind
@pytest.mark.integration and require docker-compose up.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from rest_framework.test import APIClient
from standardwebhooks.webhooks import Webhook
from temporalio.service import RPCError, RPCStatusCode

from webhooks.models import VendorCredential

TEST_SECRET = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"


def _sign(body: bytes, vendor_id: str = "fedex", secret: str = TEST_SECRET) -> dict:
    """Mint StandardWebhooks headers for `body` so the auth class accepts it."""
    msg_id = "msg_test_123"
    ts = datetime.now(tz=UTC)
    sig = Webhook(secret).sign(msg_id=msg_id, timestamp=ts, data=body.decode())
    return {
        "HTTP_WEBHOOK_ID": msg_id,
        "HTTP_WEBHOOK_TIMESTAMP": str(int(ts.timestamp())),
        "HTTP_WEBHOOK_SIGNATURE": sig,
        "HTTP_X_VENDOR_ID": vendor_id,
    }


@pytest.fixture
def vendor():
    return VendorCredential.objects.create(vendor_id="fedex", hmac_secret=TEST_SECRET)


@pytest.fixture
def client():
    return APIClient()


@pytest.mark.django_db
class TestIngestUnauth:
    def test_no_signature_returns_401(self, client):
        response = client.post("/webhook", data={"x": 1}, format="json")
        assert response.status_code == 401

    def test_unknown_vendor_returns_401(self, client):
        body = b'{"x":1}'
        response = client.post(
            "/webhook", data=body, content_type="application/json",
            **_sign(body, vendor_id="ghost"),
        )
        assert response.status_code == 401


@pytest.mark.django_db
class TestIngestSuccessPath:
    @patch("webhooks.views.dedup")
    @patch("webhooks.views.storage")
    @patch("webhooks.views.get_temporal_client")
    @patch("webhooks.views.submit")
    def test_accepted_threads_vendor_identity(
        self, mock_submit, mock_get_client, mock_storage, mock_dedup, vendor, client,
    ):
        mock_dedup.check_duplicate.return_value = (False, "fedex:msg_test_123")
        mock_dedup.mark_as_seen.return_value = None
        mock_storage.insert_webhook_event.return_value = "evt-1"
        mock_get_client.return_value = MagicMock()
        mock_submit.return_value = None

        body = b'{"trackingNumber":"X"}'
        response = client.post(
            "/webhook", data=body, content_type="application/json", **_sign(body),
        )
        assert response.status_code == 200, response.content
        data = response.json()
        assert data["status"] == "accepted"
        assert "idempotency_key" in data

        # Vendor identity threaded into all three downstream calls.
        assert mock_dedup.check_duplicate.call_args.kwargs["vendor_id"] == "fedex"
        assert mock_storage.insert_webhook_event.call_args.kwargs["source_vendor"] == "fedex"
        assert mock_dedup.mark_as_seen.call_args.kwargs["vendor_id"] == "fedex"


@pytest.mark.django_db
class TestIngestErrorMapping:
    @patch("webhooks.views.dedup")
    def test_redis_failure_returns_503(self, mock_dedup, vendor, client):
        mock_dedup.check_duplicate.side_effect = ConnectionError("redis: refused")
        body = b'{"x":1}'
        response = client.post(
            "/webhook", data=body, content_type="application/json", **_sign(body),
        )
        assert response.status_code == 503

    @patch("webhooks.views.dedup")
    @patch("webhooks.views.storage")
    def test_db_operational_error_returns_503(
        self, mock_storage, mock_dedup, vendor, client,
    ):
        from django.db import OperationalError
        mock_dedup.check_duplicate.return_value = (False, "fedex:msg_test_123")
        mock_storage.insert_webhook_event.side_effect = OperationalError("connect refused")
        body = b'{"x":1}'
        response = client.post(
            "/webhook", data=body, content_type="application/json", **_sign(body),
        )
        assert response.status_code == 503

    @patch("webhooks.views.dedup")
    @patch("webhooks.views.storage")
    @patch("webhooks.views.get_temporal_client")
    @patch("webhooks.views.submit")
    def test_temporal_unavailable_returns_503(
        self, mock_submit, mock_get_client, mock_storage, mock_dedup, vendor, client,
    ):
        mock_dedup.check_duplicate.return_value = (False, "fedex:msg_test_123")
        mock_storage.insert_webhook_event.return_value = "evt-1"
        mock_get_client.return_value = MagicMock()
        mock_submit.side_effect = RPCError(
            "frontend down", RPCStatusCode.UNAVAILABLE, b"",
        )
        body = b'{"x":1}'
        response = client.post(
            "/webhook", data=body, content_type="application/json", **_sign(body),
        )
        assert response.status_code == 503

    @patch("webhooks.views.dedup")
    @patch("webhooks.views.storage")
    @patch("webhooks.views.get_temporal_client")
    @patch("webhooks.views.submit")
    def test_bridge_overloaded_returns_503_with_retry_after(
        self, mock_submit, mock_get_client, mock_storage, mock_dedup, vendor, client,
    ):
        """When the bridge in-flight cap is hit, the view must 503
        immediately with Retry-After=1 — not stack on the submit
        timeout. The DB row is durably persisted; the sweeper picks up
        the unprocessed RECEIVED row on the next interval."""
        from project.temporal_bridge import BridgeOverloadedError

        mock_dedup.check_duplicate.return_value = (False, "fedex:msg_test_123")
        mock_storage.insert_webhook_event.return_value = "evt-1"
        mock_get_client.return_value = MagicMock()
        mock_submit.side_effect = BridgeOverloadedError("64 in-flight, cap=64")

        body = b'{"x":1}'
        response = client.post(
            "/webhook", data=body, content_type="application/json", **_sign(body),
        )
        assert response.status_code == 503
        assert response.headers.get("Retry-After") == "1"

    @patch("webhooks.views.dedup")
    @patch("webhooks.views.storage")
    @patch("webhooks.views.get_temporal_client")
    @patch("webhooks.views.submit")
    def test_temporal_already_exists_returns_already_received(
        self, mock_submit, mock_get_client, mock_storage, mock_dedup, vendor, client,
    ):
        mock_dedup.check_duplicate.return_value = (False, "fedex:msg_test_123")
        mock_storage.insert_webhook_event.return_value = "evt-1"
        mock_get_client.return_value = MagicMock()
        mock_submit.side_effect = RPCError(
            "already started", RPCStatusCode.ALREADY_EXISTS, b"",
        )
        body = b'{"x":1}'
        response = client.post(
            "/webhook", data=body, content_type="application/json", **_sign(body),
        )
        assert response.status_code == 200
        assert response.json()["status"] == "already_received"


@pytest.mark.django_db
class TestIngestBodyValidation:
    @patch("webhooks.views.dedup")
    def test_invalid_json_returns_422(self, mock_dedup, vendor, client):
        # Need to sign the bad body so auth doesn't 401 first.
        body = b"not json"
        response = client.post(
            "/webhook", data=body, content_type="application/json", **_sign(body),
        )
        assert response.status_code == 422

    @patch("webhooks.views.dedup")
    def test_non_object_returns_422(self, mock_dedup, vendor, client):
        body = b"[1, 2, 3]"
        response = client.post(
            "/webhook", data=body, content_type="application/json", **_sign(body),
        )
        assert response.status_code == 422

    def test_oversized_body_returns_413(self, vendor, client):
        """Django raises `RequestDataTooBig` on body access when the body
        exceeds `DATA_UPLOAD_MAX_MEMORY_SIZE` (1MB). The exception fires
        before the view body runs, so neither dedup nor storage are touched
        — no mocks needed. The DRF exception handler catches the Django
        exception and renders 413 as RFC 7807 Problem Details."""
        body = b'{"x":"' + b"a" * (1_048_576 + 100) + b'"}'
        response = client.post(
            "/webhook", data=body, content_type="application/json", **_sign(body),
        )
        assert response.status_code == 413
        body_json = response.json()
        assert body_json["status"] == 413
        assert body_json["title"] == "Payload too large"
        assert body_json["type"] == "/errors/payload-too-large"
        assert "instance" in body_json
        assert response.headers.get("Cache-Control") == "no-store"


@pytest.mark.django_db
class TestLiveness:
    def test_alive(self, client):
        response = client.get("/health/live")
        assert response.status_code == 200
        assert response.json() == {"status": "alive"}


@pytest.mark.django_db
class TestResponseConventions:
    """The ingest API has three wire-shape conventions worth pinning so
    future contributors can't silently regress them:

    - Replay responses carry `Idempotent-Replayed: true` so SDKs can
      detect a dedup'd retry from the header alone.
    - All ingest responses set `Cache-Control: no-store` — webhook
      replies must never be cached by intermediaries.
    - Error responses use RFC 7807 Problem Details (`type`, `title`,
      `status`, `detail`, `instance`).
    """

    @patch("webhooks.views.dedup")
    def test_replay_carries_idempotent_replayed_header(
        self, mock_dedup, vendor, client,
    ):
        mock_dedup.check_duplicate.return_value = (True, "fedex:msg_test_123")
        body = b'{"x":1}'
        response = client.post(
            "/webhook", data=body, content_type="application/json", **_sign(body),
        )
        assert response.status_code == 200
        assert response.json()["status"] == "already_received"
        assert response.headers.get("Idempotent-Replayed") == "true"
        assert response.headers.get("Cache-Control") == "no-store"

    @patch("webhooks.views.dedup")
    @patch("webhooks.views.storage")
    @patch("webhooks.views.get_temporal_client")
    @patch("webhooks.views.submit")
    def test_fresh_accept_does_not_carry_replayed_header(
        self, mock_submit, mock_get_client, mock_storage, mock_dedup, vendor, client,
    ):
        mock_dedup.check_duplicate.return_value = (False, "fedex:msg_test_123")
        mock_dedup.mark_as_seen.return_value = None
        mock_storage.insert_webhook_event.return_value = "evt-1"
        mock_get_client.return_value = MagicMock()
        mock_submit.return_value = None
        body = b'{"x":1}'
        response = client.post(
            "/webhook", data=body, content_type="application/json", **_sign(body),
        )
        assert response.status_code == 200
        assert response.json()["status"] == "accepted"
        assert "Idempotent-Replayed" not in response.headers
        assert response.headers.get("Cache-Control") == "no-store"

    @patch("webhooks.views.dedup")
    def test_error_response_is_rfc7807(self, mock_dedup, vendor, client):
        body = b"not json"
        response = client.post(
            "/webhook", data=body, content_type="application/json", **_sign(body),
        )
        assert response.status_code == 422
        body_json = response.json()
        # Required RFC 7807 fields:
        assert body_json["type"].startswith("/errors/")
        assert isinstance(body_json["title"], str) and body_json["title"]
        assert body_json["status"] == 422
        assert isinstance(body_json["detail"], str) and body_json["detail"]
        assert body_json["instance"] == "/webhook"
        assert response.headers.get("Cache-Control") == "no-store"

    def test_unauthenticated_response_is_rfc7807(self, client):
        response = client.post("/webhook", data={"x": 1}, format="json")
        assert response.status_code == 401
        body_json = response.json()
        assert body_json["type"] == "/errors/unauthenticated"
        assert body_json["status"] == 401
        assert response.headers.get("Cache-Control") == "no-store"
