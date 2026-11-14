#!/bin/sh
# One backup: pg_dump -Fc -> integrity check -> optional AES-256 encryption -> sha256 -> retention -> optional S3.
# Connection: standard libpq env (PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE).
set -eu
: "${BACKUP_DIR:=/backups}"
: "${BACKUP_KEEP_DAILY:=7}"
: "${BACKUP_KEEP_WEEKLY:=4}"
: "${BACKUP_WEEKLY_DAY:=7}"
: "${PGDATABASE:=crm}"

stamp=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$BACKUP_DIR/daily" "$BACKUP_DIR/weekly"
tmp="$BACKUP_DIR/.in-progress-$stamp.dump"
trap 'rm -f "$tmp" "$tmp.enc"' EXIT

t0=$(date +%s)
pg_dump --format=custom --compress=6 --no-owner --no-privileges --file="$tmp" "$PGDATABASE"
pg_restore --list "$tmp" > /dev/null          # archive is readable / complete
tables=$(pg_restore --list "$tmp" | grep -c ' TABLE DATA ' || true)

name="${PGDATABASE}_${stamp}.dump"
if [ -n "${BACKUP_PASSPHRASE:-}" ]; then
  openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass env:BACKUP_PASSPHRASE -in "$tmp" -out "$tmp.enc"
  rm -f "$tmp"; tmp="$tmp.enc"; name="$name.enc"
fi
final="$BACKUP_DIR/daily/$name"
mv "$tmp" "$final"
(cd "$BACKUP_DIR/daily" && sha256sum "$name" > "$name.sha256")

if [ "$(date -u +%u)" = "$BACKUP_WEEKLY_DAY" ]; then
  cp "$final" "$BACKUP_DIR/weekly/$name" && cp "$final.sha256" "$BACKUP_DIR/weekly/$name.sha256"
fi

prune() { # dir keep
  ls -1t "$1" 2>/dev/null | grep -E '\.dump(\.enc)?$' | tail -n +"$(( $2 + 1 ))" | while read -r f; do
    rm -f "$1/$f" "$1/$f.sha256"
  done
}
prune "$BACKUP_DIR/daily" "$BACKUP_KEEP_DAILY"
prune "$BACKUP_DIR/weekly" "$BACKUP_KEEP_WEEKLY"

if [ -n "${S3_BUCKET:-}" ]; then
  dest="s3://$S3_BUCKET/${S3_PREFIX:-ai-crm}/daily/"
  set -- --only-show-errors
  [ -n "${S3_ENDPOINT_URL:-}" ] && set -- "$@" --endpoint-url "$S3_ENDPOINT_URL"
  aws s3 cp "$@" "$final" "$dest"
  aws s3 cp "$@" "$final.sha256" "$dest"
  # remote retention: configure a lifecycle rule on the bucket (docs/OPERATIONS.md)
fi

size=$(du -h "$final" | cut -f1)
echo "backup: OK $final size=$size tables=$tables duration=$(( $(date +%s) - t0 ))s s3=${S3_BUCKET:+yes}"
date -u +%Y-%m-%dT%H:%M:%SZ > "$BACKUP_DIR/.last_success"
