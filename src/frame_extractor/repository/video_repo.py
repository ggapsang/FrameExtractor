"""Video table CRUD."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import asyncpg


class VideoRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def insert(
        self,
        *,
        video_id: UUID,
        filename: str,
        file_path: str,
        container_ext: str,
        duration_sec: float | None,
        src_fps: float | None,
        width: int | None,
        height: int | None,
        size_bytes: int | None,
    ) -> dict[str, Any]:
        row = await self._pool.fetchrow(
            """
            INSERT INTO video
                (id, filename, file_path, container_ext, duration_sec,
                 src_fps, width, height, size_bytes)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING *
            """,
            video_id, filename, file_path, container_ext, duration_sec,
            src_fps, width, height, size_bytes,
        )
        assert row is not None
        return dict(row)

    async def get(self, video_id: UUID) -> dict[str, Any] | None:
        row = await self._pool.fetchrow("SELECT * FROM video WHERE id = $1", video_id)
        return dict(row) if row else None

    async def list_all(self) -> list[dict[str, Any]]:
        rows = await self._pool.fetch(
            "SELECT * FROM video ORDER BY uploaded_at DESC"
        )
        return [dict(r) for r in rows]

    async def delete(self, video_id: UUID) -> bool:
        result = await self._pool.execute("DELETE FROM video WHERE id = $1", video_id)
        # asyncpg returns "DELETE n"
        return result.endswith(" 1")
