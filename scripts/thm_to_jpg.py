"""Convert .THM thumbnail files to .jpg.

THM files (typically from Canon/Nikon camcorders and similar devices) are
standard JPEG byte streams stored with a .THM extension. Conversion is
just a file rename after verifying the JPEG magic bytes (FF D8 FF).

Examples
--------
  python thm_to_jpg.py C:\\path\\to\\folder
  python thm_to_jpg.py C:\\path\\to\\folder --recursive
  python thm_to_jpg.py C:\\path\\to\\folder --copy        # keep originals
  python thm_to_jpg.py C:\\path\\to\\folder --dry-run     # preview only
  python thm_to_jpg.py C:\\path\\to\\folder --overwrite   # replace existing .jpg

Stdlib only — Python 3.8+ should work.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

JPEG_MAGIC = b"\xff\xd8\xff"


def is_jpeg(path: Path) -> bool:
    """Return True if the file starts with the JPEG SOI marker."""
    try:
        with path.open("rb") as f:
            head = f.read(3)
    except OSError:
        return False
    return head.startswith(JPEG_MAGIC)


def find_thm(folder: Path, *, recursive: bool) -> list[Path]:
    """Return all *.THM (case-insensitive) under ``folder``, sorted."""
    pattern = "**/*" if recursive else "*"
    out: list[Path] = []
    for p in folder.glob(pattern):
        if p.is_file() and p.suffix.lower() == ".thm":
            out.append(p)
    out.sort()
    return out


def convert_one(
    src: Path,
    *,
    copy: bool,
    overwrite: bool,
    dry_run: bool,
) -> tuple[str, str]:
    """Convert one .THM. Returns (status, message).

    status:
      ok     — converted (or would convert, in dry-run)
      skip   — target exists and --overwrite was not set
      error  — not a JPEG, or filesystem error
    """
    dst = src.with_suffix(".jpg")

    if not is_jpeg(src):
        return ("error", f"not a JPEG (magic bytes don't match): {src}")

    if dst.exists() and not overwrite:
        return ("skip", f"target exists, use --overwrite: {dst}")

    if dry_run:
        verb = "copy" if copy else "rename"
        return ("ok", f"would {verb}: {src.name} -> {dst.name}")

    try:
        if copy:
            shutil.copy2(src, dst)
        else:
            # Path.rename refuses to overwrite on some platforms; use replace
            # so --overwrite works cross-platform.
            src.replace(dst) if overwrite else src.rename(dst)
    except OSError as exc:
        return ("error", f"{src.name}: {exc}")

    past = "copied" if copy else "renamed"
    return ("ok", f"{past}: {src.name} -> {dst.name}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Convert all .THM files in a folder to .jpg.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("folder", type=Path, help="Folder containing .THM files")
    p.add_argument("-r", "--recursive", action="store_true",
                   help="Descend into subfolders")
    p.add_argument("-c", "--copy", action="store_true",
                   help="Copy to .jpg instead of renaming (keeps originals)")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="Print what would happen, don't modify files")
    p.add_argument("--overwrite", action="store_true",
                   help="Overwrite existing .jpg targets (default: skip)")
    args = p.parse_args(argv)

    folder: Path = args.folder
    if not folder.is_dir():
        print(f"error: not a directory: {folder}", file=sys.stderr)
        return 2

    thm_files = find_thm(folder, recursive=args.recursive)
    if not thm_files:
        scope = " (recursive)" if args.recursive else ""
        print(f"no .THM files found in {folder}{scope}")
        return 0

    counts = {"ok": 0, "skip": 0, "error": 0}
    for src in thm_files:
        status, msg = convert_one(
            src,
            copy=args.copy,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
        )
        counts[status] += 1
        print(f"[{status:5}] {msg}")

    print()
    print(
        f"summary: ok={counts['ok']} skip={counts['skip']} "
        f"error={counts['error']} (total={len(thm_files)})"
        + (" [dry-run]" if args.dry_run else "")
    )
    return 0 if counts["error"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
