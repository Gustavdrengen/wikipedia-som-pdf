import hashlib
import time
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, ThreadPoolExecutor, as_completed, wait
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from urllib.error import HTTPError

from .articles import CachedFailure, article_title, canonical_url, fetch_article, load_processed_article, safe_filename, store_processed_article
from .config import CACHE_DIR, MAX_CONCURRENCY, MEDIA_CACHE_DIR
from .epub import EpubArticle, write_epub
from .html_processing import parse_article, prepare_media, rewrite_links
from .input import read_master_file
from .math_render import render_math_in_content
from .pdf_renderer import fingerprint_of, is_current, prune_fingerprints, render_pdf, save_fingerprints
from .shortcuts import create_shortcut
from .utils import ascii_safe


def _render_job(payload: tuple[str, str, Path, str, str]) -> None:
    # Module-level so ProcessPoolExecutor can pickle it. The fingerprint is
    # reused instead of hashing the article HTML a second time in this process.
    title, content, output, fingerprint, source_url = payload
    render_pdf(title, content, output, fingerprint=fingerprint, source_url=source_url)


def generate(
    master_file: Path,
    output: Path = Path("Noter"),
    workers: int | None = None,
    request_delay: float = 0,
    log=print,
    *,
    refresh: bool = False,
    epub: Path | None = None,
    prune_output: bool = False,
) -> int:
    if workers is not None and workers < 1:
        raise ValueError("workers must be at least 1")
    if request_delay < 0:
        raise ValueError("request_delay cannot be negative")
    subjects = read_master_file(master_file)
    direct_by_subject = {}
    for subject, urls in subjects.items():
        direct_by_subject[subject] = {canonical for url in urls if (canonical := canonical_url(url))}
    root_urls = set().union(*direct_by_subject.values()) if direct_by_subject else set()
    pages: dict[str, tuple[str, set[str]]] = {}
    stats = {"requests": 0, "cache_hits": 0, "not_found": 0, "failures": 0}
    fetch_workers = min(MAX_CONCURRENCY, max(1, workers or MAX_CONCURRENCY))

    def download(url: str):
        started = time.monotonic()
        try:
            cached = None if refresh else load_processed_article(url, CACHE_DIR)
            if cached is not None:
                content, links = cached
                return url, content, links, True, None, time.monotonic() - started

            article_html = fetch_article(url, CACHE_DIR, request_delay, refresh=refresh)
            content, links = parse_article(url, article_html)
            store_processed_article(url, content, links, CACHE_DIR)
            return url, content, links, False, None, time.monotonic() - started
        except Exception as exc:
            return url, None, set(), False, exc, time.monotonic() - started

    with ThreadPoolExecutor(max_workers=fetch_workers) as executor:
        # A set makes the one-level crawl O(1) per discovered link instead of
        # scanning every pending future for each link.
        queued: set[str] = set(root_urls)
        futures: dict[Future, str] = {
            executor.submit(download, url): url for url in queued
        }
        while futures:
            completed, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in completed:
                url = futures.pop(future)
                queued.discard(url)
                url, content, links, cache_hit, error, duration = future.result()
                cached_failure = isinstance(error, CachedFailure)
                if cache_hit or cached_failure:
                    stats["cache_hits"] += 1
                else:
                    stats["requests"] += 1
                log(f"{'Using cache' if cache_hit or cached_failure else 'Fetched'} {ascii_safe(article_title(url))} ({duration:.1f}s)", flush=True)
                if error:
                    if isinstance(error, HTTPError) and error.code == 404:
                        stats["not_found"] += 1
                    else:
                        stats["failures"] += 1
                    log(f"  ERROR: {ascii_safe(repr(error))}", flush=True)
                    continue
                pages[url] = (content, links)
                if url in root_urls:
                    for link in links:
                        if link not in queued and link not in pages:
                            queued.add(link)
                            futures[executor.submit(download, link)] = link

    articles_dir = output / "artikler"
    articles_dir.mkdir(parents=True, exist_ok=True)
    page_info = {url: (article_title(url), safe_filename(article_title(url))) for url in pages}
    used_filenames: dict[str, str] = {}
    for url, (title, filename) in page_info.items():
        if filename in used_filenames and used_filenames[filename] != url:
            stem, suffix = Path(filename).stem, Path(filename).suffix
            filename = f"{stem} - {hashlib.sha256(url.encode('utf-8')).hexdigest()[:8]}{suffix}"
            page_info[url] = (title, filename)
        used_filenames[filename] = url
    pdf_by_url = {url: articles_dir / filename for url, (_, filename) in page_info.items()}
    jobs = list(pages.items())
    worker_count = min(workers or MAX_CONCURRENCY, MAX_CONCURRENCY, max(1, len(jobs)))

    media_started = time.monotonic()
    # Math (TeX) is rendered locally with matplotlib, which is not thread-safe, so
    # this pass runs single-threaded before figure images are fetched in parallel.
    math_content: dict[str, str] = {}
    math_formulas = 0
    for url, (content, _) in jobs:
        math_formulas += content.count("<img data-math=")
        math_content[url] = render_math_in_content(content)
    if math_formulas:
        log(f"Rendered {math_formulas} math formula(s) locally ({time.monotonic() - media_started:.1f}s)", flush=True)

    prepared_content: dict[str, str] = {}
    epub_content: dict[str, str] = {}

    def prepare_one(item: tuple[str, tuple[str, set[str]]]) -> tuple[str, str, str]:
        url, (content, _) = item
        pdf = pdf_by_url[url]
        # Keep the media-prepared HTML for EPUB export; PDF link rewriting is
        # applied only to the copy that goes into the PDF.
        media_ready = prepare_media(math_content[url], url, MEDIA_CACHE_DIR)
        return url, rewrite_links(media_ready, pdf_by_url, pdf), media_ready

    log(f"Preparing media for {len(jobs)} PDFs with {worker_count} workers...", flush=True)
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {executor.submit(prepare_one, item): item[0] for item in jobs}
        for index, future in enumerate(as_completed(futures), 1):
            url, rendered_content, media_ready = future.result()
            prepared_content[url] = rendered_content
            epub_content[url] = media_ready
            log(f"Prepared media {index}/{len(jobs)}: {ascii_safe(page_info[url][0])}", flush=True)
    media_duration = time.monotonic() - media_started

    render_started = time.monotonic()
    render_jobs: list[tuple[str, str, Path, str, str]] = []
    for url, _ in jobs:
        title = page_info[url][0]
        content = prepared_content[url]
        fingerprint = fingerprint_of(title, content)
        if not is_current(pdf_by_url[url], title, content, fingerprint=fingerprint):
            render_jobs.append((title, content, pdf_by_url[url], fingerprint, url))
    skipped = len(jobs) - len(render_jobs)

    # Spawning worker processes costs ~0.5-1s on Windows. Benchmarks showed the
    # pool only pays off once the batch contains a substantial amount of HTML;
    # a handful of short articles renders faster in-process.
    pool_worthwhile = (
        worker_count > 1
        and len(render_jobs) > 1
        and sum(len(job[1]) for job in render_jobs) >= 200_000
    )
    if render_jobs:
        workers_detail = f" with {min(worker_count, len(render_jobs))} workers" if pool_worthwhile else ""
        log(f"Rendering {len(render_jobs)} PDF(s){workers_detail}...", flush=True)
    if skipped:
        log(f"Reusing {skipped} unchanged PDF(s).", flush=True)

    rendered: dict[str, str] = {}
    if pool_worthwhile:
        # PDF layout is pure-Python CPU work, so the GIL makes threads useless
        # here; separate processes are needed for real parallel speedups.
        try:
            with ProcessPoolExecutor(max_workers=min(worker_count, len(render_jobs))) as executor:
                futures = {executor.submit(_render_job, job): job for job in render_jobs}
                for index, future in enumerate(as_completed(futures), 1):
                    job = futures[future]
                    future.result()
                    rendered[str(job[2].resolve())] = job[3]
                    log(f"Rendered PDF {index}/{len(render_jobs)}: {ascii_safe(job[0])}", flush=True)
        except (OSError, ValueError, BrokenProcessPool):
            # Platforms without a working spawn/fork: fall back to in-process
            # rendering; the fingerprint cache makes the retry nearly free.
            for index, job in enumerate(render_jobs, 1):
                _render_job(job)
                rendered[str(job[2].resolve())] = job[3]
                log(f"Rendered PDF {index}/{len(render_jobs)}: {ascii_safe(job[0])}", flush=True)
    else:
        for index, job in enumerate(render_jobs, 1):
            _render_job(job)
            rendered[str(job[2].resolve())] = job[3]
            log(f"Rendered PDF {index}/{len(render_jobs)}: {ascii_safe(job[0])}", flush=True)
    if rendered:
        save_fingerprints(rendered)

    if prune_output:
        if not pages or stats["failures"]:
            # A transient network failure must not wipe a previously complete
            # collection from the output folder.
            log("Skipping output prune because the run was incomplete.", flush=True)
        else:
            produced = {str(path.resolve()) for path in pdf_by_url.values()}
            removed = 0
            for existing in articles_dir.glob("*.pdf"):
                if str(existing.resolve()) not in produced:
                    existing.unlink(missing_ok=True)
                    removed += 1
            stale_fingerprints = prune_fingerprints(produced)
            if removed:
                log(f"Removed {removed} orphaned PDF(s) from {articles_dir}.", flush=True)
            if stale_fingerprints:
                log(f"Removed {stale_fingerprints} stale render fingerprint(s).", flush=True)

    if epub is not None:
        if pages:
            # Keep the book order stable: subject order from the input file, then
            # followed articles that were not named explicitly. Network completion
            # order must not leak into the EPUB spine.
            ordered_urls: list[str] = []
            seen_urls: set[str] = set()
            for urls in direct_by_subject.values():
                for url in sorted((url for url in urls if url in pages), key=lambda item: page_info[item][0].casefold()):
                    if url not in seen_urls:
                        ordered_urls.append(url)
                        seen_urls.add(url)
            for url in sorted((url for url in pages if url not in seen_urls), key=lambda item: page_info[item][0].casefold()):
                ordered_urls.append(url)
            epub_articles = [
                EpubArticle(url, page_info[url][0], epub_content[url]) for url in ordered_urls
            ]
            collection_title = epub.stem or master_file.stem or "Artikler"
            write_epub(epub, collection_title, epub_articles, direct_by_subject)
            log(f"Wrote EPUB: {ascii_safe(str(epub))}", flush=True)
        else:
            log("No articles were downloaded; skipping EPUB export.", flush=True)

    for subject, urls in direct_by_subject.items():
        subject_dir = output / subject
        subject_dir.mkdir(parents=True, exist_ok=True)
        for url in urls:
            if url in pdf_by_url:
                create_shortcut(pdf_by_url[url], subject_dir / pdf_by_url[url].name)
    log(f"Done: {len(pages)} PDF(s) created in {articles_dir}; {stats['requests']} network request(s), {stats['cache_hits']} cache hits, {stats['not_found']} cached/new 404s, {stats['failures']} failures; media preparation took {media_duration:.1f}s; rendering took {time.monotonic() - render_started:.1f}s.", flush=True)
    return len(pages)
