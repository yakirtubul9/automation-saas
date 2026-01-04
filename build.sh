#!/usr/bin/env bash
set -euo pipefail

python -m pip install --upgrade pip
pip install -r requirements.txt

# Static + DB migrations
python manage.py collectstatic --noinput
python manage.py migrate --noinput

# Create/update an admin user for staging/production (idempotent)
# Uses env vars: ADMIN_USERNAME, ADMIN_PASSWORD, (optional) ADMIN_EMAIL
if [[ -n "${ADMIN_USERNAME:-}" && -n "${ADMIN_PASSWORD:-}" ]]; then
  python manage.py shell -c "import os; from django.contrib.auth import get_user_model; U=get_user_model(); u=os.environ.get('ADMIN_USERNAME'); p=os.environ.get('ADMIN_PASSWORD'); e=os.environ.get('ADMIN_EMAIL','admin@example.com'); obj,created=U.objects.get_or_create(username=u, defaults={'email':e}); obj.is_staff=True; obj.is_superuser=True; obj.email=e; obj.set_password(p); obj.save(); print('BOOTSTRAP_ADMIN', 'created' if created else 'updated', u)"
else
  echo "BOOTSTRAP_ADMIN skipped (ADMIN_USERNAME/ADMIN_PASSWORD not set)"
fi
