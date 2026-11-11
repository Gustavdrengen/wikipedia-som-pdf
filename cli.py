#!/usr/bin/env python3
"""Command-line entrypoint for the article PDF generator."""

import argparse
from datetime import datetime
from pathlib import Path

from src.app import generate
from src.cache_maintenance import cache_stats, format_bytes, prune_cache
from src.config import DEFAULT_REQUEST_DELAY


def _format_time(timestamp: float | None) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M") if timestamp else "-"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate an interconnected offline article PDF collection.")
    parser.add_argument("master_file", nargs="?", type=Path)
    parser.add_argument("--output", type=Path, default=Path("Noter"))
    parser.add_argument("--workers", type=int, default=None, help="Number of article downloads and PDFs rendered concurrently (default: up to 4)")
    parser.add_argument("--request-delay", type=float, default=DEFAULT_REQUEST_DELAY, help="Additional seconds to wait between article downloads (default: 1)")
    parser.add_argument("--epub", type=Path, help="Also write the collection to this EPUB file")
    parser.add_argument("--refresh", action="store_true", help="Ignore cached article HTML and fetch every article again")
    parser.add_argument("--prune-output", action="store_true", help="Remove PDFs in the output folder that are not part of this run")
    maintenance = parser.add_mutually_exclusive_group()
    maintenance.add_argument("--cache-stats", action="store_true", help="Show article/media cache size and exit")
    maintenance.add_argument("--prune-cache", type=float, metavar="DAYS", help="Delete cache files older than DAYS and exit (0 clears everything)")
    args = parser.parse_args()

    if args.cache_stats or args.prune_cache is not None:
        if args.master_file is not None:
            parser.error("--cache-stats/--prune-cache cannot be combined with a master file")
        if args.cache_stats:
            stats = cache_stats()
            for directory, (files, size) in stats.directories.items():
                print(f"{directory}: {files} file(s), {format_bytes(size)}")
            print(
                f"Total: {stats.files} file(s), {format_bytes(stats.bytes)}; "
                f"oldest {_format_time(stats.oldest)}, newest {_format_time(stats.newest)}"
            )
        else:
            days = args.prune_cache
            if days < 0:
                parser.error("--prune-cache cannot be negative")
            result = prune_cache(days)
            print(f"Removed {result['removed']} cache file(s), freed {format_bytes(result['freed'])}.")
        return 0

    if args.master_file is None:
        parser.error("the following arguments are required: master_file")
    if args.workers is not None and args.workers < 1:
        parser.error("--workers must be at least 1")
    if args.request_delay < 0:
        parser.error("--request-delay cannot be negative")
    try:
        generate(
            args.master_file,
            args.output,
            args.workers,
            args.request_delay,
            refresh=args.refresh,
            epub=args.epub,
            prune_output=args.prune_output,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"Error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
