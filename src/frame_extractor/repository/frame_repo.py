"""Frame table CRUD."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg


class FrameRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def insert(
        self,
        *,
        job_id: UUID,
        video_id: UUID,
        frame_index: int,
        time_sec: float,
        file_path: str,
        width: int,
        height: int,
    ) -> dict[str, Any]:
        row = await self._pool.fetchrow(
            """
            INSERT INTO frame (job_id, video_id, frame_index, time_sec,
                               file_path, width, height)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING *
            """,
            job_id, video_id, frame_index, time_sec, file_path, width, height,
        )
        assert row is not None
        return dict(row)

    async def list_for_job(
        self, job_id: UUID, *, limit: int = 200, offset: int = 0,
    ) -> list[dict[str, Any]]:
        rows = await self._pool.fetch(
            """
            SELECT * FROM frame
             WHERE job_id = $1
             ORDER BY frame_index
             LIMIT $2 OFFSET $3
            """,
            job_id, limit, offset,
        )
        return [dict(r) for r in rows]

    async def count_for_job(self, job_id: UUID) -> int:
        n = await self._pool.fetchval(
            "SELECT count(*) FROM frame WHERE job_id = $1", job_id,
        )
        return int(n or 0)

    async def get(self, frame_id: UUID) -> dict[str, Any] | None:
        row = await self._pool.fetchrow("SELECT * FROM frame WHERE id = $1", frame_id)
        return dict(row) if row else None

    async def delete(self, frame_id: UUID) -> bool:
        result = await self._pool.execute("DELETE FROM frame WHERE id = $1", frame_id)
        return result.endswith(" 1")
