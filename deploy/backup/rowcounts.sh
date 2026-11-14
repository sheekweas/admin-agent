#!/bin/sh
# Exact row count of every table in the public schema: "<table> <count>" lines, sorted.
# Usage: rowcounts.sh <database>
set -eu
psql -X -q -At -d "$1" -v ON_ERROR_STOP=1 <<'SQL'
SELECT format('SELECT %L || '' '' || count(*) FROM public.%I', tablename, tablename)
FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename
\gexec
SQL
