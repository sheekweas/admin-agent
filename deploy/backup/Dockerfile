# Backup sidecar: nightly pg_dump -Fc with 7 daily / 4 weekly retention, optional encryption and S3 upload.
# Runs as the unprivileged "postgres" user (uid 70) of the official image; no cron daemon needed.
FROM postgres:16-alpine
RUN apk add --no-cache aws-cli openssl coreutils \
    && mkdir -p /backups && chown postgres:postgres /backups
COPY --chmod=0755 backup.sh restore.sh backup-loop.sh backup-health.sh rowcounts.sh /usr/local/bin/
USER postgres
WORKDIR /backups
ENTRYPOINT ["/usr/local/bin/backup-loop.sh"]
