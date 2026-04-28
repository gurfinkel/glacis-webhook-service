"""Shared Django settings — values common to dev/prod/test.

Environment-driven via `django-environ`. All defaults are declared in the
`environ.Env(...)` constructor so per-call `env(..., default=...)` is never
needed (and pyright is happy: django-environ's stubs flag default-as-kwarg).
"""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    # Django bootstrap
    DEBUG=(bool, False),
    DJANGO_SECRET_KEY=(str, "django-insecure-dev-only-do-not-use-in-prod"),
    ALLOWED_HOSTS=(list, []),
    # Datastores
    POSTGRES_URL=(str, "postgres://postgres:postgres@localhost:5432/glacis"),
    REDIS_URL=(str, "redis://localhost:6379/0"),
    # Webhook service
    OPENROUTER_API_KEY=(str, ""),
    OPENROUTER_MODEL=(str, "anthropic/claude-sonnet-4-20250514"),
    # Temporal
    TEMPORAL_ADDRESS=(str, "localhost:7233"),
    TEMPORAL_NAMESPACE=(str, "default"),
    TEMPORAL_TASK_QUEUE=(str, "webhook-classification"),
    TEMPORAL_START_WORKFLOW_TIMEOUT=(float, 5.0),
    # Max sync callers blocked on the bridge at once. Default sized
    # generously vs typical workers × threads (4 × 8 = 32) so it's a
    # smell test for "Temporal is unhealthy", not a normal-load gate.
    TEMPORAL_BRIDGE_MAX_INFLIGHT=(int, 64),
    # LLM retries
    MAX_RETRIES=(int, 3),
    # Sweeper
    SWEEPER_INTERVAL_SECONDS=(int, 60),
    SWEEPER_GRACE_SECONDS=(int, 300),
    SWEEPER_PROCESSING_GRACE_SECONDS=(int, 900),
    SWEEPER_BATCH_LIMIT=(int, 100),
    # Rate limit
    DEFAULT_RATE_LIMIT=(str, "100/10s"),
    PRE_AUTH_IP_RATE_LIMIT=(str, "200/10s"),
    # Observability
    OTEL_EXPORTER_OTLP_ENDPOINT=(str, ""),
    OTEL_SERVICE_NAME=(str, "glacis-webhook-service"),
)

environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("DJANGO_SECRET_KEY")

DEBUG = env("DEBUG")

ALLOWED_HOSTS = env("ALLOWED_HOSTS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "webhooks",
    "workflows",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Layer-1 IP rate limit runs before DRF auth so bad-signature
    # floods don't burn HMAC verify + DB lookup per request.
    "webhooks.middleware.PreAuthIPRateLimitMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "project.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "project.wsgi.application"

DATABASES = {"default": env.db("POSTGRES_URL")}

# Exposed as a top-level setting so the dedup helper can build a raw
# `redis.Redis` client without parsing CACHES['default']['LOCATION'].
REDIS_URL = env("REDIS_URL")

CACHES = {"default": env.cache("REDIS_URL")}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# 1 MB body cap — vendor webhook payloads should be tiny; reject anything
# larger before it reaches our ingest path.
DATA_UPLOAD_MAX_MEMORY_SIZE = 1_048_576

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "webhooks.auth.StandardWebhooksAuthentication",
    ],
    "EXCEPTION_HANDLER": "webhooks.views.custom_exception_handler",
    "UNAUTHENTICATED_USER": None,
}

RATELIMIT_USE_CACHE = "default"

OPENROUTER_API_KEY = env("OPENROUTER_API_KEY")
OPENROUTER_MODEL = env("OPENROUTER_MODEL")

TEMPORAL_ADDRESS = env("TEMPORAL_ADDRESS")
TEMPORAL_NAMESPACE = env("TEMPORAL_NAMESPACE")
TEMPORAL_TASK_QUEUE = env("TEMPORAL_TASK_QUEUE")
TEMPORAL_START_WORKFLOW_TIMEOUT = env("TEMPORAL_START_WORKFLOW_TIMEOUT")
TEMPORAL_BRIDGE_MAX_INFLIGHT = env("TEMPORAL_BRIDGE_MAX_INFLIGHT")

MAX_RETRIES = env("MAX_RETRIES")

SWEEPER_INTERVAL_SECONDS = env("SWEEPER_INTERVAL_SECONDS")
SWEEPER_GRACE_SECONDS = env("SWEEPER_GRACE_SECONDS")
SWEEPER_PROCESSING_GRACE_SECONDS = env("SWEEPER_PROCESSING_GRACE_SECONDS")
SWEEPER_BATCH_LIMIT = env("SWEEPER_BATCH_LIMIT")

DEFAULT_RATE_LIMIT = env("DEFAULT_RATE_LIMIT")
PRE_AUTH_IP_RATE_LIMIT = env("PRE_AUTH_IP_RATE_LIMIT")

OTEL_EXPORTER_OTLP_ENDPOINT = env("OTEL_EXPORTER_OTLP_ENDPOINT")
OTEL_SERVICE_NAME = env("OTEL_SERVICE_NAME")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "webhooks": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "workflows": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "project": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
