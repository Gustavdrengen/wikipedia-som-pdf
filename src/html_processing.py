import hashlib
import html
import json
import os
import re
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from .config import CACHE_MAX_AGE_SECONDS, MAX_RETRY_WAIT_SECONDS, MEDIA_CACHE_DIR, SUPPORTED_TAGS, USER_AGENT, VOID_TAGS
from .rate_limit import GLOBAL_REQUEST_LIMITER, wait_with_progress
from .sites import canonical_url, site_for
from .utils import ascii_safe


_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg"})
# Page furniture that adds noise rather than information. Content tables on
# MediaWiki are almost always marked "wikitable"; infoboxes, navboxes, message
# boxes and table-of-contents boxes are dropped instead of rendered.
_NOISE_TABLE_CLASSES = frozenset({
    "ambox", "catlinks", "cmbox", "fmbox", "gallery", "hatnote", "imbox",
    "infobox", "mbox", "messagebox", "metadata", "navbox", "notice", "ombox",
    "portal", "quotebox", "shortcut", "side-box", "sidebar", "sistersitebox",
    "succession-box", "tmbox", "toc", "vertical-navbox",
})
_CELL_ALIGNMENTS = frozenset({"left", "center", "right", "justify"})


@dataclass
class _Cell:
    tag: str
    attributes: str
    parts: list[str] = field(default_factory=list)
    need_space: bool = False
    nested_tables: int = 0


def _cell_attributes(attributes: dict[str, str | None]) -> str:
    """Keep only the cell attributes fpdf2 understands and can render well."""
    rendered = []
    for name in ("colspan", "rowspan"):
        value = (attributes.get(name) or "").strip()
        if value.isdigit() and 2 <= int(value) <= 64:
            rendered.append(f' {name}="{int(value)}"')
    align = (attributes.get("align") or "").strip().lower()
    if align in _CELL_ALIGNMENTS:
        rendered.append(f' align="{align}"')
    return "".join(rendered)


def _is_noise_table(attributes: dict[str, str | None]) -> bool:
    classes = {token.lower() for token in (attributes.get("class") or "").split()}
    return bool(classes & _NOISE_TABLE_CLASSES) or (attributes.get("role") or "").strip().lower() == "presentation"


