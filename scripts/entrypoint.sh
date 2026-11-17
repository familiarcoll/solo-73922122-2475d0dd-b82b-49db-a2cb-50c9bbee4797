#!/bin/sh
set -e

# Wait for PostgreSQL to accept connections, then create tables.
python -m app.db_init

exec "$@"
