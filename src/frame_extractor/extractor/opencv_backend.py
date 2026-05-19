"""OpenCV-backed video probe + capture.

Lifted from MockImages/src/mock_images/frame_extractor.py and adapted for
seek-based extraction (we don't iterate the whole stream — we set
CAP_PROP_POS_FRAMES on each target index).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2  # type: ignore[import-untyped]


@dataclass(slots=True)
class VideoMeta:
    duration_sec: float
    src_fps: float
    width: int
    height: int
    frame_count: int


def open_capture(path: Path) -> "cv2.VideoCapture":
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"failed to open video: {path}")
    return cap


def probe(path: Path) -> VideoMeta:
    """Read duration / fps / resolution / frame count without decoding."""
    cap = open_capture(path)
    try:
        fps = cap.get(cv2.CAP_PROP_FPS)
        if not fps or fps <= 0:
            fps = 30.0
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        duration = frame_count / fps if fps > 0 else 0.0
        return VideoMeta(
            duration_sec=duration,
            src_fps=float(fps),
            width=width,
            height=height,
            frame_count=frame_count,
        )
    finally:
        cap.release()
