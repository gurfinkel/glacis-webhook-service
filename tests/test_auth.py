"""HMAC authentication — accept valid, reject everything else.

We use the real `standardwebhooks.Webhook.sign` to mint signatures matching
what a vendor would send, so the test path exercises the full verify chain.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from rest_framework.exceptions import AuthenticationFailed
from standardwebhooks.webhooks import Webhook

from webhooks.auth import StandardWebhooksAuthentication
from webhooks.models import VendorCredential

# A test secret that conforms to the StandardWebhooks `whsec_<base64>` shape.
TEST_SECRET = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"


def _signed_request(
    body: bytes,
    *,
    vendor_id: str,
    secret: str = TEST_SECRET,
    msg_id: str = "msg_test_123",
    timestamp: datetime | None = None,
) -> MagicMock:
    """Mint a real HMAC signature and wrap it in a mock DRF request."""
    if timestamp is None:
        timestamp = datetime.now(tz=UTC)

    sig = Webhook(secret).sign(
        msg_id=msg_id,
        timestamp=timestamp,
        data=body.decode(),
    )

    request = MagicMock()
    request.body = body
    request.headers = {
        "webhook-id": msg_id,
        "webhook-timestamp": str(int(timestamp.timestamp())),
        "webhook-signature": sig,
        "x-vendor-id": vendor_id,
    }
    return request


@pytest.mark.django_db
class TestStandardWebhooksAuthentication:
    def test_valid_signature_returns_credential(self):
        cred = VendorCredential.objects.create(vendor_id="fedex", hmac_secret=TEST_SECRET)
        body = b'{"trackingNumber":"X"}'
        request = _signed_request(body, vendor_id="fedex")

        auth = StandardWebhooksAuthentication()
        result = auth.authenticate(request)

        assert result is not None
        returned_cred, _token = result
        assert returned_cred.vendor_id == cred.vendor_id

    def test_unknown_vendor_id_rejected(self):
        body = b'{"x":1}'
        request = _signed_request(body, vendor_id="unknown")

        auth = StandardWebhooksAuthentication()
        with pytest.raises(AuthenticationFailed, match="invalid signature"):
            auth.authenticate(request)

    def test_inactive_vendor_rejected(self):
        VendorCredential.objects.create(vendor_id="fedex", hmac_secret=TEST_SECRET, active=False)
        body = b'{"x":1}'
        request = _signed_request(body, vendor_id="fedex")

        auth = StandardWebhooksAuthentication()
        with pytest.raises(AuthenticationFailed, match="invalid signature"):
            auth.authenticate(request)

    def test_wrong_secret_rejected(self):
        VendorCredential.objects.create(vendor_id="fedex", hmac_secret=TEST_SECRET)
        body = b'{"x":1}'
        # Sign with a *different* secret — vendor's claim will fail verification
        # against the real one we have stored.
        request = _signed_request(body, vendor_id="fedex", secret="whsec_evil_secret_value")

        auth = StandardWebhooksAuthentication()
        with pytest.raises(AuthenticationFailed, match="invalid signature"):
            auth.authenticate(request)

    def test_tampered_body_rejected(self):
        VendorCredential.objects.create(vendor_id="fedex", hmac_secret=TEST_SECRET)
        body = b'{"x":1}'
        request = _signed_request(body, vendor_id="fedex")
        # Attacker swaps the body after signing.
        request.body = b'{"x":999}'

        auth = StandardWebhooksAuthentication()
        with pytest.raises(AuthenticationFailed, match="invalid signature"):
            auth.authenticate(request)

    def test_missing_signature_header_returns_none(self):
        """Missing headers → return None (not raise) so DRF can chain to the
        next auth class. The view's IsAuthenticated permission then 401s
        with a generic message."""
        request = MagicMock()
        request.body = b'{"x":1}'
        request.headers = {"x-vendor-id": "fedex"}  # missing webhook-* headers

        auth = StandardWebhooksAuthentication()
        assert auth.authenticate(request) is None

    def test_missing_vendor_id_returns_none(self):
        request = MagicMock()
        request.body = b'{"x":1}'
        request.headers = {
            "webhook-id": "msg_x",
            "webhook-timestamp": "1234567890",
            "webhook-signature": "v1,abc",
        }

        auth = StandardWebhooksAuthentication()
        assert auth.authenticate(request) is None
