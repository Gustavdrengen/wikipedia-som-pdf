import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.error import HTTPError
import hashlib

from .config import CACHE_DIR, MAX_WIKIMEDIA_CONCURRENCY, MEDIA_CACHE_DIR
from .html_processing import parse_article, rewrite_links, rewrite_media
from .input import read_master_file
from .pdf_renderer import render_pdf
from .shortcuts import create_shortcut
from .utils import ascii_safe
from .wikipedia import article_title, canonical_url, clean_response_cache, fetch_article, safe_filename


def generate(master_file: Path, output: Path = Path("Noter"), workers: int | None = None, request_delay: float = 0, log=print) -> int:
    if workers is not None and workers < 1:
        raise ValueError("workers must be at least 1")
    if request_delay < 0:
        raise ValueError("request_delay cannot be negative")
    clean_response_cache(CACHE_DIR)
    subjects = read_master_file(master_file)
    direct_by_subject = {}
    for subject, urls in subjects.items():
        direct_by_subject[subject] = {canonical for url in urls if (canonical := canonical_url(url))}
    root_urls = set().union(*direct_by_subject.values()) if direct_by_subject else set()
    pages: dict[str, tuple[str, set[str]]] = {}
    pending = list(root_urls)
    discovered: set[str] = set(root_urls)
    stats = {"requests": 0, "cache_hits": 0, "not_found": 0, "failures": 0}

    def download(url: str):
        started = time.monotonic()
        try:
            article_html, cache_hit = fetch_article(url, CACHE_DIR, request_delay)
            content, links = parse_article(url, article_html, CACHE_DIR)
            return url, content, links, cache_hit, None, time.monotonic() - started
        except Exception as exc:
            return url, None, set(), False, exc, time.monotonic() - started

    fetch_workers = min(MAX_WIKIMEDIA_CONCURRENCY, max(1, workers or MAX_WIKIMEDIA_CONCURRENCY))
    while pending:
        batch, pending = pending[:fetch_workers], pending[fetch_workers:]
        with ThreadPoolExecutor(max_workers=fetch_workers) as executor:
            futures = {executor.submit(download, url): url for url in batch}
            for future in as_completed(futures):
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
                        if link not in discovered:
                            discovered.add(link)
                            pending.append(link)
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
    worker_count = min(workers or MAX_WIKIMEDIA_CONCURRENCY, MAX_WIKIMEDIA_CONCURRENCY, max(1, len(jobs)))
    log(f"Rendering {len(jobs)} PDFs with {worker_count} workers...", flush=True)

    def render_one(item):
        url, (content, _) = item
        pdf = pdf_by_url[url]
        rendered = rewrite_media(rewrite_links(content, pdf_by_url, pdf), MEDIA_CACHE_DIR)
        render_pdf(page_info[url][0], rendered, pdf)
        return url

    render_started = time.monotonic()
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {executor.submit(render_one, item): item[0] for item in jobs}
        for index, future in enumerate(as_completed(futures), 1):
            url = futures[future]
            future.result()
            log(f"Rendered PDF {index}/{len(jobs)}: {ascii_safe(page_info[url][0])}", flush=True)
    for subject, urls in direct_by_subject.items():
        subject_dir = output / subject
        subject_dir.mkdir(parents=True, exist_ok=True)
        for url in urls:
            if url in pdf_by_url:
                create_shortcut(pdf_by_url[url], subject_dir / pdf_by_url[url].name)
    log(f"Done: {len(pages)} PDF(s) created in {articles_dir}; {stats['requests']} network request(s), {stats['cache_hits']} cache hits, {stats['not_found']} cached/new 404s, {stats['failures']} failures; rendering took {time.monotonic() - render_started:.1f}s.", flush=True)
    return len(pages)