class ArticleParser(HTMLParser):
    def __init__(self, source_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.source_url = source_url
        self.site = site_for(source_url)
        self.parts: list[str] = []
        self.links: set[str] = set()
        self.active = False
        self._skip_stack: list[str] = []
        self._table_depth = 0
        self._table_start: int | None = None
        self._table_rows = 0
        self._row_start: int | None = None
        self._row_has_cell = False
        self._cell: _Cell | None = None
        self._cell_skip = 0
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

    def _push_skip(self, tag: str) -> None:
        if tag not in VOID_TAGS:
            self._skip_stack.append(tag)

    def _pop_skip(self, tag: str) -> None:
        # Skipped containers may hold arbitrarily nested elements (tables in
        # particular). Only the matching end tag closes one, and any end tags in
        # between must not end the skip early.
        if tag in VOID_TAGS:
            return
        for index in range(len(self._skip_stack) - 1, -1, -1):
            if self._skip_stack[index] == tag:
                del self._skip_stack[index:]
                return

    def _begin_cell(self, tag: str, attributes: dict[str, str | None]) -> None:
        if self._cell is not None:
            self._finish_cell()
        self._cell = _Cell(tag, _cell_attributes(attributes))
        self._row_has_cell = True

    def _finish_cell(self) -> None:
        cell = self._cell
        self._cell = None
        if cell is None:
            return
        text = re.sub(r"\s+", " ", "".join(cell.parts)).strip()
        self.parts.append(f"<{cell.tag}{cell.attributes}>{html.escape(text)}</{cell.tag}>")

    def _handle_cell_start(self, tag: str, attributes: dict[str, str | None]) -> None:
        cell = self._cell
        assert cell is not None
        if self._cell_skip:
            if tag not in VOID_TAGS:
                self._cell_skip += 1
            return
        if tag in _SKIP_TAGS or self._is_math_start(tag, attributes):
            # Cells are plain text for fpdf2, so math and embedded markup are
            # dropped instead of leaking MathML/script tokens into the cell.
            if tag not in VOID_TAGS:
                self._cell_skip = 1
            cell.need_space = True
            return
        if tag == "table":
            cell.nested_tables += 1
            cell.need_space = True
            return
        if tag == "tr":
            cell.need_space = True
            return
        if tag in {"td", "th"}:
            if cell.nested_tables:
                if cell.parts:
                    cell.parts.append(" | ")
            cell.need_space = False
            return
        if tag == "br":
            cell.parts.append(" ")
            return
        cell.need_space = True

    def _handle_cell_end(self, tag: str) -> None:
        cell = self._cell
        assert cell is not None
        if self._cell_skip:
            if tag not in VOID_TAGS:
                self._cell_skip -= 1
            return
        if cell.nested_tables:
            if tag == "table":
                cell.nested_tables -= 1
                cell.need_space = True
            return
        if tag == "table":
            # Malformed table with an open cell: close both instead of leaving
            # unbalanced markup behind for the PDF renderer.
            self._finish_cell()
            self._end_table()
            return
        if tag in {"td", "th"}:
            self._finish_cell()
            return
        cell.need_space = True

    def _handle_cell_data(self, data: str) -> None:
        if self._cell_skip or self._cell is None:
            return
        text = re.sub(r"\s+", " ", data).strip()
        if not text:
            return
        if self._cell.parts and self._cell.need_space:
            self._cell.parts.append(" ")
        self._cell.parts.append(text)
        self._cell.need_space = False

    def _start_table(self, attributes: dict[str, str | None]) -> None:
        if self._table_depth or _is_noise_table(attributes):
            self._skip_stack.append("table")
            return
        self._table_start = len(self.parts)
        self.parts.append('<table width="100%" border="1">')
        self._table_depth = 1
        self._table_rows = 0

    def _close_row(self) -> None:
        if self._row_start is None:
            return
        if self._row_has_cell:
            self.parts.append("</tr>")
            self._table_rows += 1
        else:
            # An empty <tr> only confuses fpdf2's layout; drop it.
            del self.parts[self._row_start]
        self._row_start = None
        self._row_has_cell = False

    def _end_table(self) -> None:
        self._close_row()
        if self._table_rows == 0:
            if self._table_start is not None:
                del self.parts[self._table_start]
        else:
            self.parts.append("</table>")
        self._table_depth = 0
        self._table_start = None
        self._table_rows = 0

    def _handle_table_start(self, tag: str, attributes: dict[str, str | None]) -> None:
        if tag == "table":
            self._skip_stack.append("table")
            return
        if tag == "tr":
            self._close_row()
            self._row_start = len(self.parts)
            self._row_has_cell = False
            self.parts.append("<tr>")
            return
        if tag in {"td", "th"} and self._row_start is not None:
            self._begin_cell(tag, attributes)

    def _handle_table_end(self, tag: str) -> None:
        if tag in {"tr", "table"} and self._cell is not None:
            # Tolerate a missing </td> rather than emitting unbalanced table
            # markup that would make fpdf2 raise.
            self._finish_cell()
        if tag == "table":
            self._end_table()
        elif tag == "tr":
            self._close_row()
        elif tag in {"td", "th"} and self._cell is not None:
            self._finish_cell()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attributes = dict(attrs)
        if self._math_capture is not None:
            # Inside a math block every element contributes to the nesting depth,
            # whether or not it is one of the supported output tags.
            self._handle_math_tag(tag, attributes)
            return
        if self._skip_stack:
            self._push_skip(tag)
            return
        if self._cell is not None:
            self._handle_cell_start(tag, attributes)
            return
        if tag in _SKIP_TAGS:
            self._push_skip(tag)
            return
        if self.site is not None and self.site.is_content_start(attributes):
            self.active = True
        if not self.active:
            return
        if tag == "table":
            self._start_table(attributes)
            return
        if self._table_depth:
            self._handle_table_start(tag, attributes)
            return
        if self._is_math_start(tag, attributes):
            self._begin_math_capture()
            self._handle_math_tag(tag, attributes)
            if tag == "img":
                # A bare math <img> (no wrapping <span>): void, so close immediately.
                self._finish_math_capture()
            return
        if tag in {"html", "body", "main"} or tag not in SUPPORTED_TAGS:
            return
        safe = ""
        if tag == "a":
            href = (attributes.get("href") or "").strip()
            target = canonical_url(href, self.source_url) if href else ""
            if target:
                self.links.add(target)
                escaped = html.escape(target, quote=True)
                safe = f' href="{escaped}" data-article-url="{escaped}"'
            elif href:
                # Keep ordinary web links clickable in the PDF/EPUB instead of
                # degrading every source citation to plain text.
                absolute = urljoin(self.source_url, href)
                if urlparse(absolute).scheme in {"http", "https"}:
                    safe = f' href="{html.escape(absolute, quote=True)}"'
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
        if self._skip_stack:
            self._pop_skip(tag)
            return
        if self._cell is not None:
            self._handle_cell_end(tag)
            return
        if not self.active:
            return
        if self._table_depth:
            self._handle_table_end(tag)
            return
        if tag not in SUPPORTED_TAGS or tag in VOID_TAGS:
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
        elif self._skip_stack:
            return
        elif self._cell is not None:
            self._handle_cell_data(data)
        elif self._table_depth:
            return
        elif self.active:
            self.parts.append(html.escape(data))

    def close(self) -> None:
        super().close()
        if self._math_capture is not None:
            # Unbalanced math block: salvage what was captured rather than
            # swallowing the rest of the document.
            self._finish_math_capture()
        if self._cell is not None:
            self._finish_cell()
        if self._table_depth:
            self._end_table()
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
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",

}
# Bound the in-memory index so a long-running process that touches thousands of
# distinct images does not grow without limit.
_MEDIA_CACHE_MAX_ENTRIES = 512
_MEDIA_CACHE_INDEX: OrderedDict[tuple[str, str], Path] = OrderedDict()
_MEDIA_INDEX_LOCK = threading.Lock()
_IMAGE_SIZES: dict[str, tuple[int, int]] = {}
_IMAGE_SIZE_LOCK = threading.Lock()
# Images are placed at 72 dpi, so pixel size maps 1:1 to points. Wide or tall
# photos would otherwise run off the page or consume it entirely.
_MAX_IMAGE_WIDTH_PT = 520.0
_MAX_IMAGE_HEIGHT_PT = 520.0


