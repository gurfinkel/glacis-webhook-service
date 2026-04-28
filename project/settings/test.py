"""Test settings — lightweight, in-memory where possible, deterministic."""

from .base import *  # noqa: F401,F403

DEBUG = False

ALLOWED_HOSTS = ["*"]

# Tests must boot without the real key. Activities are mocked at unit level;
# live LLM tests are gated separately via `RUN_LLM_TESTS=1`.
OPENROUTER_API_KEY = "test-key-not-real"

# In-memory SQLite. Tests requiring Postgres-specific features (advisory locks,
# JSON ops) are marked `integration` and run against the real compose stack.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    },
}

# Local-memory cache so unit tests don't need Redis.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "tests",
    },
}

# Integration tests reach a real Redis instance via this URL. Unit tests
# don't import dedup helpers (or mock them at the boundary).
REDIS_URL = "redis://localhost:6379/15"

# Prevent the bridge thread from being spawned during unit tests — they mock
# the Temporal client at the boundary instead.
TEMPORAL_BRIDGE_DISABLED = True
