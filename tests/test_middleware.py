"""Pre-auth IP rate-limit middleware.

HMAC verify + DB lookup must NOT be reachable to an unauthenticated
attacker spamming bad signatures. A pre-auth IP bucket in middleware
closes that hole — verify the bucket fires before DRF's auth chain
runs.
"""

from __future__ import annotations

import pytest
from django.test import Client, override_settings


@pytest.fixture(autouse=True)
def _flush_ratelimit_cache():
    """The rate-limit bucket is cached per process. Clear it between
    tests so a previous test's increments don't leak into this one."""
    from django.core.cache import cache
    cache.clear()
    yield
    cache.clear()


@pytest.mark.django_db
class TestPreAuthIPRateLimit:
    """Each request is sent without HMAC headers — the middleware should
    still 429 on flood, *before* DRF auth gets a chance to 401."""

    @override_settings(PRE_AUTH_IP_RATE_LIMIT="3/1m")
    def test_blocks_flood_from_single_ip_before_auth(self):
        """4th request from the same IP is 429'd without ever reaching
        StandardWebhooksAuthentication. Response uses RFC 7807 Problem
        Details with `Retry-After` and `Cache-Control: no-store`."""
        client = Client()

        # 3 unauthenticated requests — each gets 401 (auth fail) but
        # should NOT trip the rate limit.
        for _ in range(3):
            r = client.post(
                "/webhook",
                data="{}",
                content_type="application/json",
                REMOTE_ADDR="10.0.0.1",
            )
            assert r.status_code == 401

        # 4th — middleware throttles before auth.
        r = client.post(
            "/webhook",
            data="{}",
            content_type="application/json",
            REMOTE_ADDR="10.0.0.1",
        )
        assert r.status_code == 429
        body = r.json()
        assert body["type"] == "/errors/rate-limited"
        assert body["status"] == 429
        assert body["title"] == "Too many requests"
        assert "more than 3" in body["detail"].lower()
        assert body["retry_after_seconds"] == 60
        assert r.headers.get("Retry-After") == "60"  # 1m → 60s
        assert r.headers.get("Cache-Control") == "no-store"

    @override_settings(PRE_AUTH_IP_RATE_LIMIT="2/1m")
    def test_per_ip_buckets_independent(self):
        """One IP exhausting its bucket must not affect another IP."""
        client = Client()

        for _ in range(2):
            r = client.post(
                "/webhook", data="{}",
                content_type="application/json",
                REMOTE_ADDR="10.0.0.1",
            )
            assert r.status_code == 401

        # 10.0.0.1 is now over its limit.
        r = client.post(
            "/webhook", data="{}",
            content_type="application/json",
            REMOTE_ADDR="10.0.0.1",
        )
        assert r.status_code == 429

        # 10.0.0.2 has its own bucket — should still get 401.
        r = client.post(
            "/webhook", data="{}",
            content_type="application/json",
            REMOTE_ADDR="10.0.0.2",
        )
        assert r.status_code == 401

    @override_settings(PRE_AUTH_IP_RATE_LIMIT="1/1m")
    def test_health_endpoint_not_throttled(self):
        """Pre-auth IP throttle is scoped to the ingest path only —
        flooding `/health` is a separate concern (cheap endpoint, no HMAC,
        no DB write) and shouldn't share the same bucket."""
        client = Client()

        # Burn the (per-IP) ingest bucket via /webhook.
        r = client.post(
            "/webhook", data="{}",
            content_type="application/json",
            REMOTE_ADDR="10.0.0.5",
        )
        assert r.status_code == 401  # auth fail, not 429

        r = client.post(
            "/webhook", data="{}",
            content_type="application/json",
            REMOTE_ADDR="10.0.0.5",
        )
        assert r.status_code == 429  # ingest bucket exhausted

        # /health from the same IP is NOT throttled.
        for _ in range(5):
            r = client.get("/health/live", REMOTE_ADDR="10.0.0.5")
            assert r.status_code == 200

    @override_settings(PRE_AUTH_IP_RATE_LIMIT="100/1m")
    def test_xforwarded_for_does_not_bypass_throttle(self):
        """An attacker spoofing `X-Forwarded-For` to vary their
        apparent IP must NOT bypass the per-IP bucket. We key on
        REMOTE_ADDR (the real TCP peer) deliberately."""
        client = Client()

        with override_settings(PRE_AUTH_IP_RATE_LIMIT="2/1m"):
            for spoofed in ["1.1.1.1", "2.2.2.2", "3.3.3.3"]:
                r = client.post(
                    "/webhook",
                    data="{}",
                    content_type="application/json",
                    REMOTE_ADDR="10.0.0.1",
                    HTTP_X_FORWARDED_FOR=spoofed,
                )
                # First two: 401 (auth fail). Third: 429.
            assert r.status_code == 429

    @override_settings(PRE_AUTH_IP_RATE_LIMIT="1/1m")
    def test_path_matching_is_exact_not_prefix(self):
        """The throttle scope is the ingest path *exactly*, not any path
        that happens to start with `/webhook`. A `startswith("/webhook")`
        check would silently throttle future routes like `/webhook-info`
        or `/webhooks/v2`. Verify that burning the bucket on `/webhook`
        does NOT 429 a probe on a non-matching path from the same IP."""
        client = Client()

        # Burn the bucket on the real ingest path.
        r = client.post(
            "/webhook", data="{}",
            content_type="application/json",
            REMOTE_ADDR="10.0.0.7",
        )
        assert r.status_code == 401  # auth fail, bucket consumed
        r = client.post(
            "/webhook", data="{}",
            content_type="application/json",
            REMOTE_ADDR="10.0.0.7",
        )
        assert r.status_code == 429  # bucket exhausted

        # A non-matching path from the same IP must NOT inherit the throttle.
        # `/webhookXYZ` doesn't resolve (404), but it has to reach the URL
        # resolver to get the 404 — i.e. the middleware let it through.
        r = client.post(
            "/webhookXYZ", data="{}",
            content_type="application/json",
            REMOTE_ADDR="10.0.0.7",
        )
        assert r.status_code == 404  # NOT 429 — middleware didn't throttle


