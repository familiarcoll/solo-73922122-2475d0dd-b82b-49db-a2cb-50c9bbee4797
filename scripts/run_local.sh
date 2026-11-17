#!/usr/bin/env bash
# Convenience launcher for local (non-Docker) development.
# Requires a reachable PostgreSQL in DATABASE_URL (default points at compose db).
set -euo pipefail

export DATABASE_URL="${DATABASE_URL:-postgresql+psycopg2://research:research@localhost:5432/researchdb}"
export PYTHONPATH="$(cd "$(dirname "$0")" && pwd):${PYTHONPATH:-}"

python -m app.db_init
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
