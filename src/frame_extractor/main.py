"""FrameExtractor entrypoint — uvicorn + DB pool + background worker pool."""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

import structlog
import uvicorn

from .app import build_app
from .config import Settings
from .job_queue import JobQueue
from .logging_config import configure_logging
from .repository import (
    FrameRepository,
    JobRepository,
    VideoRepository,
    create_pool,
)


async def run() -> None:
    settings = Settings()
    configure_logging(settings.log_level, settings.log_format)
    log = structlog.get_logger(__name__)

    # Ensure mount points exist (bind mounts come in pre-created on host, but
    # guard against `docker compose up` on a fresh checkout).
    Path(settings.media_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.frames_dir).mkdir(parents=True, exist_ok=True)

    log.info(
        "starting_frame_extractor",
        http_host=settings.http_host, http_port=settings.http_port,
        db_host=settings.db_host, db_name=settings.db_name,
        workers=settings.workers,
        media_dir=settings.media_dir, frames_dir=settings.frames_dir,
    )

    pool = await create_pool(settings)
    log.info("db_pool_ready")

    video_repo = VideoRepository(pool)
    job_repo = JobRepository(pool)
    frame_repo = FrameRepository(pool)

    # On boot, any 'running' jobs left from a previous container instance are
    # stale — flip them back to 'queued' so the worker picks them up again.
    requeued = await job_repo.reset_orphaned_running()
    if requeued:
        log.info("requeued_orphaned_running_jobs", count=requeued)

    job_queue = JobQueue(
        settings=settings,
        video_repo=video_repo,
        job_repo=job_repo,
        frame_repo=frame_repo,
    )
    await job_queue.start()

    # Re-enqueue any pending (queued) jobs left in the DB
    pending = await job_repo.list_queued_ids()
    for jid in pending:
        await job_queue.enqueue(jid)
    if pending:
        log.info("reenqueued_pending_jobs", count=len(pending))

    app = build_app(
        settings=settings,
        pool=pool,
        video_repo=video_repo,
        job_repo=job_repo,
        frame_repo=frame_repo,
        job_queue=job_queue,
    )

    config = uvicorn.Config(
        app,
        host=settings.http_host,
        port=settings.http_port,
        log_level="warning",
        access_log=False,
        lifespan="off",
    )
    server = uvicorn.Server(config)

    stop_event = asyncio.Event()

    def _request_stop() -> None:
        log.info("shutdown_requested")
        stop_event.set()
        server.should_exit = True

    if sys.platform != "win32":
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _request_stop)
            except NotImplementedError:
                pass

    try:
        await server.serve()
    finally:
        await job_queue.stop()
        await pool.close()
        log.info("frame_extractor_stopped")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
