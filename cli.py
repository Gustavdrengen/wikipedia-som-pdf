#!/usr/bin/env python3
"""Command-line entrypoint for the article PDF generator."""

import argparse
from pathlib import Path

from src.app import generate
from src.config import DEFAULT_REQUEST_DELAY


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate an interconnected offline article PDF collection.")
    parser.add_argument("master_file", type=Path)
    parser.add_argument("--output", type=Path, default=Path("Noter"))
    parser.add_argument("--workers", type=int, default=None, help="Number of article downloads and PDFs rendered concurrently (default: up to 2)")
    parser.add_argument("--request-delay", type=float, default=DEFAULT_REQUEST_DELAY, help="Additional seconds to wait between article downloads (default: 0)")
    args = parser.parse_args()
    if args.workers is not None and args.workers < 1:
        parser.error("--workers must be at least 1")
    if args.request_delay < 0:
        parser.error("--request-delay cannot be negative")
    try:
        generate(args.master_file, args.output, args.workers, args.request_delay)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"Error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
