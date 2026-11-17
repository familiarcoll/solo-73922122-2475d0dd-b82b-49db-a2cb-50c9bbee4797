"""Application configuration."""
from __future__ import annotations

import os


class Settings:
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg2://research:research@localhost:5432/researchdb",
    )
    # Maximum number of entries accepted in one transaction Bundle.
    MAX_BUNDLE_ENTRIES: int = int(os.getenv("MAX_BUNDLE_ENTRIES", "20"))
    # A pending idempotency record whose heartbeat is older than this is treated
    # as a crashed request and may be re-driven (seconds). The threshold is
    # deliberately kept at its original value: a *live* request keeps refreshing
    # its heartbeat, so slowness alone never triggers recovery.
    IDEMPOTENCY_STALE_SECONDS: int = int(
        os.getenv("IDEMPOTENCY_STALE_SECONDS", "60")
    )
    # How often an in-flight request refreshes its liveness heartbeat. Must be
    # well below IDEMPOTENCY_STALE_SECONDS so healthy requests are never mistaken
    # for crashed ones.
    IDEMPOTENCY_HEARTBEAT_SECONDS: int = int(
        os.getenv("IDEMPOTENCY_HEARTBEAT_SECONDS", "20")
    )


settings = Settings()
