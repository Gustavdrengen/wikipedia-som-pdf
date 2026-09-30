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

from .config import CACHE_MAX_AGE_SECONDS, MAX_RETRY_WAIT_SECONDS, MEDIA_CACHE_DIR, SUPPORTED_TAGS, USER_AGENT, VOID_TAGS
from .rate_limit import GLOBAL_REQUEST_LIMITER, wait_with_progress
from .sites import canonical_url, site_for
from .utils import ascii_safe


class ArticleParser(HTMLParser):
    def __init__(self, source_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.source_url = source_url
        self.site = site_for(source_url)
        self.parts: list[str] = []
        self.links: set[str] = set()
        self.active = False
        self.skip_depth = 0
        self.open_tags: list[str] = []
        # State while consuming a MediaWiki math block (<span class="mwe-math...">
        # wrapping hidden MathML + an SVG fallback <img>). Math is never rendered
        # remotely: we capture the TeX source and emit a data-math placeholder that
        # the media-preparation step replaces with a locally rendered image.
        self._math_capture: dict | None = None

    @staticmethod
    def _is_math_start(tag: str, attributes: dict[str, str | None]) -> bool:
        if tag == "math":
            return True
        classes = attributes.get("class", "") or ""
        if "mwe-math" in classes:
            return True
        if tag == "img":
            src = (attributes.get("src") or "").lower()
            return "math/render/" in src
        return False

    def _begin_math_capture(self) -> None:
        self._math_capture = {
            "depth": 0,
            "annotation_open": False,
            "annotation": [],
            "tokens": [],
            "display": False,
            "alt": "",
        }

    def _handle_math_tag(self, tag: str, attributes: dict[str, str | None]) -> None:
        capture = self._math_capture
        if tag not in VOID_TAGS:
            capture["depth"] += 1
        classes = attributes.get("class", "") or ""
        if tag == "math" and (attributes.get("display") or "").lower() == "block":
            capture["display"] = True
        if "mwe-math-mathml-display" in classes or "mwe-math-element-display" in classes:
            capture["display"] = True
        if tag == "annotation" and (attributes.get("encoding") or "") == "application/x-tex":
            capture["annotation_open"] = True
        if tag == "img":
            alt = attributes.get("alt")
            if alt and not capture["alt"]:
                capture["alt"] = alt

    def _handle_math_end(self, tag: str) -> None:
        capture = self._math_capture
        if tag == "annotation":
            capture["annotation_open"] = False
        if tag not in VOID_TAGS:
            capture["depth"] -= 1
            if capture["depth"] <= 0:
                self._finish_math_capture()

    def _finish_math_capture(self) -> None:
        capture = self._math_capture
        self._math_capture = None
        if capture is None:
            return
        tex = "".join(capture["annotation"]).strip()
        if not tex and capture["alt"]:
            tex = capture["alt"].strip()
        fallback = "".join(capture["tokens"]).strip()
        if not tex and not fallback:
            return
        display = "1" if capture["display"] else "0"
        self.parts.append(
            f'<img data-math="{html.escape(tex, quote=True)}" '
            f'data-math-fallback="{html.escape(fallback, quote=True)}" '
            f'data-math-display="{display}">'
        )

    def _collect_math_data(self, data: str) -> None:
        capture = self._math_capture
        if capture["annotation_open"]:
            capture["annotation"].append(data)
        else:
            text = data.strip()
            if text:
                capture["tokens"].append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attributes = dict(attrs)
        if self._math_capture is not None:
            # Inside a math block every element contributes to the nesting depth,
            # whether or not it is one of the supported output tags.
            self._handle_math_tag(tag, attributes)
            return
        if tag in {"script", "style", "noscript", "table", "thead", "tbody", "tfoot", "tr", "td", "th"}:
            self.skip_depth = 1
            return
        if self.skip_depth:
            if tag not in VOID_TAGS:
                self.skip_depth += 1
            return
        if self.site is not None and self.site.is_content_start(attributes):
            self.active = True
        if self.active and self._is_math_start(tag, attributes):
            self._begin_math_capture()
            self._handle_math_tag(tag, attributes)
            if tag == "img":
                # A bare math <img> (no wrapping <span>): void, so close immediately.
                self._finish_math_capture()
            return
        if not self.active or tag in {"html", "body", "main"} or tag not in SUPPORTED_TAGS:
            return
        safe = ""
        if tag == "a":
            target = canonical_url(attributes.get("href", ""), self.source_url) if attributes.get("href") else ""
            if target:
                self.links.add(target)
                escaped = html.escape(target, quote=True)
                safe = f' href="{escaped}" data-article-url="{escaped}"'
            else:
                safe = ' href=""'
        elif tag == "img" and attributes.get("src"):
            image_url = urljoin(self.source_url, attributes["src"])
            image_site = site_for(image_url) or self.site
            if image_site is None or image_site.accepts_media(image_url):
                safe = f' src="{html.escape(image_url, quote=True)}"'
            else:
                return
        self.parts.append(f"<{tag}{safe}>")
        if tag not in VOID_TAGS:
            self.open_tags.append(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._math_capture is not None:
            self._handle_math_end(tag)
            return
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
        if self._math_capture is not None:
            self._collect_math_data(data)
        elif self.active and not self.skip_depth:
            self.parts.append(html.escape(data))

    def close(self) -> None:
        super().close()
        if self._math_capture is not None:
            # Unbalanced math block: salvage what was captured rather than
            # swallowing the rest of the document.
            self._finish_math_capture()
        while self.open_tags:
            self.parts.append(f"</{self.open_tags.pop()}>")


def parse_article(source_url: str, article_html: str) -> tuple[str, set[str]]:
    parser = ArticleParser(source_url)
    parser.feed(article_html)
    parser.close()
    content = "".join(parser.parts)
    if not content.strip():
        raise ValueError("Article content could not be found")
    return content, parser.links

_MEDIA_LOCK = threading.Lock()
_MEDIA_IN_FLIGHT: dict[str, threading.Event] = {}
_MEDIA_FAILURES = {400, 401, 403, 404, 410, 451}
_MEDIA_SUFFIXES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",                    "image/webp": ".webp",
    "image/svg+xml": ".svg",

}
_MEDIA_CACHE_INDEX: dict[tuple[str, str], Path] = {}
_MEDIA_SEEN: set[tuple[str, str]] = set()


def fetch_media(url: str, cache_dir: Path = MEDIA_CACHE_DIR, referer: str | None = None) -> Path:
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    cache_key = (str(cache_dir.resolve()), url)
    cached_path = _MEDIA_CACHE_INDEX.get(cache_key)
    if cached_path is not None:
        return cached_path
    failure_file = cache_dir / f"{key}.media-failure"
    for suffix in _MEDIA_SUFFIXES.values():
        cache_file = cache_dir / f"{key}{suffix}"
        try:
            if time.time() - cache_file.stat().st_mtime <= CACHE_MAX_AGE_SECONDS:
                _MEDIA_CACHE_INDEX[cache_key] = cache_file
                return cache_file
        except (FileNotFoundError, OSError):
            continue

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
        return fetch_media(url, cache_dir, referer)
    try:
        retry_waited = 0.0
        for attempt in range(4):
            GLOBAL_REQUEST_LIMITER.wait()
            try:
                headers = {
                    "User-Agent": USER_AGENT,
                    "Accept": "image/png,image/jpeg,image/gif,image/svg+xml,image/*;q=0.8",
                }
                if referer:
                    headers["Referer"] = referer
                request = Request(url, headers=headers)
                with urlopen(request, timeout=30) as response:  # noqa: S310
                    data = response.read()
                    content_type = response.headers.get_content_type()
                GLOBAL_REQUEST_LIMITER.success()
                suffix = _MEDIA_SUFFIXES.get(content_type)
                if suffix is None:
                    raise ValueError(f"unexpected image content type: {content_type}")
                cache_dir.mkdir(parents=True, exist_ok=True)
                cache_file = cache_dir / f"{key}{suffix}"
                temporary_file = cache_file.with_suffix(cache_file.suffix + ".tmp")
                temporary_file.write_bytes(data)
                temporary_file.replace(cache_file)
                try:
                    if suffix != ".svg":
                        from PIL import Image
                        with Image.open(cache_file) as image:
                            image.verify()
                except Exception:
                    cache_file.unlink(missing_ok=True)
                    raise
                _MEDIA_CACHE_INDEX[cache_key] = cache_file
                return cache_file
            except HTTPError as exc:
                if exc.code in _MEDIA_FAILURES or exc.code == 429:
                    if exc.code == 429:
                        retry_after = exc.headers.get("Retry-After")
                        try:
                            cooldown = float(retry_after) if retry_after else 60.0
                        except (TypeError, ValueError):
                            cooldown = 60.0
                        GLOBAL_REQUEST_LIMITER.rate_limited(cooldown)
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    temporary_failure = failure_file.with_suffix(".tmp")
                    temporary_failure.write_text(str(exc.code), encoding="utf-8")
                    temporary_failure.replace(failure_file)
                    raise
                if exc.code not in {500, 502, 503, 504} or attempt == 3:
                    raise
                retry_after = exc.headers.get("Retry-After")
                try:
                    wait_seconds = max(5.0, float(retry_after)) if retry_after else 2.0 ** attempt
                except (TypeError, ValueError):
                    wait_seconds = 2.0 ** attempt
                wait_seconds = min(wait_seconds, MAX_RETRY_WAIT_SECONDS - retry_waited)
                if wait_seconds <= 0:
                    raise
                retry_waited += wait_seconds
                GLOBAL_REQUEST_LIMITER.rate_limited(wait_seconds)
                print(f"  HTTP {exc.code} for {url}; retrying in {wait_seconds:g}s", flush=True)
                wait_with_progress(wait_seconds, f"HTTP {exc.code} retry for {url}", log=lambda message, **_: print(message, flush=True))
    finally:
        with _MEDIA_LOCK:
            _MEDIA_IN_FLIGHT.pop(url, None)
            event.set()


def prepare_media(content: str, source_url: str, media_directory: Path = MEDIA_CACHE_DIR) -> str:
    site = site_for(source_url)

    def cached_image(match: re.Match[str]) -> str:
        url = html.unescape(match.group(1))
        if not urlparse(url).scheme:
            # Already a local file (e.g. a locally rendered math PNG): keep it as-is.
            # The media cache index short-circuit makes it safe to process the same
            # local path repeatedly without re-verifying every occurrence.
            media_key = (str(media_directory.resolve()), url)
            with _MEDIA_LOCK:
                _MEDIA_CACHE_INDEX.setdefault(media_key, Path(url))
            return f' src="{html.escape(url, quote=True)}"'
        media_site = site_for(url) or site
        if media_site is not None and not media_site.accepts_media(url):
            return ""
        media_key = (str(media_directory.resolve()), url)
        with _MEDIA_LOCK:
            if media_key in _MEDIA_SEEN:
                return ""
            _MEDIA_SEEN.add(media_key)
        try:
            media_path = fetch_media(url, media_directory, source_url)
            if media_path.suffix.lower() == ".svg":
                return ""
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
    rewritten = re.sub(r' href="[^"]*" data-article-url="([^"]+)"', local_link, content)
    return re.sub(r' data-article-url="[^"]+"', "", rewritten)
