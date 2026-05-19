"""Job table CRUD + status transitions."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import asyncpg


class JobRepository:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def create(
        self,
        *,
        video_id: UUID,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        row = await self._pool.fetchrow(
            """
            INSERT INTO job (video_id, params, status)
            VALUES ($1, $2::jsonb, 'queued')
            RETURNING *
            """,
            video_id, json.dumps(params),
        )
        assert row is not None
        return self._decode(row)

    async def get(self, job_id: UUID) -> dict[str, Any] | None:
        row = await self._pool.fetchrow("SELECT * FROM job WHERE id = $1", job_id)
        return self._decode(row) if row else None

    async def list_for_video(self, video_id: UUID) -> list[dict[str, Any]]:
        rows = await self._pool.fetch(
            "SELECT * FROM job WHERE video_id = $1 ORDER BY created_at DESC",
            video_id,
        )
        return [self._decode(r) for r in rows]

    async def list_queued_ids(self) -> list[UUID]:
        rows = await self._pool.fetch(
            "SELECT id FROM job WHERE status = 'queued' ORDER BY created_at"
        )
        return [r["id"] for r in rows]

    async def reset_orphaned_running(self) -> int:
        """On boot, flip 'running' jobs (left over from crash/restart) back to
        'queued' so workers re-pick them up. Returns affected row count."""
        result = await self._pool.execute(
            """
            UPDATE job
               SET status = 'queued',
                   started_at = NULL,
                   progress_pct = 0,
                   frames_done = 0
             WHERE status = 'running'
            """
        )
        # asyncpg returns "UPDATE n"
        try:
            return int(result.split()[-1])
        except (ValueError, IndexError):
            return 0

    async def mark_running(self, job_id: UUID) -> None:
        await self._pool.execute(
            """
            UPDATE job
               SET status = 'running',
                   started_at = NOW(),
                   error_message = NULL
             WHERE id = $1
            """,
            job_id,
        )

    async def set_total(self, job_id: UUID, total: int) -> None:
        await self._pool.execute(
            "UPDATE job SET frames_total = $2 WHERE id = $1",
            job_id, total,
        )

    async def update_progress(
        self, job_id: UUID, frames_done: int, progress_pct: int,
    ) -> None:
        await self._pool.execute(
            """
            UPDATE job
               SET frames_done = $2,
                   progress_pct = $3
             WHERE id = $1
            """,
            job_id, frames_done, progress_pct,
        )

    async def mark_done(self, job_id: UUID) -> None:
        await self._pool.execute(
            """
            UPDATE job
               SET status = 'done',
                   progress_pct = 100,
                   finished_at = NOW()
             WHERE id = $1
            """,
            job_id,
        )

    async def mark_failed(self, job_id: UUID, error_message: str) -> None:
        await self._pool.execute(
            """
            UPDATE job
               SET status = 'failed',
                   finished_at = NOW(),
                   error_message = $2
             WHERE id = $1
            """,
            job_id, error_message,
        )

    async def mark_cancelled(self, job_id: UUID) -> None:
        await self._pool.execute(
            """
            UPDATE job
               SET status = 'cancelled',
                   finished_at = NOW()
             WHERE id = $1
            """,
            job_id,
        )

    @staticmethod
    def _decode(row: asyncpg.Record) -> dict[str, Any]:
        d = dict(row)
        # jsonb comes back as str in asyncpg unless a codec is set; normalize.
        params = d.get("params")
        if isinstance(params, str):
            try:
                d["params"] = json.loads(params)
            except json.JSONDecodeError:
                pass
        return d
