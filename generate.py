#!/usr/bin/env python3
"""Generate an interconnected offline Wikipedia PDF collection."""

from __future__ import annotations

import argparse
import hashlib
import html
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlparse, urlunparse
from urllib.error import HTTPError
from urllib.request import Request, urlopen

USER_AGENT = "WikipediaArticlePdfGenerator/1.0 (https://github.com/your-name/wikipedia-pdf-generator; personal use)"
MAX_RETRIES = 5
CACHE_MAX_AGE_SECONDS = 31 * 24 * 60 * 60
CACHE_DIR = Path(".wikipedia-cache")
MEDIA_CACHE_DIR = Path(".wikipedia-media-cache")
DEFAULT_REQUEST_DELAY = 1.0
SKIP_NAMESPACES = {"category", "file", "help", "special", "template", "talk", "portal", "wikipedia", "module", "book", "draft", "mediawiki", "timedtext", "topic", "user", "education program", "gadget", "gadget definition"}
SUPPORTED_TAGS = {"p", "br", "hr", "b", "i", "s", "u", "font", "center", "a", "pre", "code", "ol", "ul", "li", "dl", "dt", "dd", "table", "thead", "tbody", "tfoot", "tr", "th", "td", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "sup", "sub", "img"}
VOID_TAGS = {"br", "hr", "img"}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def ascii_safe(value: str) -> str:
    return value.encode("ascii", errors="backslashreplace").decode("ascii")


def read_master_file(path: Path) -> dict[str, set[str]]:
    subjects: dict[str, set[str]] = {}
    current: str | None = None
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw_line.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        if line.endswith(":"):
            current = line[:-1].strip()
            if not current:
                raise ValueError(f"Line {line_number}: subject name is empty")
            subjects.setdefault(current, set())
        elif current is None:
            raise ValueError(f"Line {line_number}: URL appears before a subject header")
        elif not line.startswith(("http://", "https://")):
            raise ValueError(f"Line {line_number}: expected an HTTP(S) URL")
        else:
            subjects[current].add(line)
    if not subjects:
        raise ValueError("No subjects found in the master file")
    return subjects


def canonical_url(url: str, base: str = "https://en.wikipedia.org/wiki/") -> str:
    parsed = urlparse(urljoin(base, url))
    if parsed.scheme not in {"http", "https"} or not (parsed.hostname or "").lower().endswith("wikipedia.org") or "/wiki/" not in parsed.path:
        return ""
    title = unquote(parsed.path.split("/wiki/", 1)[1]).replace("_", " ").strip()
    namespace = title.split(":", 1)[0].lower() if ":" in title else ""
    if not title or namespace in SKIP_NAMESPACES:
        return ""
    return urlunparse(("https", parsed.hostname.lower(), "/wiki/" + title.replace(" ", "_"), "", parsed.query, ""))


def article_title(url: str) -> str:
    return unquote(urlparse(url).path.split("/wiki/", 1)[-1]).replace("_", " ").strip() or "Wikipedia article"


def safe_filename(title: str) -> str:
    cleaned = re.sub(r"[^\w. -]+", "", title, flags=re.UNICODE)
    return (re.sub(r"\s+", " ", cleaned).strip(" .") or "article")[:180] + ".pdf"


def clean_response_cache(cache_dir: Path, now: float | None = None) -> None:
    now = time.time() if now is None else now
    cache_dir.mkdir(parents=True, exist_ok=True)
    for cache_file in cache_dir.glob("*.html"):
        try:
            if now - cache_file.stat().st_mtime > CACHE_MAX_AGE_SECONDS:
                cache_file.unlink()
        except FileNotFoundError:
            continue


def fetch_media(url: str, cache_dir: Path = MEDIA_CACHE_DIR) -> Path:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}:
        suffix = ".bin"
    cache_file = cache_dir / f"{hashlib.sha256(url.encode('utf-8')).hexdigest()}{suffix}"
    if cache_file.exists() and time.time() - cache_file.stat().st_mtime <= CACHE_MAX_AGE_SECONDS:
        return cache_file
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


