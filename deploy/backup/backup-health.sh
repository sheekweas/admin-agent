#!/bin/sh
# Healthy while the last successful backup is younger than BACKUP_MAX_AGE_HOURS (or the container is newer).
max=$(( ${BACKUP_MAX_AGE_HOURS:-26} * 3600 ))
now=$(date -u +%s)
ref=/backups/.last_success
[ -f "$ref" ] || ref=/tmp/backup-started
[ -f "$ref" ] || exit 1
age=$(( now - $(stat -c %Y "$ref") ))
[ "$age" -lt "$max" ]