class TestPeriodToSeconds:
    """Strict parsing — bad rate-limit periods must fail loudly at config
    load instead of producing surprising Retry-After headers."""

    def test_seconds(self):
        from webhooks.middleware import _period_to_seconds

        assert _period_to_seconds("10s") == "10"
        assert _period_to_seconds("1s") == "1"

    def test_minutes_hours_days(self):
        from webhooks.middleware import _period_to_seconds

        assert _period_to_seconds("1m") == "60"
        assert _period_to_seconds("2h") == "7200"
        assert _period_to_seconds("3d") == "259200"

    def test_uppercase_unit_normalized(self):
        from webhooks.middleware import _period_to_seconds

        assert _period_to_seconds("10S") == "10"

    def test_unknown_unit_raises(self):
        from webhooks.middleware import _period_to_seconds

        with pytest.raises(ValueError, match="unknown unit"):
            _period_to_seconds("10x")

    def test_fractional_count_raises(self):
        from webhooks.middleware import _period_to_seconds

        # `0.5h` would have silently produced "0.5" before — now raises.
        with pytest.raises(ValueError, match="positive integer"):
            _period_to_seconds("0.5h")

    def test_zero_count_raises(self):
        from webhooks.middleware import _period_to_seconds

        with pytest.raises(ValueError, match="must be > 0"):
            _period_to_seconds("0s")

    def test_negative_count_raises(self):
        from webhooks.middleware import _period_to_seconds

        with pytest.raises(ValueError, match="must be > 0"):
            _period_to_seconds("-5s")

    def test_too_short_raises(self):
        from webhooks.middleware import _period_to_seconds

        with pytest.raises(ValueError, match="N<unit>"):
            _period_to_seconds("s")
