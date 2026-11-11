"""Inspection and age-based pruning for the on-disk caches."""

import time
from dataclasses import dataclass
from pathlib import Path

from .config import CACHE_DIR, MEDIA_CACHE_DIR


@dataclass
class CacheStats:
    files: int
    bytes: int
    oldest: float | None
    newest: float | None
    directories: dict[str, tuple[int, int]]


def _files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return [path for path in directory.rglob("*") if path.is_file()]


def cache_stats(
    cache_dir: Path = CACHE_DIR,
    media_dir: Path = MEDIA_CACHE_DIR,
) -> CacheStats:
    total_files = 0
    total_bytes = 0
    oldest: float | None = None
    newest: float | None = None
    directories: dict[str, tuple[int, int]] = {}
    for directory in (cache_dir, media_dir):
        files = _files(directory)
        size = 0
        for path in files:
            try:
                stat = path.stat()
            except OSError:
                continue
            total_files += 1
            size += stat.st_size
            total_bytes += stat.st_size
            oldest = stat.st_mtime if oldest is None else min(oldest, stat.st_mtime)
            newest = stat.st_mtime if newest is None else max(newest, stat.st_mtime)
        directories[str(directory)] = (len(files), size)
    return CacheStats(total_files, total_bytes, oldest, newest, directories)


def prune_cache(
    max_age_days: float,
    cache_dir: Path = CACHE_DIR,
    media_dir: Path = MEDIA_CACHE_DIR,
) -> dict[str, int]:
    """Delete cache files older than ``max_age_days``.

    This only touches files inside the two cache directories, never the output
    folder. Pass ``0`` to clear both caches completely.
    """
    if max_age_days < 0:
        raise ValueError("max_age_days cannot be negative")
    cutoff = time.time() - max_age_days * 24 * 60 * 60
    removed = 0
    freed = 0
    for directory in (cache_dir, media_dir):
        for path in _files(directory):
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime >= cutoff:
                continue
            try:
                path.unlink()
            except OSError:
                continue
            removed += 1
            freed += stat.st_size
    return {"removed": removed, "freed": freed}


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GiB"
