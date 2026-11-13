#!/usr/bin/env bash
# Creates .env from .env.example with random secrets. Never overwrites.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  echo ".env already exists - leaving it untouched."
  exit 0
fi

secret() { openssl rand -hex "${1:-24}"; }

sed \
  -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(secret)|" \
  -e "s|^APP_DB_PASSWORD=.*|APP_DB_PASSWORD=$(secret)|" \
  -e "s|^JWT_SECRET=.*|JWT_SECRET=$(secret 32)|" \
  -e "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=$(secret 8)|" \
  -e "s|^JELLYFIN_ADMIN_PASSWORD=.*|JELLYFIN_ADMIN_PASSWORD=$(secret 12)|" \
  .env.example > .env
chmod 600 .env

# Secrets are never printed; the admin login is ADMIN_USERNAME / ADMIN_PASSWORD in .env.
echo "Created .env with random secrets (admin login: ADMIN_USERNAME / ADMIN_PASSWORD in .env)."
