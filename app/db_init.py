"""Create database tables. Runs on container start and for local setup."""
from __future__ import annotations

import time

from sqlalchemy import text

from app.database import engine, init_db


def wait_for_database(max_attempts: int = 30, delay: float = 1.0) -> None:
    for attempt in range(1, max_attempts + 1):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except Exception as exc:  # pragma: no cover - startup path
            if attempt == max_attempts:
                raise
            print(f"waiting for database... ({attempt}/{max_attempts}) {exc}")
            time.sleep(delay)


if __name__ == "__main__":
    wait_for_database()
    init_db()
    print("database schema ready")
