#!/usr/bin/env sh
# Prints strong random values for deploy/.env.production (copy the lines you need).
# Values are hex: safe inside URLs (DATABASE_URL / REDIS_URL) and shell files. Nothing is written to disk.
set -eu

rand_hex() { # $1 = bytes
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex "$1"
  else
    head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'
  fi
}

echo "JWT_SECRET=$(rand_hex 32)"
echo "SECRETS_ENCRYPTION_KEY=$(rand_hex 32)"
echo "TELEGRAM_WEBHOOK_SECRET=$(rand_hex 24)"
echo "WHATSAPP_VERIFY_TOKEN=$(rand_hex 16)"
echo "METRICS_TOKEN=$(rand_hex 24)"
echo "POSTGRES_PASSWORD=$(rand_hex 24)"
echo "REDIS_PASSWORD=$(rand_hex 24)"
echo "BACKUP_PASSPHRASE=$(rand_hex 32)"
echo "# WHATSAPP_APP_SECRET comes from Meta (App settings -> Basic), it is not generated here." >&2
