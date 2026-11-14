#!/bin/sh
# Restore a dump into a NEW database and verify it. Never overwrites an existing non-empty database.
#
#   restore.sh <file.dump|file.dump.enc|s3://bucket/key> <target_db> [--compare <source_db>]
#
# Steps: (download) -> sha256 check if a .sha256 sits next to the file -> decrypt (BACKUP_PASSPHRASE)
# -> pg_restore --list -> CREATE DATABASE -> pg_restore -j4 --exit-on-error -> row counts
# (--compare: row counts must equal those of <source_db>, exit 3 otherwise).
# Switching production to the restored database is a separate, manual step (docs/OPERATIONS.md).
set -eu
[ $# -ge 2 ] || { sed -n '2,10p' "$0"; exit 2; }
src=$1; target=$2; compare=""
[ "${3:-}" = "--compare" ] && compare=${4:?--compare needs a database name}
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
t0=$(date +%s)

case "$src" in
  s3://*)
    set -- --only-show-errors
    [ -n "${S3_ENDPOINT_URL:-}" ] && set -- "$@" --endpoint-url "$S3_ENDPOINT_URL"
    aws s3 cp "$@" "$src" "$work/"
    aws s3 cp "$@" "$src.sha256" "$work/" 2>/dev/null || true
    file="$work/$(basename "$src")" ;;
  *) file=$src ;;
esac

if [ -f "$file.sha256" ]; then
  (cd "$(dirname "$file")" && sha256sum -c "$(basename "$file").sha256" >/dev/null) \
    || { echo "restore: checksum mismatch for $file" >&2; exit 1; }
  echo "restore: checksum OK"
fi
case "$file" in
  *.enc)
    : "${BACKUP_PASSPHRASE:?BACKUP_PASSPHRASE is required for an encrypted dump}"
    openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass env:BACKUP_PASSPHRASE -in "$file" -out "$work/db.dump"
    file="$work/db.dump" ;;
esac
pg_restore --list "$file" > /dev/null

exists=$(psql -X -At -d postgres -c "SELECT 1 FROM pg_database WHERE datname = '$target'")
if [ "$exists" = "1" ]; then
  n=$(psql -X -At -d "$target" -c "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
  [ "$n" = "0" ] || { echo "restore: database $target exists and is not empty - refusing" >&2; exit 1; }
else
  psql -X -q -d postgres -c "CREATE DATABASE \"$target\""
fi
t1=$(date +%s)
pg_restore --no-owner --no-privileges --exit-on-error --jobs=4 -d "$target" "$file"
t2=$(date +%s)
rowcounts.sh "$target" > "$work/restored.txt"
echo "restore: OK into $target - pg_restore $(( t2 - t1 ))s, total $(( t2 - t0 ))s, tables $(wc -l < "$work/restored.txt"), rows $(awk '{s+=$2} END {print s}' "$work/restored.txt")"

if [ -n "$compare" ]; then
  rowcounts.sh "$compare" > "$work/source.txt"
  if diff "$work/source.txt" "$work/restored.txt" > "$work/diff.txt"; then
    echo "restore: row counts identical to $compare ($(wc -l < "$work/source.txt") tables)"
  else
    echo "restore: ROW COUNTS DIFFER from $compare:" >&2; cat "$work/diff.txt" >&2; exit 3
  fi
fi
