#!/usr/bin/env bash
# Seed the demo vendor credential and superuser.
#
# Idempotent — running this multiple times leaves the database in the
# same state. Used by scripts/demo.sh.

set -euo pipefail

DEMO_VENDOR_ID="${DEMO_VENDOR_ID:-demo}"
DEMO_HMAC_SECRET="${DEMO_HMAC_SECRET:-whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw}"

ADMIN_USERNAME="${DJANGO_SUPERUSER_USERNAME:-admin}"
ADMIN_EMAIL="${DJANGO_SUPERUSER_EMAIL:-admin@example.com}"
ADMIN_PASSWORD="${DJANGO_SUPERUSER_PASSWORD:-admin}"

echo "Seeding demo vendor credential ($DEMO_VENDOR_ID)..."
docker compose exec -T api python manage.py shell -c "
from webhooks.models import VendorCredential
VendorCredential.objects.update_or_create(
    vendor_id='$DEMO_VENDOR_ID',
    defaults={'hmac_secret': '$DEMO_HMAC_SECRET', 'active': True},
)
print('vendor ok: $DEMO_VENDOR_ID')
"

echo "Ensuring Django superuser ($ADMIN_USERNAME)..."
docker compose exec -T \
    -e DJANGO_SUPERUSER_USERNAME="$ADMIN_USERNAME" \
    -e DJANGO_SUPERUSER_EMAIL="$ADMIN_EMAIL" \
    -e DJANGO_SUPERUSER_PASSWORD="$ADMIN_PASSWORD" \
    api python manage.py shell -c "
import os
from django.contrib.auth import get_user_model
User = get_user_model()
username = os.environ['DJANGO_SUPERUSER_USERNAME']
email = os.environ['DJANGO_SUPERUSER_EMAIL']
password = os.environ['DJANGO_SUPERUSER_PASSWORD']
user, created = User.objects.get_or_create(
    username=username, defaults={'email': email, 'is_staff': True, 'is_superuser': True},
)
user.set_password(password)
user.is_staff = True
user.is_superuser = True
user.email = email
user.save()
print(('created' if created else 'updated') + f' superuser: {username}')
"
