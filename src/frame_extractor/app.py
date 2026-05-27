"""FastAPI app — Jinja2 admin UI + JSON API + static file serving."""

from __future__ import annotations

import asyncio
import io
import re
import shutil
import urllib.parse
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Literal
from uuid import UUID

import asyncpg
import structlog
from fastapi import (
    Depends,
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    FileResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .config import Settings
from .extractor.opencv_backend import probe
from .extractor.sampling import ExtractionParams
from .extractor.worker import remove_legacy_job_output_dir, remove_video_output_dir
from .job_queue import JobQueue
from .repository import FrameRepository, JobRepository, VideoRepository

log = structlog.get_logger(__name__)

_HERE = Path(__file__).resolve().parent
_TEMPLATES_DIR = _HERE / "templates"
_STATIC_DIR = _HERE / "static"


# ---------------------------------------------------------------------------
# Pydantic body models
# ---------------------------------------------------------------------------


class JobCreate(BaseModel):
    target_fps: float = Field(default=5.0, gt=0, le=240)
    interval_sec: float | None = Field(default=None, gt=0, le=3600)
    resize_w: int | None = Field(default=None, gt=0, le=16384)
    resize_h: int | None = Field(default=None, gt=0, le=16384)
    head_skip_sec: float = Field(default=0.0, ge=0, le=86400)
    tail_skip_sec: float = Field(default=0.0, ge=0, le=86400)
    sampling_mode: Literal["uniform", "random_n"] = "uniform"
    random_n: int | None = Field(default=None, gt=0, le=1_000_000)
    seed: int | None = None
    format: Literal["png"] = "png"

class BatchJobCreate(BaseModel):
    video_ids: list[UUID] = Field(min_length=1, max_length=10_000)
    params: JobCreate

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


_ALLOWED_VIDEO_EXTS = {"mp4", "mov", "avi", "mkv", "webm", "m4v"}
_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


def build_app(
    *,
    settings: Settings,
    pool: asyncpg.Pool,
    video_repo: VideoRepository,
    job_repo: JobRepository,
    frame_repo: FrameRepository,
    job_queue: JobQueue,
) -> FastAPI:
    app = FastAPI(title="FrameExtractor", docs_url=None, redoc_url=None)

    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    # Serve raw media + extracted frames directly — bind-mounted dirs.
    app.mount(
        "/media", StaticFiles(directory=settings.media_dir), name="media",
    )
    app.mount(
        "/frames", StaticFiles(directory=settings.frames_dir), name="frames",
    )

    media_root = Path(settings.media_dir).resolve()
    frames_root = Path(settings.frames_dir).resolve()
    import_root: Path | None = (
        Path(settings.import_dir).resolve() if settings.import_enabled else None
    )
    if import_root is not None:
        # Be forgiving here: the directory may be a bind mount that comes up
        # empty. We just create it if missing so the operator can drop files
        # in later.
        import_root.mkdir(parents=True, exist_ok=True)

    # ---- helpers ----------------------------------------------------------

    def _video_to_dict(row: dict[str, Any]) -> dict[str, Any]:
        return _jsonify_row(row)

    def _resolve_inside(root: Path, raw: str) -> Path:
        """Resolve ``raw`` (a relative path string from the client) against
        ``root`` and confirm the result stays inside ``root``. Raises HTTP
        400/403 on traversal or invalid input."""
        rel = raw.strip().lstrip("/\\")
        if not rel or rel in {".", ".."}:
            raise HTTPException(400, "invalid path")
        candidate = (root / rel).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            raise HTTPException(403, "path escapes import root")
        return candidate

    def _scan_import_root() -> list[dict[str, Any]]:
        """Walk ``import_root`` and return rows describing each video file
        found. Cheap (just stat + extension check); no DB lookup."""
        assert import_root is not None
        out: list[dict[str, Any]] = []
        for p in sorted(import_root.rglob("*")):
            if not p.is_file():
                continue
            ext = p.suffix.lower().lstrip(".")
            if ext not in _ALLOWED_VIDEO_EXTS:
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            out.append({
                "name": p.relative_to(import_root).as_posix(),
                "size_bytes": size,
                "ext": ext,
            })
        return out

    async def _register_existing_file(source: Path) -> dict[str, Any]:
        """Materialize a video that already exists on disk (under
        import_root) into the media dir + a video row. Honors
        ``settings.import_move`` for copy vs rename. Returns the inserted
        row as a JSON dict."""
        ext = source.suffix.lower().lstrip(".")
        if ext not in _ALLOWED_VIDEO_EXTS:
            raise HTTPException(415, f"unsupported extension: .{ext}")
        if not source.is_file():
            raise HTTPException(404, f"not a file: {source.name}")

        safe_name = _SAFE_FILENAME.sub("_", source.name)
        if not safe_name:
            raise HTTPException(400, "empty filename")

        video_id = uuid.uuid4()
        dest = media_root / f"{video_id}.{ext}"

        def _materialize() -> int:
            if settings.import_move:
                # Rename inside the same volume is atomic; cross-volume falls
                # back to copy+unlink via shutil.move.
                shutil.move(str(source), str(dest))
            else:
                shutil.copy2(str(source), str(dest))
            return dest.stat().st_size

        try:
            size_written = await asyncio.to_thread(_materialize)
        except OSError as exc:
            raise HTTPException(500, f"import failed: {exc}")

        try:
            meta = await asyncio.to_thread(probe, dest)
            duration = meta.duration_sec
            fps = meta.src_fps
            width = meta.width
            height = meta.height
        except Exception as exc:
            log.warning("video_probe_failed", file=str(dest), err=str(exc))
            duration = fps = None
            width = height = None

        row = await video_repo.insert(
            video_id=video_id,
            filename=safe_name,
            file_path=str(dest),
            container_ext=ext,
            duration_sec=duration,
            src_fps=fps,
            width=width,
            height=height,
            size_bytes=size_written,
        )
        return _jsonify_row(row)

    def _attachment_disposition(filename: str) -> str:
        """Build a RFC 5987-compliant Content-Disposition value so non-ASCII
        filenames survive intact."""
        ascii_fallback = filename.encode("ascii", "ignore").decode() or "file"
        quoted = urllib.parse.quote(filename, safe="")
        return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quoted}"

    # ---- HTML pages -------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        rows = await video_repo.list_all()
        return templates.TemplateResponse(
            request, "index.html", {
                "title": "FrameExtractor",
                "videos": [_video_to_dict(r) for r in rows],
                "max_upload_mb": settings.max_upload_mb,
                "default_target_fps": settings.default_target_fps,
<<<<<<< HEAD
                "media_dir": settings.media_dir,
=======
                "import_enabled": settings.import_enabled,
                "import_dir": settings.import_dir,
>>>>>>> d2496dce02bb9cb2a5fba4bd6c8d023966e14368
            },
        )

    @app.get("/videos/{video_id}", response_class=HTMLResponse)
    async def video_detail(request: Request, video_id: UUID) -> HTMLResponse:
        video = await video_repo.get(video_id)
        if video is None:
            raise HTTPException(404, "video not found")
        jobs = await job_repo.list_for_video(video_id)
        return templates.TemplateResponse(
            request, "video_detail.html", {
                "title": video["filename"],
                "video": _video_to_dict(video),
                "jobs": [_jsonify_row(j) for j in jobs],
                "default_target_fps": settings.default_target_fps,
            },
        )

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    async def job_detail(request: Request, job_id: UUID) -> HTMLResponse:
        job = await job_repo.get(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        video = await video_repo.get(job["video_id"])
        frames = await frame_repo.list_for_job(job_id, limit=500, offset=0)
        total_frames = await frame_repo.count_for_job(job_id)
        return templates.TemplateResponse(
            request, "job_detail.html", {
                "title": f"Job {str(job_id)[:8]}",
                "job": _jsonify_row(job),
                "video": _video_to_dict(video) if video else None,
                "frames": [_jsonify_row(f) for f in frames],
                "total_frames": total_frames,
            },
        )

    # ---- API: videos ------------------------------------------------------

    async def _save_one_upload(file: UploadFile) -> dict[str, Any]:
        """Save one UploadFile and INSERT a video row. Raises HTTPException
        for caller to catch and bucket. file.close() is the caller's job."""
        # webkitdirectory submissions carry the directory path in filename
        # (e.g. "subdir/clip.mp4"). Take just the basename.
        raw = file.filename or ""
        basename = Path(raw).name
        safe_name = _SAFE_FILENAME.sub("_", basename)
        if not safe_name:
            raise HTTPException(400, "empty filename")

        ext = Path(safe_name).suffix.lower().lstrip(".")
        if ext not in _ALLOWED_VIDEO_EXTS:
            raise HTTPException(
                415,
                f"unsupported extension: .{ext} "
                f"(allowed: {', '.join(sorted(_ALLOWED_VIDEO_EXTS))})",
            )

        video_id = uuid.uuid4()
        dest = media_root / f"{video_id}.{ext}"
        size_limit = settings.max_upload_mb * 1024 * 1024
        size_written = 0
        with dest.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size_written += len(chunk)
                if size_written > size_limit:
                    out.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(
                        413,
                        f"upload exceeds {settings.max_upload_mb} MB limit",
                    )
                out.write(chunk)

        try:
            meta = await asyncio.to_thread(probe, dest)
            duration = meta.duration_sec
            fps = meta.src_fps
            width = meta.width
            height = meta.height
        except Exception as exc:
            log.warning("video_probe_failed", file=str(dest), err=str(exc))
            duration = fps = None
            width = height = None

        row = await video_repo.insert(
            video_id=video_id,
            filename=safe_name,
            file_path=str(dest),
            container_ext=ext,
            duration_sec=duration,
            src_fps=fps,
            width=width,
            height=height,
            size_bytes=size_written,
        )
        return _jsonify_row(row)

    @app.post("/api/videos")
    async def upload_videos(
        files: list[UploadFile] = File(...),
    ) -> dict[str, Any]:
        """Multi-file upload.

        Returns a summary:
          - uploaded: list of created video rows
          - failed:   files that errored AFTER passing the extension filter
          - skipped:  files silently dropped because their extension is not
                      a video (folder uploads often include images, docs, etc.)
        """
        if not files:
            raise HTTPException(400, "no files provided")

        uploaded: list[dict[str, Any]] = []
        failed: list[dict[str, str]] = []
        skipped: list[str] = []

        for file in files:
            raw = file.filename or ""
            basename = Path(raw).name
            ext = Path(basename).suffix.lower().lstrip(".")
            # Silent skip for non-video so folder uploads stay quiet.
            if ext not in _ALLOWED_VIDEO_EXTS:
                skipped.append(basename or "(unnamed)")
                await file.close()
                continue
            try:
                row = await _save_one_upload(file)
                uploaded.append(row)
            except HTTPException as exc:
                failed.append({
                    "filename": basename,
                    "error": str(exc.detail),
                })
            except Exception as exc:
                log.exception("upload_failed", file=basename)
                failed.append({"filename": basename, "error": str(exc)})
            finally:
                await file.close()

        return {
            "uploaded": uploaded,
            "failed": failed,
            "skipped": skipped,
        }

    # ---- API: server-side import (opt-in via FX_IMPORT_DIR) ---------------

    @app.get("/api/import")
    async def list_import_candidates() -> dict[str, Any]:
        """List video files under FX_IMPORT_DIR. The browser-upload flow
        is the default; this is for environments where multipart upload
        through the browser is blocked but files can land on the server
        via SCP / SFTP / bind mount."""
        if import_root is None:
            raise HTTPException(404, "import disabled (set FX_IMPORT_DIR)")
        return {
            "root": str(import_root),
            "files": await asyncio.to_thread(_scan_import_root),
        }

    @app.post("/api/import")
    async def import_from_path(body: ImportRequest) -> dict[str, Any]:
        """Register files from FX_IMPORT_DIR as videos.

        - ``names == []`` → import everything the scan finds.
        - ``names == [...]`` → import just those relative paths.

        Each entry is resolved against the import root and rejected if it
        escapes (path traversal). Result shape mirrors the multipart
        upload endpoint."""
        if import_root is None:
            raise HTTPException(404, "import disabled (set FX_IMPORT_DIR)")

        if body.names:
            candidates = [_resolve_inside(import_root, n) for n in body.names]
        else:
            scan = await asyncio.to_thread(_scan_import_root)
            candidates = [import_root / e["name"] for e in scan]

        if not candidates:
            return {"uploaded": [], "failed": [], "skipped": []}

        uploaded: list[dict[str, Any]] = []
        failed: list[dict[str, str]] = []
        skipped: list[str] = []

        for source in candidates:
            label = source.relative_to(import_root).as_posix()
            ext = source.suffix.lower().lstrip(".")
            if ext not in _ALLOWED_VIDEO_EXTS:
                skipped.append(label)
                continue
            try:
                row = await _register_existing_file(source)
                uploaded.append(row)
            except HTTPException as exc:
                failed.append({"filename": label, "error": str(exc.detail)})
            except Exception as exc:
                log.exception("import_failed", file=label)
                failed.append({"filename": label, "error": str(exc)})

        return {
            "uploaded": uploaded,
            "failed": failed,
            "skipped": skipped,
            "moved": settings.import_move,
        }

    @app.get("/api/videos")
    async def list_videos() -> list[dict[str, Any]]:
        rows = await video_repo.list_all()
        return [_jsonify_row(r) for r in rows]

    @app.post("/api/videos/rescan")
    async def rescan_media() -> dict[str, Any]:
        """Two-way sync between FX_MEDIA_DIR and the video table.

        Top-level only (no recursion).
          * disk has, DB doesn't  → probe + INSERT
          * disk has, DB has      → skip (skipped_registered)
          * non-video on disk     → silent skip (skipped_non_video)
          * DB has, disk doesn't  → DELETE row (cascades to jobs/frames)
                                    and drop the per-video frames folder
        """
        existing_rows = await video_repo.list_all()
        # filter to rows whose file_path is rooted under THIS media_root.
        # uploads + rescans both land under media_root, so this is the
        # universe we own. anything outside (manual edits / legacy) we
        # leave alone.
        existing_in_scope: dict[str, dict[str, Any]] = {}
        for row in existing_rows:
            try:
                Path(row["file_path"]).resolve().relative_to(media_root)
            except ValueError:
                continue
            existing_in_scope[row["file_path"]] = row

        on_disk_paths: set[str] = set()
        added: list[dict[str, Any]] = []
        skipped_registered: list[str] = []
        skipped_non_video: list[str] = []
        removed: list[str] = []

        # ---- pass 1: walk disk → add newcomers --------------------------
        for p in sorted(media_root.iterdir()):
            if not p.is_file():
                continue
            on_disk_paths.add(str(p))
            ext = p.suffix.lower().lstrip(".")
            if ext not in _ALLOWED_VIDEO_EXTS:
                skipped_non_video.append(p.name)
                continue
            if str(p) in existing_in_scope:
                skipped_registered.append(p.name)
                continue
            try:
                meta = await asyncio.to_thread(probe, p)
                duration = meta.duration_sec
                fps = meta.src_fps
                width = meta.width
                height = meta.height
            except Exception as exc:
                log.warning("rescan_probe_failed", file=str(p), err=str(exc))
                duration = fps = None
                width = height = None
            try:
                size_bytes = p.stat().st_size
            except OSError:
                size_bytes = None
            row = await video_repo.insert(
                video_id=uuid.uuid4(),
                filename=p.name,
                file_path=str(p),
                container_ext=ext,
                duration_sec=duration,
                src_fps=fps,
                width=width,
                height=height,
                size_bytes=size_bytes,
            )
            added.append(_jsonify_row(row))

        # ---- pass 2: sweep DB → drop orphans the user removed from disk -
        for fp, row in existing_in_scope.items():
            if fp in on_disk_paths:
                continue
            # cancel any in-flight job first (queue picks the cancel up;
            # DB cascade then takes care of the row itself).
            jobs = await job_repo.list_for_video(row["id"])
            for j in jobs:
                if j["status"] in ("queued", "running"):
                    job_queue.request_cancel(j["id"])
                # legacy <frames_root>/<job_id>/ dirs from before the
                # per-video folder rename — be safe and clean those too.
                remove_legacy_job_output_dir(frames_root, j["id"])
            remove_video_output_dir(frames_root, row["filename"])
            await video_repo.delete(row["id"])
            removed.append(row["filename"])
            log.info("rescan_removed_orphan", filename=row["filename"], video_id=str(row["id"]))

        return {
            "media_dir": settings.media_dir,
            "added": added,
            "skipped_registered": skipped_registered,
            "skipped_non_video": skipped_non_video,
            "removed": removed,
        }

    @app.get("/api/videos/{video_id}")
    async def get_video(video_id: UUID) -> dict[str, Any]:
        row = await video_repo.get(video_id)
        if row is None:
            raise HTTPException(404, "video not found")
        return _jsonify_row(row)

    @app.get("/api/videos/{video_id}/download")
    async def download_video(video_id: UUID) -> FileResponse:
        """Force-download the original upload. The /media/* static mount
        serves it inline (browser preview); this endpoint adds
        Content-Disposition: attachment so the browser saves it."""
        row = await video_repo.get(video_id)
        if row is None:
            raise HTTPException(404, "video not found")
        p = Path(row["file_path"]).resolve()
        try:
            p.relative_to(media_root)
        except ValueError:
            raise HTTPException(403, "file_path escapes media_root")
        if not p.is_file():
            raise HTTPException(404, "underlying file missing")
        filename = row.get("filename") or p.name
        return FileResponse(
            path=str(p),
            media_type="application/octet-stream",
            headers={"Content-Disposition": _attachment_disposition(filename)},
        )

    @app.delete("/api/videos/{video_id}")
    async def delete_video(video_id: UUID) -> dict[str, bool]:
        video = await video_repo.get(video_id)
        if video is None:
            raise HTTPException(404, "video not found")

        # Cancel any in-flight jobs for this video before cascade.
        jobs = await job_repo.list_for_video(video_id)
        for j in jobs:
            if j["status"] in ("queued", "running"):
                job_queue.request_cancel(j["id"])
            # Backward cleanup: older runs may have written to
            # <frames_root>/<job_id>/ before the per-video folder rename.
            remove_legacy_job_output_dir(frames_root, j["id"])

        # New layout: one folder per video, share across all its jobs.
        remove_video_output_dir(frames_root, video["filename"])

        # DB cascade removes job + frame rows.
        await video_repo.delete(video_id)

        # Remove source file (best effort).
        try:
            Path(video["file_path"]).unlink(missing_ok=True)
        except OSError as exc:
            log.warning("video_unlink_failed", path=video["file_path"], err=str(exc))

        return {"ok": True}

    # ---- API: jobs --------------------------------------------------------

    @app.post("/api/videos/{video_id}/jobs", status_code=201)
    async def create_job(video_id: UUID, body: JobCreate) -> dict[str, Any]:
        video = await video_repo.get(video_id)
        if video is None:
            raise HTTPException(404, "video not found")

        params = body.model_dump(exclude_none=False)
        # Validate via the dataclass before persisting.
        try:
            ExtractionParams(
                target_fps=body.target_fps,
                interval_sec=body.interval_sec,
                resize_w=body.resize_w,
                resize_h=body.resize_h,
                head_skip_sec=body.head_skip_sec,
                tail_skip_sec=body.tail_skip_sec,
                sampling_mode=body.sampling_mode,
                random_n=body.random_n,
                format=body.format,
                seed=body.seed,
            ).validate()
        except ValueError as exc:
            raise HTTPException(400, str(exc))

        row = await job_repo.create(video_id=video_id, params=params)
        await job_queue.enqueue(row["id"])
        return _jsonify_row(row)

    @app.post("/api/jobs/batch", status_code=201)
    async def create_batch_jobs(body: BatchJobCreate) -> dict[str, Any]:
        """Create one extraction job per video_id with identical params.

        Validates the shared params once. Per-video errors (missing row,
        DB failures) go into ``failed`` so a partial batch still creates
        the good ones.
        """
        params_dict = body.params.model_dump(exclude_none=False)
        try:
            ExtractionParams(
                target_fps=body.params.target_fps,
                interval_sec=body.params.interval_sec,
                resize_w=body.params.resize_w,
                resize_h=body.params.resize_h,
                head_skip_sec=body.params.head_skip_sec,
                tail_skip_sec=body.params.tail_skip_sec,
                sampling_mode=body.params.sampling_mode,
                random_n=body.params.random_n,
                format=body.params.format,
                seed=body.params.seed,
            ).validate()
        except ValueError as exc:
            raise HTTPException(400, str(exc))

        created: list[dict[str, Any]] = []
        failed: list[dict[str, str]] = []
        for vid in body.video_ids:
            v = await video_repo.get(vid)
            if v is None:
                failed.append({"video_id": str(vid), "error": "video not found"})
                continue
            try:
                row = await job_repo.create(video_id=vid, params=params_dict)
                await job_queue.enqueue(row["id"])
                created.append(_jsonify_row(row))
            except Exception as exc:
                log.exception("batch_job_create_failed", video_id=str(vid))
                failed.append({"video_id": str(vid), "error": str(exc)})
        return {"created": created, "failed": failed}

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: UUID) -> dict[str, Any]:
        row = await job_repo.get(job_id)
        if row is None:
            raise HTTPException(404, "job not found")
        return _jsonify_row(row)

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: UUID) -> dict[str, Any]:
        row = await job_repo.get(job_id)
        if row is None:
            raise HTTPException(404, "job not found")
        if row["status"] in ("done", "failed", "cancelled"):
            return {"ok": True, "status": row["status"]}
        signalled = job_queue.request_cancel(job_id)
        if not signalled:
            # No live cancel event (rare: between worker pop and event setup)
            # — set the DB status directly so the user sees feedback.
            await job_repo.mark_cancelled(job_id)
        return {"ok": True, "signalled": signalled}

    @app.get("/api/jobs/{job_id}/download")
    async def download_job_frames(job_id: UUID) -> StreamingResponse:
        """Stream all frames of a job as a single uncompressed ZIP.

        Uses ``zipfile.ZIP_STORED`` because PNG is already compressed —
        deflate just wastes CPU. Streams in 1 MiB chunks so memory stays
        flat regardless of job size."""
        job = await job_repo.get(job_id)
        if job is None:
            raise HTTPException(404, "job not found")

        # We page through frames in batches so the DB call doesn't have to
        # buffer huge result sets in memory.
        total = await frame_repo.count_for_job(job_id)
        if total == 0:
            raise HTTPException(404, "no frames in this job")

        archive_name = f"job-{str(job_id)[:8]}.zip"

        async def _frame_pages() -> AsyncIterator[dict[str, Any]]:
            page_size = 200
            offset = 0
            while True:
                rows = await frame_repo.list_for_job(
                    job_id, limit=page_size, offset=offset,
                )
                if not rows:
                    break
                for r in rows:
                    yield r
                if len(rows) < page_size:
                    break
                offset += page_size

        async def _iter_zip() -> AsyncIterator[bytes]:
            buffer = io.BytesIO()
            with zipfile.ZipFile(
                buffer, mode="w", compression=zipfile.ZIP_STORED,
            ) as zf:
                async for row in _frame_pages():
                    fp = Path(row["file_path"]).resolve()
                    try:
                        fp.relative_to(frames_root)
                    except ValueError:
                        # Skip rows pointing outside the frames root rather
                        # than abort the whole archive.
                        log.warning(
                            "frame_outside_root_in_zip",
                            job_id=str(job_id), path=str(fp),
                        )
                        continue
                    if not fp.is_file():
                        continue
                    arcname = fp.name
                    zf.write(str(fp), arcname=arcname)
                    chunk = buffer.getvalue()
                    if chunk:
                        yield chunk
                        buffer.seek(0)
                        buffer.truncate(0)
            tail = buffer.getvalue()
            if tail:
                yield tail

        return StreamingResponse(
            _iter_zip(),
            media_type="application/zip",
            headers={
                "Content-Disposition": _attachment_disposition(archive_name),
            },
        )

    @app.get("/api/jobs/{job_id}/frames")
    async def list_frames(
        job_id: UUID,
        page: int = Query(1, ge=1),
        size: int = Query(50, ge=1, le=500),
    ) -> dict[str, Any]:
        if await job_repo.get(job_id) is None:
            raise HTTPException(404, "job not found")
        total = await frame_repo.count_for_job(job_id)
        rows = await frame_repo.list_for_job(
            job_id, limit=size, offset=(page - 1) * size,
        )
        return {
            "rows": [_jsonify_row(r) for r in rows],
            "total": total,
            "page": page,
            "size": size,
        }

    # ---- API: frames ------------------------------------------------------

    @app.get("/api/frames/{frame_id}/download")
    async def download_frame(frame_id: UUID) -> FileResponse:
        """Force-download a single frame PNG.

        The /frames/* static mount displays the PNG inline; this endpoint
        adds Content-Disposition: attachment so a browser save dialog
        opens instead. Useful when corporate browser policy rewrites
        right-click → save URLs."""
        row = await frame_repo.get(frame_id)
        if row is None:
            raise HTTPException(404, "frame not found")
        p = Path(row["file_path"]).resolve()
        try:
            p.relative_to(frames_root)
        except ValueError:
            raise HTTPException(403, "file_path escapes frames_root")
        if not p.is_file():
            raise HTTPException(404, "underlying file missing")
        # Name the download by job + frame index so multiple downloads from
        # different jobs don't clobber each other in the user's downloads
        # folder.
        filename = f"job-{str(row['job_id'])[:8]}_frame-{p.name}"
        return FileResponse(
            path=str(p),
            media_type="image/png",
            headers={"Content-Disposition": _attachment_disposition(filename)},
        )

    @app.delete("/api/frames/{frame_id}")
    async def delete_frame(frame_id: UUID) -> dict[str, bool]:
        row = await frame_repo.get(frame_id)
        if row is None:
            raise HTTPException(404, "frame not found")
        # Path traversal guard
        p = Path(row["file_path"]).resolve()
        try:
            p.relative_to(frames_root)
        except ValueError:
            raise HTTPException(403, "file_path escapes frames_root")
        try:
            p.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("frame_unlink_failed", path=str(p), err=str(exc))
        await frame_repo.delete(frame_id)
        return {"ok": True}

    # ---- Health -----------------------------------------------------------

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        try:
            await pool.fetchval("SELECT 1")
            db_ok = True
        except Exception:
            db_ok = False
        return {
            "ok": db_ok,
            "now": datetime.now(timezone.utc).isoformat(),
        }

    # ---- Error envelope ---------------------------------------------------

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:  # noqa: ARG001
        log.exception("unhandled_error", path=str(request.url))
        return JSONResponse(status_code=500, content={"detail": "internal_error"})

    return app


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _isoformat(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _jsonify_row(row: dict[str, Any] | None) -> dict[str, Any]:
    if row is None:
        return {}
    out: dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, UUID):
            out[k] = str(v)
        elif isinstance(v, datetime):
            out[k] = _isoformat(v)
        else:
            out[k] = v
    return out
