"""asyncio Job queue + worker pool + per-job cancel events."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID

import structlog

from .config import Settings
from .extractor.worker import JobContext, params_from_dict, run_job
from .repository import FrameRepository, JobRepository, VideoRepository

log = structlog.get_logger(__name__)


class JobQueue:
    def __init__(
        self,
        *,
        settings: Settings,
        video_repo: VideoRepository,
        job_repo: JobRepository,
        frame_repo: FrameRepository,
    ) -> None:
        self._settings = settings
        self._video_repo = video_repo
        self._job_repo = job_repo
        self._frame_repo = frame_repo

        self._queue: asyncio.Queue[UUID] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        # Per-job cancel events. We hold the event for the entire lifetime of
        # the job (queued + running) so admin can set it any time.
        self._cancel_events: dict[UUID, asyncio.Event] = {}

    # --- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        for i in range(self._settings.workers):
            t = asyncio.create_task(self._worker_loop(i), name=f"fx-worker-{i}")
            self._workers.append(t)
        log.info("workers_started", count=len(self._workers))

    async def stop(self) -> None:
        for t in self._workers:
            t.cancel()
        for t in self._workers:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._workers.clear()

    # --- public API -------------------------------------------------------

    async def enqueue(self, job_id: UUID) -> None:
        """Push a job onto the queue. Idempotent w.r.t. the cancel-event dict."""
        self._cancel_events.setdefault(job_id, asyncio.Event())
        await self._queue.put(job_id)

    def request_cancel(self, job_id: UUID) -> bool:
        """Signal the running/queued job to cancel. Returns True if a cancel
        flag was set (False means we have no record of that job — likely it
        already finished)."""
        ev = self._cancel_events.get(job_id)
        if ev is None:
            return False
        ev.set()
        return True

    # --- worker loop ------------------------------------------------------

    async def _worker_loop(self, idx: int) -> None:
        log.info("worker_started", worker=idx)
        try:
            while True:
                job_id = await self._queue.get()
                try:
                    await self._handle_one(job_id)
                except Exception:
                    log.exception("worker_unhandled", job_id=str(job_id))
                finally:
                    # Drop the cancel event once the job is over (whatever its
                    # outcome). A subsequent re-enqueue (e.g. after a reset)
                    # will create a fresh event.
                    self._cancel_events.pop(job_id, None)
                    self._queue.task_done()
        except asyncio.CancelledError:
            log.info("worker_stopped", worker=idx)
            raise

    async def _handle_one(self, job_id: UUID) -> None:
        row = await self._job_repo.get(job_id)
        if row is None:
            log.warning("job_missing", job_id=str(job_id))
            return

        # If admin cancelled before we popped it, mark and skip.
        cancel_event = self._cancel_events.setdefault(job_id, asyncio.Event())
        if cancel_event.is_set():
            await self._job_repo.mark_cancelled(job_id)
            return

        video = await self._video_repo.get(row["video_id"])
        if video is None:
            await self._job_repo.mark_failed(job_id, "video row missing")
            return

        try:
            params = params_from_dict(row["params"])
        except Exception as exc:
            await self._job_repo.mark_failed(job_id, f"invalid params: {exc}")
            return

        # Per-video folder, NOT per-job. All extractions of the same video
        # share <frames_dir>/<video_stem>/ — the worker clears stale PNGs
        # on each run so only the most recent extraction's output remains.
        video_stem = Path(video["filename"]).stem or "frame"
        out_dir = Path(self._settings.frames_dir) / video_stem
        ctx = JobContext(
            job_id=job_id,
            video_id=row["video_id"],
            video_path=Path(video["file_path"]),
            video_filename=video["filename"],
            out_dir=out_dir,
            params=params,
            cancel_event=cancel_event,
        )

        await run_job(
            ctx=ctx,
            video_repo=self._video_repo,
            job_repo=self._job_repo,
            frame_repo=self._frame_repo,
        )
