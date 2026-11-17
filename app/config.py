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
    # A pending idempotency record older than this is treated as a crashed
    # request and may be re-driven (seconds).
    IDEMPOTENCY_STALE_SECONDS: int = int(
        os.getenv("IDEMPOTENCY_STALE_SECONDS", "60")
    )


settings = Settings()
