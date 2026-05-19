"""asyncpg pool helper."""

from __future__ import annotations

import asyncio

import asyncpg
import structlog

from ..config import Settings

log = structlog.get_logger(__name__)


async def create_pool(settings: Settings) -> asyncpg.Pool:
    """Create the pool, retrying on transient connect errors at boot.

    The Postgres container may not finish accepting connections in the exact
    moment compose's healthcheck flips green; retry briefly so the worker
    doesn't crash-loop on first boot.
    """
    last_exc: Exception | None = None
    for attempt in range(10):
        try:
            pool = await asyncpg.create_pool(
                dsn=settings.dsn,
                min_size=settings.db_pool_min,
                max_size=settings.db_pool_max,
            )
            assert pool is not None
            return pool
        except (OSError, asyncpg.PostgresError) as exc:
            last_exc = exc
            log.warning("db_connect_retry", attempt=attempt + 1, err=str(exc))
            await asyncio.sleep(min(2.0, 0.5 * (attempt + 1)))
    raise RuntimeError(f"failed to connect to DB after retries: {last_exc}")
