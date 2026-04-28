#!/usr/bin/env bash
# One-command end-to-end demo.
#
#   1. Verify .env has OPENROUTER_API_KEY (LLM is mandatory, no mock).
#   2. docker compose up -d  + wait for healthchecks.
#   3. Seed demo vendor credential + Django superuser.
#   4. Post every sample in samples/ to /webhook.
#   5. Wait for the workflows to run and poll /events/<key> for each.
#   6. Print URLs (Admin, Temporal Web UI).

set -euo pipefail

cd "$(dirname "$0")/.."

API_URL="http://localhost:8000"
TEMPORAL_UI_URL="http://localhost:8233"

# --- 1. .env + OPENROUTER_API_KEY guard ---------------------------------------

if [[ ! -f .env ]]; then
    echo "Copying .env.example -> .env" >&2
    cp .env.example .env
fi

# shellcheck disable=SC1091
set -a
source .env
set +a

if [[ -z "${OPENROUTER_API_KEY:-}" ]]; then
    cat >&2 <<'MSG'
ERROR: OPENROUTER_API_KEY is empty in .env.

The LLM is a mandatory part of the pipeline (classification + extraction).
Set OPENROUTER_API_KEY in .env to a valid key, then re-run:

    ./scripts/demo.sh

Get a key at https://openrouter.ai (a few cents covers the demo run).
MSG
    exit 1
fi

# --- 2. docker compose up + wait ----------------------------------------------

echo "Bringing up the stack..."
docker compose up -d --build

echo "Waiting for API to become ready..."
for i in {1..60}; do
    if curl -fsS "$API_URL/health/live" > /dev/null 2>&1; then
        echo "API is up."
        break
    fi
    if [[ $i -eq 60 ]]; then
        echo "ERROR: API did not become ready in 60s. Check 'docker compose logs api'." >&2
        exit 1
    fi
    sleep 1
done

# --- 3. Seed vendor + superuser -----------------------------------------------

./scripts/seed_vendor.sh

# --- 4. Post every sample -----------------------------------------------------

echo
echo "Posting samples..."
uv run scripts/send_sample.py --all

# --- 5. Wait + poll status ----------------------------------------------------

echo
echo "Waiting 10s for the workflows to run..."
sleep 10

echo
echo "Event status (one-glance proof the pipeline ran end-to-end):"
docker compose exec -T api python manage.py shell -c "
from collections import Counter
from webhooks.models import WebhookEvent
events = WebhookEvent.objects.all()
print(f'  total events:      {events.count()}')
status_counts = Counter(events.values_list('status', flat=True))
print(f'  by status:         {dict(status_counts)}')
type_counts = Counter(e for e in events.values_list('event_type', flat=True) if e)
print(f'  by event_type:     {dict(type_counts)}')
review_count = events.filter(requires_review=True).count()
print(f'  requires_review:   {review_count}')
"

# --- 6. URLs ------------------------------------------------------------------

cat <<MSG

Demo complete.

  API:           $API_URL/health
  Admin UI:      $API_URL/admin/   (login: admin / admin)
  Temporal UI:   $TEMPORAL_UI_URL

Next:
  ./scripts/send_sample.py shipment_fedex   # post a single sample
  docker compose down -v                     # tear down
MSG
