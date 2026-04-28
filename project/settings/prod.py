"""Production settings — strict defaults, mandatory secrets, OTel exporters live."""

from .base import *  # noqa: F401,F403
from .base import env

DEBUG = False

# Required in prod — fails fast at import if missing
SECRET_KEY = env("DJANGO_SECRET_KEY")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

# Required: production must have a live OpenRouter key. The default-empty fallback
# in base.py exists only so dev/test can boot without one.
if not env("OPENROUTER_API_KEY"):
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured("OPENROUTER_API_KEY is required in production")

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 365  # 1 year
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
