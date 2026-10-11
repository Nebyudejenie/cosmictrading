# Backup/restore tool image: the same PostgreSQL 17 build as the server (pg_dump must not be
# older than the server) plus age for public-key encryption (ADR-019).
FROM postgres:17-alpine@sha256:b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24
RUN apk add --no-cache age
COPY cosmic-backup.sh /usr/local/bin/cosmic-backup
COPY drill-check.sh /usr/local/bin/cosmic-drill-check
RUN chmod 0555 /usr/local/bin/cosmic-backup /usr/local/bin/cosmic-drill-check
