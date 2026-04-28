from django.apps import AppConfig


class WebhooksConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "webhooks"

    def ready(self) -> None:
        # OTel runs once per Django process. With `preload_app=False`,
        # this happens after fork so each worker gets its own SDK state.
        from project.otel import init_otel

        init_otel()
        self._validate_rate_limit_settings()

    @staticmethod
    def _validate_rate_limit_settings() -> None:
        """Rate-limit period strings are otherwise parsed at 429-render
        time; a typo in the env would only surface during a real flood.
        Validate at app load so a bad value crashes boot."""
        from django.conf import settings

        from webhooks.middleware import _period_to_seconds

        for setting_name in ("PRE_AUTH_IP_RATE_LIMIT", "DEFAULT_RATE_LIMIT"):
            value = getattr(settings, setting_name, None)
            if not value:
                continue
            if "/" not in value:
                raise ValueError(
                    f"{setting_name}={value!r} must be of the form 'COUNT/PERIOD' "
                    f"(e.g. '100/10s')"
                )
            _, period = value.split("/", 1)
            _period_to_seconds(period)
