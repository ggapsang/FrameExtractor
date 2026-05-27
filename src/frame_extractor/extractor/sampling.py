"""Sampling policy — convert request params to a list of (frame_index, time_sec).

Modes
-----
uniform : pick frames at `target_fps` (or every `interval_sec`) over the
          [head_skip_sec, duration - tail_skip_sec] window.
random_n: pick `random_n` indices uniformly at random from the same window.

`resize_w`/`resize_h` and `format` are not used here — they are applied
by `worker.run_job` after decoding.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Literal


SamplingMode = Literal["uniform", "random_n"]


@dataclass(slots=True)
class ExtractionParams:
    target_fps: float = 5.0
    interval_sec: float | None = None
    resize_w: int | None = None
    resize_h: int | None = None
    head_skip_sec: float = 0.0
    tail_skip_sec: float = 0.0
    sampling_mode: SamplingMode = "uniform"
    random_n: int | None = None
    format: str = "png"
    seed: int | None = None   # only used in random_n

    def validate(self) -> None:
        if self.target_fps <= 0 and self.interval_sec is None:
            raise ValueError("target_fps must be > 0 (or set interval_sec)")
        if self.interval_sec is not None and self.interval_sec <= 0:
            raise ValueError("interval_sec must be > 0")
        if self.head_skip_sec < 0 or self.tail_skip_sec < 0:
            raise ValueError("head_skip_sec/tail_skip_sec must be >= 0")
        if self.sampling_mode == "random_n":
            if not self.random_n or self.random_n <= 0:
                raise ValueError("random_n must be a positive integer in random_n mode")
        if self.resize_w is not None and self.resize_w <= 0:
            raise ValueError("resize_w must be > 0")
        if self.resize_h is not None and self.resize_h <= 0:
            raise ValueError("resize_h must be > 0")
        if self.format != "png":
            raise ValueError(f"unsupported format: {self.format}")


def _window_indices(
    *,
    duration_sec: float,
    src_fps: float,
    frame_count: int,
    head_skip_sec: float,
    tail_skip_sec: float,
) -> tuple[int, int]:
    """Return [start_idx, end_idx_exclusive) clipped to the valid range."""
    start = int(round(head_skip_sec * src_fps))
    end = int(round((duration_sec - tail_skip_sec) * src_fps))
    start = max(0, min(start, frame_count))
    end = max(start, min(end, frame_count))
    return start, end


def plan_indices(
    params: ExtractionParams,
    *,
    duration_sec: float,
    src_fps: float,
    frame_count: int,
) -> list[tuple[int, float]]:
    """Return a sorted list of (frame_index, time_sec) to extract.

    Head/tail fallback:
      * If the source video is shorter than 1 second, OR
      * If head_skip_sec + tail_skip_sec would leave zero/negative window,
    head/tail trimming is silently ignored and the whole clip is used.
    Other params (fps, interval, random_n, resize) still apply.
    """
    params.validate()

    # Fallback: head/tail are dropped when they make no sense for this clip.
    effective_head = params.head_skip_sec
    effective_tail = params.tail_skip_sec
    if (
        duration_sec < 1.0
        or (effective_head + effective_tail) >= duration_sec
    ):
        effective_head = 0.0
        effective_tail = 0.0

    start_idx, end_idx = _window_indices(
        duration_sec=duration_sec,
        src_fps=src_fps,
        frame_count=frame_count,
        head_skip_sec=effective_head,
        tail_skip_sec=effective_tail,
    )
    if end_idx <= start_idx:
        return []

    if params.sampling_mode == "uniform":
        # Step in frame units. interval_sec overrides target_fps.
        if params.interval_sec is not None:
            step = max(1, int(round(params.interval_sec * src_fps)))
        else:
            # downsample only — never upsample beyond src_fps
            target = min(params.target_fps, src_fps)
            step = max(1, int(round(src_fps / target)))
        idxs = list(range(start_idx, end_idx, step))

    elif params.sampling_mode == "random_n":
        n = min(params.random_n or 0, end_idx - start_idx)
        rng = random.Random(params.seed)
        idxs = sorted(rng.sample(range(start_idx, end_idx), n))

    else:  # pragma: no cover — validate() rejects others
        raise ValueError(f"unknown sampling_mode: {params.sampling_mode}")

    return [(i, i / src_fps) for i in idxs]
