"""URL path constants — minimal, dependency-free.

Lives in its own module so middleware (which loads at settings-init
time) can share constants with `webhooks/urls.py` without transitively
importing the view stack.
"""

INGEST_PATH = "/webhook"
