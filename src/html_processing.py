import hashlib
import html
import json
import re
import sys
import threading
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from .config import CACHE_MAX_AGE_SECONDS, DEFAULT_MEDIA_REQUEST_DELAY, MEDIA_CACHE_DIR, SUPPORTED_TAGS, USER_AGENT, VOID_TAGS
from .rate_limit import RequestLimiter
from .utils import ascii_safe
from .wikipedia import canonical_url, load_processed_article, store_processed_article


class ArticleParser(HTMLParser):
    def __init__(self, source_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.source_url = source_url
        self.parts: list[str] = []
        self.links: set[str] = set()
        self.active = False
        self.skip_depth = 0
        self.open_tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attributes = dict(attrs)
        if tag in {"script", "style", "noscript", "table", "thead", "tbody", "tfoot", "tr", "td", "th"}:
            self.skip_depth = 1
            return
        if self.skip_depth:
            if tag not in VOID_TAGS:
                self.skip_depth += 1
            return
        classes = attributes.get("class", "") or ""
        if attributes.get("id") == "mw-content-text" or "mw-parser-output" in classes:
            self.active = True
        if not self.active or tag in {"html", "body", "main"} or tag not in SUPPORTED_TAGS:
            return
        safe = ""
        if tag == "a":
            target = canonical_url(attributes.get("href", ""), self.source_url) if attributes.get("href") else ""
            if target:
                self.links.add(target)
                escaped = html.escape(target, quote=True)
                safe = f' href="{escaped}" data-wikipedia-url="{escaped}"'
            else:
                safe = ' href=""'
        elif tag == "img" and attributes.get("src"):
            safe = f' src="{html.escape(urljoin(self.source_url, attributes["src"]), quote=True)}"'
        self.parts.append(f"<{tag}{safe}>")
        if tag not in VOID_TAGS:
            self.open_tags.append(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.skip_depth:
            self.skip_depth -= 1
            return
        if not self.active or tag not in SUPPORTED_TAGS or tag in VOID_TAGS:
            return
        if tag in self.open_tags:
            while self.open_tags:
                current = self.open_tags.pop()
                self.parts.append(f"</{current}>")
                if current == tag:
                    break

    def handle_data(self, data: str) -> None:
        if self.active and not self.skip_depth:
            self.parts.append(html.escape(data))

    def close(self) -> None:
        super().close()
        while self.open_tags:
            self.parts.append(f"</{self.open_tags.pop()}>")


def parse_article(source_url: str, article_html: str, cache_dir: Path | None = None) -> tuple[str, set[str]]:
    if cache_dir is not None:
        cached = load_processed_article(source_url, cache_dir)
        if cached is not None:
            return cached
    parser = ArticleParser(source_url)
    parser.feed(article_html)
    parser.close()
    content = "".join(parser.parts)
    if not content.strip():
        raise ValueError("Wikipedia article content could not be found")
    if cache_dir is not None:
        store_processed_article(source_url, content, parser.links, cache_dir)
    return content, parser.links

_MEDIA_REQUEST_LIMITER = RequestLimiter(DEFAULT_MEDIA_REQUEST_DELAY)
_MEDIA_LOCK = threading.Lock()
_MEDIA_IN_FLIGHT: dict[str, threading.Event] = {}
_MEDIA_FAILURES = {400, 401, 403, 404, 410, 451}


def fetch_media(url: str, cache_dir: Path = MEDIA_CACHE_DIR) -> Path:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}:
        suffix = ".bin"
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    cache_file = cache_dir / f"{key}{suffix}"
    failure_file = cache_dir / f"{key}.failure"
    try:
        if time.time() - cache_file.stat().st_mtime <= CACHE_MAX_AGE_SECONDS:
            return cache_file
    except (FileNotFoundError, OSError):
        pass
    try:
        if time.time() - failure_file.stat().st_mtime <= CACHE_MAX_AGE_SECONDS:
            raise HTTPError(url, int(failure_file.read_text(encoding="utf-8") or "404"), "Cached media failure", {}, None)
    except (FileNotFoundError, OSError, ValueError):
        try:
            failure_file.unlink()
        except FileNotFoundError:
            pass
    with _MEDIA_LOCK:
        event = _MEDIA_IN_FLIGHT.get(url)
        if event is None:
            event = threading.Event()
            _MEDIA_IN_FLIGHT[url] = event
            owner = True
        else:
            owner = False
    if not owner:
        event.wait()
        return fetch_media(url, cache_dir)
    try:
        for attempt in range(4):
            _MEDIA_REQUEST_LIMITER.wait()
            try:
                request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"})
                with urlopen(request, timeout=30) as response:  # noqa: S310
                    data = response.read()
                    content_type = response.headers.get_content_type()
                if content_type not in {"image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml"}:
                    raise ValueError(f"unexpected image content type: {content_type}")
                cache_dir.mkdir(parents=True, exist_ok=True)
                temporary_file = cache_file.with_suffix(cache_file.suffix + ".tmp")
                temporary_file.write_bytes(data)
                temporary_file.replace(cache_file)
                return cache_file
            except HTTPError as exc:
                if exc.code in _MEDIA_FAILURES:
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    temporary_failure = failure_file.with_suffix(".tmp")
                    temporary_failure.write_text(str(exc.code), encoding="utf-8")
                    temporary_failure.replace(failure_file)
                    raise
                if exc.code not in {429, 500, 502, 503, 504} or attempt == 3:
                    raise
                time.sleep(min(60.0, 2.0 ** attempt))
    finally:
        with _MEDIA_LOCK:
            _MEDIA_IN_FLIGHT.pop(url, None)
            event.set()


def rewrite_media(content: str, media_directory: Path = MEDIA_CACHE_DIR) -> str:
    def cached_image(match: re.Match[str]) -> str:
        url = html.unescape(match.group(1))
        try:
            media_path = fetch_media(url, media_directory)
            if media_path.suffix.lower() != ".svg":
                from PIL import Image
                with Image.open(media_path) as image:
                    image.verify()
            return f' src="{html.escape(str(media_path), quote=True)}"'
        except Exception as exc:
            print(f"  WARNING: image unavailable: {ascii_safe(url)} ({ascii_safe(repr(exc))})", file=sys.stderr, flush=True)
            return ""
    return re.sub(r' src="([^"]+)"', cached_image, content)


def rewrite_links(content: str, pdf_by_url: dict[str, Path], source_pdf: Path) -> str:
    def local_link(match: re.Match[str]) -> str:
        pdf = pdf_by_url.get(match.group(1))
        if not pdf:
            return ' href=""'
        relative_path = Path(__import__("os").path.relpath(pdf, start=source_pdf.parent)).as_posix()
        return f' href="{html.escape(relative_path, quote=True)}"'
    rewritten = re.sub(r' href="[^"]*" data-wikipedia-url="([^"]+)"', local_link, content)
    return re.sub(r' data-wikipedia-url="[^"]+"', "", rewritten)
