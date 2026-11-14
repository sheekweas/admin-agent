#!/bin/sh
# Scheduler: one backup.sh run per day at BACKUP_TIME_UTC (HH:MM), optionally one right after start.
set -eu
: "${BACKUP_TIME_UTC:=21:30}"
: "${BACKUP_ON_START:=true}"
date -u +%s > /tmp/backup-started

run() { /usr/local/bin/backup.sh || echo "backup: FAILED (exit $?) - will retry at the next slot" >&2; }

[ "$BACKUP_ON_START" = "true" ] && run

hh=${BACKUP_TIME_UTC%:*}; mm=${BACKUP_TIME_UTC#*:}
slot=$(( ${hh#0} * 3600 + ${mm#0} * 60 ))
while true; do
  now=$(date -u +%s)
  next=$(( now - now % 86400 + slot ))
  [ "$next" -le "$now" ] && next=$(( next + 86400 ))
  echo "backup: next run at $(date -u -d "@$next" '+%Y-%m-%dT%H:%M:%SZ')"
  sleep $(( next - now ))
  run
done
