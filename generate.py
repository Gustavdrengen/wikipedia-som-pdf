#!/usr/bin/env python3
"""Generate an interconnected offline Wikipedia PDF collection."""

from __future__ import annotations

import argparse
import html
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlparse, urlunparse
from urllib.error import HTTPError
from urllib.request import Request, urlopen

USER_AGENT = "WikipediaArticlePdfGenerator/1.0 (https://github.com/your-name/wikipedia-pdf-generator; personal use)"
MAX_RETRIES = 5
SKIP_NAMESPACES = {"category", "file", "help", "special", "template", "talk", "portal", "wikipedia", "module", "book", "draft", "mediawiki", "timedtext", "topic", "user", "education program", "gadget", "gadget definition"}

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


def fetch_article(url: str) -> str:
    parsed = urlparse(url)
    title = unquote(parsed.path.split("/wiki/", 1)[1])
    api_url = urlunparse((parsed.scheme, parsed.netloc, "/api/rest_v1/page/html/" + quote(title, safe=""), "", "", ""))
    for attempt in range(MAX_RETRIES + 1):
        request = Request(api_url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
        try:
            with urlopen(request, timeout=30) as response:  # noqa: S310
                return response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace")
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


class ArticleParser(HTMLParser):
    def __init__(self, source_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.source_url = source_url
        self.parts: list[str] = []
        self.links: set[str] = set()
        self.active = False
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            self.skip_depth = 1
            return
        if self.skip_depth:
            if tag not in {"img", "br", "hr", "meta", "link", "input"}:
                self.skip_depth += 1
            return
        classes = attributes.get("class", "") or ""
        if attributes.get("id") == "mw-content-text" or "mw-parser-output" in classes:
            self.active = True
        if not self.active or tag in {"html", "body", "main"}:
            return
        safe = ""
        if tag == "a" and attributes.get("href"):
            target = canonical_url(attributes["href"], self.source_url)
            if target:
                self.links.add(target)
                safe = f' data-wikipedia-url="{html.escape(target, quote=True)}"'
            else:
                safe = f' data-external-url="{html.escape(urljoin(self.source_url, attributes["href"]), quote=True)}"'
        elif tag == "img" and attributes.get("src"):
            safe = f' src="{html.escape(urljoin(self.source_url, attributes["src"]), quote=True)}"'
        self.parts.append(f"<{tag}{safe}>")

    def handle_endtag(self, tag: str) -> None:
        if self.skip_depth:
            self.skip_depth -= 1
            return
        if self.active and tag.lower() not in {"html", "body", "main"}:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self.active and not self.skip_depth:
            self.parts.append(html.escape(data))


def parse_article(source_url: str, article_html: str) -> tuple[str, set[str]]:
    parser = ArticleParser(source_url)
    parser.feed(article_html)
    parser.close()
    content = "".join(parser.parts)
    if not content.strip():
        raise ValueError("Wikipedia article content could not be found")
    return content, parser.links


def rewrite_links(content: str, pdf_by_url: dict[str, Path]) -> str:
    def local_link(match: re.Match[str]) -> str:
        pdf = pdf_by_url.get(match.group(1))
        return f' href="{html.escape(os.path.relpath(pdf.resolve(), start=Path.cwd().resolve()).replace(os.sep, "/"), quote=True)}"' if pdf else ""

    # Keep external links as text for offline use.
    content = re.sub(r' data-wikipedia-url="([^"]+)"', local_link, content)
    content = re.sub(r' data-external-url="[^"]+"', "", content)
    return content


def render_pdf(title: str, content: str, output: Path) -> None:
    try:
        from weasyprint import HTML
    except (ImportError, OSError) as exc:
        raise RuntimeError("WeasyPrint/GTK3 is unavailable. Install requirements.txt and the Windows GTK3 runtime.") from exc
    document = f'''<!doctype html><html><head><meta charset="utf-8"><title>{html.escape(title)}</title><style>@page {{ size:A4; margin:2.1cm 2cm 2.3cm; @bottom-center {{ content:"Page " counter(page); font:9pt Arial; color:#777; }} }} body {{ font-family:Georgia,"Times New Roman",serif; color:#202124; line-height:1.58; font-size:10.8pt; }} .article-title {{ font:bold 27pt Arial,sans-serif; color:#18212b; border-bottom:3px solid #3b6ea5; padding-bottom:.32em; margin:0 0 1em; }} h2 {{ font:bold 17pt Arial,sans-serif; color:#234f7d; border-bottom:1px solid #c8d3df; margin:1.45em 0 .55em; }} h3 {{ font:bold 13pt Arial,sans-serif; color:#315f87; }} a {{ color:#245b91; text-decoration:none; }} a[href^="file:"] {{ -weasy-link: underline; }} img {{ max-width:100%; height:auto; }} table {{ border-collapse:collapse; width:100%; margin:1em 0; font-size:9pt; page-break-inside:avoid; }} th {{ background:#e9f0f7; text-align:left; }} th,td {{ border:1px solid #bbc7d2; padding:5px 7px; vertical-align:top; }} blockquote {{ border-left:4px solid #9bb5cd; padding:.2em 1em; color:#4d5660; }}</style></head><body><h1 class="article-title">{html.escape(title)}</h1><article>{content}</article></body></html>'''
    HTML(string=document, base_url=str(output.parent.resolve())).write_pdf(str(output))


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
    args = parser.parse_args()
    subjects = read_master_file(args.master_file)
    direct_by_subject = {subject: {canonical_url(url) for url in urls if canonical_url(url)} for subject, urls in subjects.items()}
    root_urls = set().union(*direct_by_subject.values()) if direct_by_subject else set()
    pages: dict[str, tuple[str, set[str]]] = {}
    pending = list(root_urls)
    discovered: set[str] = set()
    while pending:
        url = pending.pop(0)
        print(f"Downloading {ascii_safe(article_title(url))}", flush=True)
        try:
            content, links = parse_article(url, fetch_article(url))
            pages[url] = (content, links)
            if url in root_urls:
                for link in links:
                    if link not in pages and link not in discovered:
                        discovered.add(link)
                        pending.append(link)
        except Exception as exc:
            print(f"  ERROR: {ascii_safe(repr(exc))}", file=sys.stderr, flush=True)
        time.sleep(1)
    articles_dir = args.output / "artikler"
    articles_dir.mkdir(parents=True, exist_ok=True)
    pdf_by_url = {url: articles_dir / safe_filename(article_title(url)) for url in pages}
    jobs = list(pages.items())
    workers = min(4, max(1, len(jobs)))
    print(f"Rendering {len(jobs)} PDFs with {workers} workers...", flush=True)
    def render_one(item):
        url, (content, _) = item
        render_pdf(article_title(url), rewrite_links(content, pdf_by_url), pdf_by_url[url])
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
