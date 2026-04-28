"""Webhooks app `ready()` checks — rate-limit setting validation."""

from __future__ import annotations

import pytest
from django.test import override_settings

from webhooks.apps import WebhooksConfig


class TestRateLimitSettingValidation:
    def test_default_settings_pass(self):
        """The values shipped in `base.py` must validate, or every boot fails."""
        WebhooksConfig._validate_rate_limit_settings()

    @override_settings(PRE_AUTH_IP_RATE_LIMIT="200/10")  # missing unit
    def test_malformed_period_unit_raises(self):
        with pytest.raises(ValueError, match="unknown unit"):
            WebhooksConfig._validate_rate_limit_settings()

    @override_settings(DEFAULT_RATE_LIMIT="oops")  # missing slash
    def test_missing_slash_raises(self):
        with pytest.raises(ValueError, match="COUNT/PERIOD"):
            WebhooksConfig._validate_rate_limit_settings()

    @override_settings(DEFAULT_RATE_LIMIT="abc/10s")  # non-numeric count is fine for the period parser
    def test_non_numeric_count_accepted_at_period_layer(self):
        """The period validator only checks the period suffix — a bad
        count like `abc/10s` would crash django-ratelimit later, not
        here. We don't catch every misuse, just the period typo class."""
        WebhooksConfig._validate_rate_limit_settings()

    @override_settings(PRE_AUTH_IP_RATE_LIMIT="100/0s")  # zero count
    def test_zero_count_period_raises(self):
        with pytest.raises(ValueError, match="must be > 0"):
            WebhooksConfig._validate_rate_limit_settings()

    @override_settings(PRE_AUTH_IP_RATE_LIMIT="")  # empty
    def test_empty_setting_skipped(self):
        """Empty values are tolerated (operator-disabled limit) — only
        non-empty strings are validated."""
        WebhooksConfig._validate_rate_limit_settings()
