"""Run a single extraction job: probe → plan → seek/decode → resize → write PNG.

OpenCV calls are blocking — we offload the whole run to a thread so the
asyncio event loop stays responsive (multiple workers process jobs in
parallel via ``asyncio.to_thread``).
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import cv2  # type: ignore[import-untyped]
import structlog

from ..repository import FrameRepository, JobRepository, VideoRepository
from .opencv_backend import open_capture, probe
from .sampling import ExtractionParams

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class JobContext:
    job_id: UUID
    video_id: UUID
    video_path: Path
    out_dir: Path
    params: ExtractionParams
    cancel_event: asyncio.Event


PROGRESS_UPDATE_EVERY = 5   # write progress_pct every N frames


async def run_job(
    *,
    ctx: JobContext,
    video_repo: VideoRepository,
    job_repo: JobRepository,
    frame_repo: FrameRepository,
) -> None:
    """Top-level orchestrator. Catches exceptions and records them to the DB."""
    job_id = ctx.job_id
    try:
        await job_repo.mark_running(job_id)

        meta = await asyncio.to_thread(probe, ctx.video_path)
        plan = await asyncio.to_thread(
            _plan,
            ctx.params, meta.duration_sec, meta.src_fps, meta.frame_count,
        )

        await job_repo.set_total(job_id, len(plan))
        log.info(
            "job_planned",
            job_id=str(job_id), total=len(plan),
            src_fps=meta.src_fps, duration=meta.duration_sec,
        )

        if not plan:
            await job_repo.mark_done(job_id)
            return

        ctx.out_dir.mkdir(parents=True, exist_ok=True)

        # Hand the actual decode/write loop off to a thread, but we need to
        # poll cancel state and write DB rows from the loop. Use a queue:
        # the worker thread produces (idx, time_sec, file_path, w, h) tuples,
        # this coroutine drains them, writes the DB row, and re-checks
        # cancellation between frames.
        queue: asyncio.Queue[tuple[int, int, float, str, int, int] | None] = (
            asyncio.Queue(maxsize=8)
        )

        loop = asyncio.get_running_loop()

        def producer() -> None:
            try:
                for seq, idx, t_sec, file_path, w, h in _decode_and_write(
                    video_path=ctx.video_path,
                    out_dir=ctx.out_dir,
                    plan=plan,
                    resize_w=ctx.params.resize_w,
                    resize_h=ctx.params.resize_h,
                    cancel_event=ctx.cancel_event,
                ):
                    fut = asyncio.run_coroutine_threadsafe(
                        queue.put((seq, idx, t_sec, file_path, w, h)), loop,
                    )
                    fut.result()  # back-pressure
            finally:
                asyncio.run_coroutine_threadsafe(queue.put(None), loop).result()

        producer_task = asyncio.create_task(asyncio.to_thread(producer))

        frames_done = 0
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                seq, frame_idx, t_sec, file_path, w, h = item
                await frame_repo.insert(
                    job_id=job_id,
                    video_id=ctx.video_id,
                    frame_index=seq,
                    time_sec=t_sec,
                    file_path=file_path,
                    width=w,
                    height=h,
                )
                frames_done += 1
                if (
                    frames_done % PROGRESS_UPDATE_EVERY == 0
                    or frames_done == len(plan)
                ):
                    pct = int(frames_done * 100 / max(1, len(plan)))
                    await job_repo.update_progress(job_id, frames_done, pct)
        finally:
            await producer_task

        if ctx.cancel_event.is_set():
            await job_repo.mark_cancelled(job_id)
            log.info("job_cancelled", job_id=str(job_id), frames_done=frames_done)
            return

        await job_repo.mark_done(job_id)
        log.info("job_done", job_id=str(job_id), frames=frames_done)

    except asyncio.CancelledError:
        await job_repo.mark_cancelled(job_id)
        raise
    except Exception as exc:
        log.exception("job_failed", job_id=str(job_id))
        await job_repo.mark_failed(job_id, str(exc))


def _plan(
    params: ExtractionParams,
    duration_sec: float,
    src_fps: float,
    frame_count: int,
) -> list[tuple[int, float]]:
    # Thin wrapper so it can run inside asyncio.to_thread (sampling itself is
    # pure-Python; this just keeps the API consistent).
    from .sampling import plan_indices
    return plan_indices(
        params,
        duration_sec=duration_sec,
        src_fps=src_fps,
        frame_count=frame_count,
    )


def _decode_and_write(
    *,
    video_path: Path,
    out_dir: Path,
    plan: list[tuple[int, float]],
    resize_w: int | None,
    resize_h: int | None,
    cancel_event: asyncio.Event,
):
    """Generator: yield (seq, frame_idx, time_sec, file_path, w, h) per write.

    Runs in a worker thread. Seeks to each planned frame index. On seek
    failure the frame is skipped (some containers don't honor seek for
    non-keyframes).
    """
    cap = open_capture(video_path)
    try:
        seq = 0
        for frame_idx, t_sec in plan:
            if cancel_event.is_set():
                break
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            if resize_w and resize_h:
                frame = cv2.resize(
                    frame, (resize_w, resize_h), interpolation=cv2.INTER_AREA,
                )
            h, w = frame.shape[:2]
            filename = f"{seq:06d}.png"
            file_path = out_dir / filename
            ok_w = cv2.imwrite(
                str(file_path), frame,
                [int(cv2.IMWRITE_PNG_COMPRESSION), 3],
            )
            if not ok_w:
                continue
            yield seq, frame_idx, t_sec, str(file_path), w, h
            seq += 1
    finally:
        cap.release()


def remove_job_output_dir(frames_root: Path, job_id: UUID) -> None:
    """Delete the on-disk output directory for a job (idempotent)."""
    target = frames_root / str(job_id)
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)


def params_from_dict(d: dict[str, Any]) -> ExtractionParams:
    """Decode the jsonb `params` blob to a typed dataclass."""
    return ExtractionParams(
        target_fps=float(d.get("target_fps", 5.0)),
        interval_sec=(float(d["interval_sec"])
                      if d.get("interval_sec") is not None else None),
        resize_w=(int(d["resize_w"]) if d.get("resize_w") is not None else None),
        resize_h=(int(d["resize_h"]) if d.get("resize_h") is not None else None),
        head_skip_sec=float(d.get("head_skip_sec", 0.0)),
        tail_skip_sec=float(d.get("tail_skip_sec", 0.0)),
        sampling_mode=d.get("sampling_mode", "uniform"),
        random_n=(int(d["random_n"]) if d.get("random_n") is not None else None),
        format=d.get("format", "png"),
        seed=(int(d["seed"]) if d.get("seed") is not None else None),
    )
