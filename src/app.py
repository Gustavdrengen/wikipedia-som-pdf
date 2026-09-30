import hashlib
import time
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, ThreadPoolExecutor, as_completed, wait
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from urllib.error import HTTPError

from .articles import article_title, canonical_url, fetch_article, load_processed_article, safe_filename, store_processed_article
from .config import CACHE_DIR, MAX_CONCURRENCY, MEDIA_CACHE_DIR
from .html_processing import parse_article, prepare_media, rewrite_links
from .input import read_master_file
from .math_render import render_math_in_content
from .pdf_renderer import fingerprint_of, is_current, render_pdf, save_fingerprints
from .shortcuts import create_shortcut
from .utils import ascii_safe


def _render_job(payload: tuple[str, str, Path, str]) -> None:
    # Module-level so ProcessPoolExecutor can pickle it; the fingerprint lets
    # render_pdf skip PDFs whose content is unchanged since last run.
    title, content, output, _fingerprint = payload
    render_pdf(title, content, output)


def generate(master_file: Path, output: Path = Path("Noter"), workers: int | None = None, request_delay: float = 0, log=print) -> int:
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
            cached = load_processed_article(url, CACHE_DIR)
            if cached is not None:
                content, links = cached
                return url, content, links, True, None, time.monotonic() - started

            article_html = fetch_article(url, CACHE_DIR, request_delay)
            content, links = parse_article(url, article_html)
            store_processed_article(url, content, links, CACHE_DIR)
            return url, content, links, False, None, time.monotonic() - started
        except Exception as exc:
            return url, None, set(), False, exc, time.monotonic() - started

    with ThreadPoolExecutor(max_workers=fetch_workers) as executor:
        futures: dict[Future, str] = {
            executor.submit(download, url): url for url in root_urls
        }
        while futures:
            completed, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in completed:
                url = futures.pop(future)
                url, content, links, cache_hit, error, duration = future.result()
                if cache_hit:
                    stats["cache_hits"] += 1
                else:
                    stats["requests"] += 1
                log(f"{'Using cache' if cache_hit else 'Fetched'} {ascii_safe(article_title(url))} ({duration:.1f}s)", flush=True)
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
                        if link not in futures.values() and link not in pages:
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

    def prepare_one(item: tuple[str, tuple[str, set[str]]]) -> tuple[str, str]:
        url, (content, _) = item
        pdf = pdf_by_url[url]
        linked_content = rewrite_links(math_content[url], pdf_by_url, pdf)
        return url, prepare_media(linked_content, url, MEDIA_CACHE_DIR)

    log(f"Preparing media for {len(jobs)} PDFs with {worker_count} workers...", flush=True)
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {executor.submit(prepare_one, item): item[0] for item in jobs}
        for index, future in enumerate(as_completed(futures), 1):
            url, rendered_content = future.result()
            prepared_content[url] = rendered_content
            log(f"Prepared media {index}/{len(jobs)}: {ascii_safe(page_info[url][0])}", flush=True)
    media_duration = time.monotonic() - media_started

    render_started = time.monotonic()
    render_jobs = [
        (page_info[url][0], prepared_content[url], pdf_by_url[url], fingerprint_of(page_info[url][0], prepared_content[url]))
        for url, _ in jobs
        if not is_current(pdf_by_url[url], page_info[url][0], prepared_content[url])
    ]
    skipped = len(jobs) - len(render_jobs)

    if render_jobs:
        log(f"Rendering {len(render_jobs)} PDFs with {worker_count} workers...", flush=True)
    if skipped:
        log(f"Reusing {skipped} unchanged PDF(s).", flush=True)

    rendered: dict[str, str] = {}
    # PDF layout is pure-Python CPU work, so the GIL makes threads useless here;
    # separate processes are needed for real parallel rendering speedups.
    try:
        with ProcessPoolExecutor(max_workers=worker_count) as executor:
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
    save_fingerprints(rendered)

    for subject, urls in direct_by_subject.items():
        subject_dir = output / subject
        subject_dir.mkdir(parents=True, exist_ok=True)
        for url in urls:
            if url in pdf_by_url:
                create_shortcut(pdf_by_url[url], subject_dir / pdf_by_url[url].name)
    log(f"Done: {len(pages)} PDF(s) created in {articles_dir}; {stats['requests']} network request(s), {stats['cache_hits']} cache hits, {stats['not_found']} cached/new 404s, {stats['failures']} failures; media preparation took {media_duration:.1f}s; rendering took {time.monotonic() - render_started:.1f}s.", flush=True)
    return len(pages)