def _media_index_get(cache_key: tuple[str, str]) -> Path | None:
    with _MEDIA_INDEX_LOCK:
        path = _MEDIA_CACHE_INDEX.get(cache_key)
        if path is not None:
            if path.is_file():
                _MEDIA_CACHE_INDEX.move_to_end(cache_key)
                return path
            _MEDIA_CACHE_INDEX.pop(cache_key, None)
    return None


def _media_index_set(cache_key: tuple[str, str], path: Path) -> None:
    with _MEDIA_INDEX_LOCK:
        _MEDIA_CACHE_INDEX[cache_key] = path
        _MEDIA_CACHE_INDEX.move_to_end(cache_key)
        while len(_MEDIA_CACHE_INDEX) > _MEDIA_CACHE_MAX_ENTRIES:
            _MEDIA_CACHE_INDEX.popitem(last=False)


def _image_size_attributes(path: Path) -> str:
    """Return page-width-capped width/height attributes for an image, if known."""
    key = str(path.resolve())
    with _IMAGE_SIZE_LOCK:
        size = _IMAGE_SIZES.get(key)
    if size is None:
        try:
            from PIL import Image

            with Image.open(path) as image:
                size = image.size
        except Exception:
            return ""
        with _IMAGE_SIZE_LOCK:
            _IMAGE_SIZES[key] = size
    width, height = size
    if width <= 0 or height <= 0:
        return ""
    scale = min(1.0, _MAX_IMAGE_WIDTH_PT / width, _MAX_IMAGE_HEIGHT_PT / height)
    return f' width="{width * scale:.1f}" height="{height * scale:.1f}"'


def fetch_media(url: str, cache_dir: Path = MEDIA_CACHE_DIR, referer: str | None = None) -> Path:
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    cache_key = (str(cache_dir.resolve()), url)
    cached_path = _media_index_get(cache_key)
    if cached_path is not None:
        return cached_path
    failure_file = cache_dir / f"{key}.media-failure"
    for suffix in _MEDIA_SUFFIXES.values():
        cache_file = cache_dir / f"{key}{suffix}"
        try:
            if time.time() - cache_file.stat().st_mtime <= CACHE_MAX_AGE_SECONDS:
                _media_index_set(cache_key, cache_file)
                return cache_file
        except (FileNotFoundError, OSError):
            continue

    cached_status: int | None = None
    try:
        if time.time() - failure_file.stat().st_mtime <= CACHE_MAX_AGE_SECONDS:
            cached_status = int(failure_file.read_text(encoding="utf-8") or "404")
    except (OSError, ValueError):
        cached_status = None
    if cached_status is not None:
        raise HTTPError(url, cached_status, "Cached media failure", {}, None)
    # The failure marker is stale or unreadable; let this attempt replace it.
    try:
        failure_file.unlink()
    except OSError:
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
                _media_index_set(cache_key, cache_file)
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
            # Math images already arrive with point-sized width/height attributes,
            # so re-measuring them here would scale the same formula twice.
            _media_index_set((str(media_directory.resolve()), url), Path(url))
            return f' src="{html.escape(url, quote=True)}"'
        media_site = site_for(url) or site
        if media_site is not None and not media_site.accepts_media(url):
            return ""
        # Every article that references this image keeps it: fetch_media already
        # dedupes concurrent and repeated work through the media cache index and
        # in-flight events, so no extra per-run bookkeeping is needed here.
        try:
            media_path = fetch_media(url, media_directory, source_url)
            if media_path.suffix.lower() == ".svg":
                return ""
            dimensions = _image_size_attributes(media_path)
            return f' src="{html.escape(str(media_path), quote=True)}"{dimensions}'
        except Exception as exc:
            print(f"  WARNING: image unavailable: {ascii_safe(url)} ({ascii_safe(repr(exc))})", file=sys.stderr, flush=True)
            return ""
    return re.sub(r' src="([^"]+)"', cached_image, content)


def rewrite_links(content: str, pdf_by_url: dict[str, Path], source_pdf: Path) -> str:
    def local_link(match: re.Match[str]) -> str:
        article_url = html.unescape(match.group(1))
        pdf = pdf_by_url.get(article_url)
        if pdf is None:
            # The article was not downloaded, but the original web address is
            # still useful: keep it as a clickable external link.
            return f' href="{html.escape(article_url, quote=True)}"'
        relative_path = Path(os.path.relpath(pdf, start=source_pdf.parent)).as_posix()
        return f' href="{html.escape(relative_path, quote=True)}"'
    rewritten = re.sub(r' href="[^"]*" data-article-url="([^"]+)"', local_link, content)
    return re.sub(r' data-article-url="[^"]+"', "", rewritten)