def fetch_article(url: str, cache_dir: Path = CACHE_DIR) -> tuple[str, bool]:
    cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    cache_file = cache_dir / f"{cache_key}.html"
    try:
        if time.time() - cache_file.stat().st_mtime <= CACHE_MAX_AGE_SECONDS:
            print(f"Using cached article {ascii_safe(article_title(url))}", flush=True)
            return cache_file.read_text(encoding="utf-8"), True
    except (FileNotFoundError, OSError, UnicodeError):
        pass
    parsed = urlparse(url)
    title = unquote(parsed.path.split("/wiki/", 1)[1])
    api_url = urlunparse((parsed.scheme, parsed.netloc, "/api/rest_v1/page/html/" + quote(title, safe=""), "", "", ""))
    for attempt in range(MAX_RETRIES + 1):
        request = Request(api_url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
        try:
            with urlopen(request, timeout=30) as response:  # noqa: S310
                content = response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace")
            cache_dir.mkdir(parents=True, exist_ok=True)
            temporary_file = cache_file.with_suffix(".tmp")
            temporary_file.write_text(content, encoding="utf-8")
            temporary_file.replace(cache_file)
            return content, False
        except HTTPError as exc:
            if exc.code not in {429, 503} or attempt >= MAX_RETRIES:
                raise
            retry_after = exc.headers.get("Retry-After")
            try:
                wait_seconds = max(5.0, float(retry_after)) if retry_after else 5.0 * (2 ** attempt)
            except ValueError:
                wait_seconds = 5.0 * (2 ** attempt)
            wait_seconds = min(wait_seconds, 300.0)
            print(f"  HTTP {exc.code}; retrying in {wait_seconds:g}s", flush=True)
            time.sleep(wait_seconds)
    raise RuntimeError(f"Could not fetch {url}")


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
        if tag in {"script", "style", "noscript", "br", "table", "thead", "tbody", "tfoot", "tr", "td", "th"}:
            self.skip_depth = 1
            return
        if self.skip_depth:
            if tag not in VOID_TAGS:
                self.skip_depth += 1
            return
        classes = attributes.get("class", "") or ""
        if attributes.get("id") == "mw-content-text" or "mw-parser-output" in classes:
            self.active = True
        if not self.active or tag in {"html", "body", "main"}:
            return
        if tag not in SUPPORTED_TAGS:
            return
        safe = ""
        if tag == "a":
            target = canonical_url(attributes.get("href", ""), self.source_url) if attributes.get("href") else ""
            if target:
                self.links.add(target)
                safe = f' href="{html.escape(target, quote=True)}" data-wikipedia-url="{html.escape(target, quote=True)}"'
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


def parse_article(source_url: str, article_html: str) -> tuple[str, set[str]]:
    parser = ArticleParser(source_url)
    parser.feed(article_html)
    parser.close()
    content = "".join(parser.parts)
    if not content.strip():
        raise ValueError("Wikipedia article content could not be found")
    return content, parser.links


def rewrite_media(content: str, media_directory: Path) -> str:
    def cached_image(match: re.Match[str]) -> str:
        url = html.unescape(match.group(1))
        try:
            media_path = fetch_media(url, media_directory)
            from PIL import Image
            with Image.open(media_path) as image:
                image.verify()
            return f' src="{html.escape(str(media_path), quote=True)}"'
        except Exception as exc:
            print(f"  WARNING: image unavailable: {ascii_safe(url)} ({ascii_safe(repr(exc))})", file=sys.stderr, flush=True)
            return ""
    return re.sub(r' src="([^"]+)"', cached_image, content)


def rewrite_links(content: str, pdf_by_url: dict[str, Path], output_directory: Path, source_pdf: Path | None = None) -> str:
    def local_link(match: re.Match[str]) -> str:
        pdf = pdf_by_url.get(match.group(1))
        if not pdf:
            return ' href=""'
        base = source_pdf.parent if source_pdf is not None else output_directory
        relative_path = os.path.relpath(pdf, start=base).replace(os.sep, "/")
        return f' href="{html.escape(relative_path, quote=True)}"'
    rewritten = re.sub(r' href="[^"]*" data-wikipedia-url="([^"]+)"', local_link, content)
    return re.sub(r' data-wikipedia-url="[^"]+"', "", rewritten)


@lru_cache(maxsize=1)
def find_unicode_font() -> Path:
    candidates = [
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "arial.ttf",
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "segoeui.ttf",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/dejavu/DejaVuSans.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise RuntimeError("No Unicode TrueType font found. Install a system font such as Arial or DejaVu Sans.")


def render_pdf(title: str, content: str, output: Path) -> None:
    try:
        from fpdf import FPDF
    except ImportError as exc:
        raise RuntimeError("fpdf2 is unavailable. Install requirements.txt.") from exc

    class ArticlePDF(FPDF):
        def footer(self) -> None:
            self.set_y(-15)
            self.set_font("ArticleFont", size=9)
            self.set_text_color(119, 119, 119)
            self.cell(0, 10, f"Page {self.page_no()}", align="C")

    pdf = ArticlePDF()
    font_path = find_unicode_font()
    for style in ("", "B", "I", "BI"):
        pdf.add_font("ArticleFont", style=style, fname=str(font_path))
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    pdf.set_title(title)
    pdf.set_font("ArticleFont", "B", 22)
    pdf.set_text_color(24, 33, 43)
    pdf.multi_cell(0, 12, title)
    pdf.set_draw_color(59, 110, 165)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(6)
    pdf.set_font("ArticleFont", size=10)
    pdf.set_text_color(32, 33, 36)
    pdf.write_html(content)
    pdf.output(output)


def create_shortcut(target: Path, shortcut: Path) -> None:
    link_path = shortcut.with_suffix(".lnk") if os.name == "nt" else shortcut
    if link_path.exists() or link_path.is_symlink():
        link_path.unlink()
    if os.name == "nt":
        script = "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('" + str(link_path).replace("'", "''") + "');$s.TargetPath='" + str(target.resolve()).replace("'", "''") + "';$s.WorkingDirectory='" + str(target.parent.resolve()).replace("'", "''") + "';$s.Save()"
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script], check=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
    else:
        link_path.symlink_to(os.path.relpath(target, link_path.parent))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("master_file", type=Path)
    parser.add_argument("--output", type=Path, default=Path("Noter"))
    parser.add_argument("--workers", type=int, default=None, help="Number of PDFs to render concurrently (default: up to 4)")
    parser.add_argument("--request-delay", type=float, default=DEFAULT_REQUEST_DELAY, help="Seconds to wait between article downloads (default: 1)")
    args = parser.parse_args()
    if args.workers is not None and args.workers < 1:
        parser.error("--workers must be at least 1")
    if args.request_delay < 0:
        parser.error("--request-delay cannot be negative")
    clean_response_cache(CACHE_DIR)
    subjects = read_master_file(args.master_file)
    direct_by_subject = {subject: {canonical_url(url) for url in urls if canonical_url(url)} for subject, urls in subjects.items()}
    root_urls = set().union(*direct_by_subject.values()) if direct_by_subject else set()
    pages: dict[str, tuple[str, set[str]]] = {}
    pending = list(root_urls)
    discovered: set[str] = set()
    while pending:
        url = pending.pop(0)
        print(f"Downloading {ascii_safe(article_title(url))}", flush=True)
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
            print(f"  ERROR: {ascii_safe(repr(exc))}", file=sys.stderr, flush=True)
        if not cache_hit and args.request_delay:
            time.sleep(args.request_delay)
    articles_dir = args.output / "artikler"
    articles_dir.mkdir(parents=True, exist_ok=True)
    pdf_by_url = {url: articles_dir / safe_filename(article_title(url)) for url in pages}
    jobs = list(pages.items())
    workers = args.workers if args.workers is not None else min(4, max(1, len(jobs)))
    workers = min(workers, max(1, len(jobs)))
    print(f"Rendering {len(jobs)} PDFs with {workers} workers...", flush=True)

    def render_one(item):
        url, (content, _) = item
        output = pdf_by_url[url]
        rendered_content = rewrite_media(rewrite_links(content, pdf_by_url, articles_dir, output), MEDIA_CACHE_DIR)
        render_pdf(article_title(url), rendered_content, output)
        return url

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(render_one, item): item[0] for item in jobs}
        for index, future in enumerate(as_completed(futures), 1):
            url = futures[future]
            future.result()
            print(f"Rendered PDF {index}/{len(jobs)}: {ascii_safe(article_title(url))}", flush=True)
    for subject, urls in direct_by_subject.items():
        subject_dir = args.output / subject
        subject_dir.mkdir(parents=True, exist_ok=True)
        for url in urls:
            if url in pdf_by_url:
                create_shortcut(pdf_by_url[url], subject_dir / pdf_by_url[url].name)
    print(f"Done: {len(pages)} PDF(s) created in {articles_dir}.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
