"""HMAC webhook signature verification — Standard Webhooks compliant.

The DRF auth class returns the `VendorCredential` as `request.user` so
downstream code (rate limiter, view) can read `request.user.vendor_id`.
The `X-Vendor-ID` header is only a hint to look up the right secret; the
HMAC is what authenticates the claim, so a wrong vendor id resolves to
the wrong secret, the signature mismatches, and the request is 401'd.
"""

from __future__ import annotations

import json
import logging

from rest_framework import authentication, exceptions
from standardwebhooks.webhooks import Webhook, WebhookVerificationError

from webhooks.models import VendorCredential

logger = logging.getLogger(__name__)


class StandardWebhooksAuthentication(authentication.BaseAuthentication):
    keyword = "StandardWebhooks"

    def authenticate(self, request) -> tuple[VendorCredential, None] | None:
        msg_id = request.headers.get("webhook-id")
        timestamp = request.headers.get("webhook-timestamp")
        signature = request.headers.get("webhook-signature")
        vendor_id = request.headers.get("x-vendor-id")

        if not (msg_id and timestamp and signature and vendor_id):
            # Returning None lets DRF's IsAuthenticated permission render
            # a generic 401; raising would leak which header was missing.
            return None

        try:
            cred = VendorCredential.objects.get(vendor_id=vendor_id, active=True)
        except VendorCredential.DoesNotExist:
            logger.warning("Auth: unknown vendor_id=%s", vendor_id)
            raise exceptions.AuthenticationFailed("invalid signature") from None

        try:
            wh = Webhook(cred.hmac_secret)
            wh.verify(
                request.body,
                {
                    "webhook-id": msg_id,
                    "webhook-timestamp": timestamp,
                    "webhook-signature": signature,
                },
            )
        except WebhookVerificationError as e:
            # Same opaque message for unknown-vendor and bad-signature so
            # callers can't distinguish "vendor exists" from "wrong key".
            logger.warning("Auth: HMAC verification failed for vendor_id=%s: %s", vendor_id, e)
            raise exceptions.AuthenticationFailed("invalid signature") from e
        except json.JSONDecodeError:
            # standardwebhooks verifies the signature before JSON-decoding
            # the body; a non-JSON body that survived the signature check
            # is a 422 the view will return.
            pass

        return (cred, None)

    def authenticate_header(self, request) -> str:
        return self.keyword
