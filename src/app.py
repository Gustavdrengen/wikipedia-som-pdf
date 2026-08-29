import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .config import CACHE_DIR, DEFAULT_REQUEST_DELAY, MAX_WIKIMEDIA_CONCURRENCY, MEDIA_CACHE_DIR
from .html_processing import parse_article, rewrite_links, rewrite_media
from .input import read_master_file
from .pdf_renderer import render_pdf
from .shortcuts import create_shortcut
from .utils import ascii_safe
from .wikipedia import article_title, canonical_url, clean_response_cache, fetch_article, safe_filename


def generate(master_file: Path, output: Path = Path("Noter"), workers: int | None = None, request_delay: float = DEFAULT_REQUEST_DELAY, log=print) -> int:
    if workers is not None and workers < 1:
        raise ValueError("workers must be at least 1")
    if request_delay < 0:
        raise ValueError("request_delay cannot be negative")
    clean_response_cache(CACHE_DIR)
    subjects = read_master_file(master_file)
    direct_by_subject = {subject: {canonical_url(url) for url in urls if canonical_url(url)} for subject, urls in subjects.items()}
    root_urls = set().union(*direct_by_subject.values()) if direct_by_subject else set()
    pages: dict[str, tuple[str, set[str]]] = {}
    pending = list(root_urls)
    discovered: set[str] = set()
    while pending:
        url = pending.pop(0)
        log(f"Downloading {ascii_safe(article_title(url))}", flush=True)
        cache_hit = False
        try:
            article_html, cache_hit = fetch_article(url)
            content, links = parse_article(url, article_html)
            pages[url] = (content, links)
            if url in root_urls:
                for link in links:
                    if link not in pages and link not in discovered:
                        discovered.add(link)
                        pending.append(link)
        except Exception as exc:
            log(f"  ERROR: {ascii_safe(repr(exc))}", flush=True)
        if not cache_hit and request_delay:
            time.sleep(request_delay)
    articles_dir = output / "artikler"
    articles_dir.mkdir(parents=True, exist_ok=True)
    pdf_by_url = {url: articles_dir / safe_filename(article_title(url)) for url in pages}
    jobs = list(pages.items())
    worker_count = workers if workers is not None else min(MAX_WIKIMEDIA_CONCURRENCY, max(1, len(jobs)))
    worker_count = min(worker_count, MAX_WIKIMEDIA_CONCURRENCY, max(1, len(jobs)))
    log(f"Rendering {len(jobs)} PDFs with {worker_count} workers...", flush=True)

    def render_one(item):
        url, (content, _) = item
        pdf = pdf_by_url[url]
        rendered = rewrite_media(rewrite_links(content, pdf_by_url, pdf), MEDIA_CACHE_DIR)
        render_pdf(article_title(url), rendered, pdf)
        return url

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {executor.submit(render_one, item): item[0] for item in jobs}
        for index, future in enumerate(as_completed(futures), 1):
            url = futures[future]
            future.result()
            log(f"Rendered PDF {index}/{len(jobs)}: {ascii_safe(article_title(url))}", flush=True)
    for subject, urls in direct_by_subject.items():
        subject_dir = output / subject
        subject_dir.mkdir(parents=True, exist_ok=True)
        for url in urls:
            if url in pdf_by_url:
                create_shortcut(pdf_by_url[url], subject_dir / pdf_by_url[url].name)
    log(f"Done: {len(pages)} PDF(s) created in {articles_dir}.", flush=True)
    return len(pages)
