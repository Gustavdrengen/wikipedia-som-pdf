import hashlib
import time
from html import unescape
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

from .config import CACHE_DIR, CACHE_MAX_AGE_SECONDS, MAX_RETRIES, SKIP_NAMESPACES, USER_AGENT
from .rate_limit import RequestLimiter

_REQUEST_LIMITER = RequestLimiter(1.0)
from .utils import ascii_safe


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
    import re
    cleaned = re.sub(r"[^\w. -]+", "", title, flags=re.UNICODE)
    return (re.sub(r"\s+", " ", cleaned).strip(" .") or "article")[:180] + ".pdf"


def clean_response_cache(cache_dir: Path = CACHE_DIR, now: float | None = None) -> None:
    now = time.time() if now is None else now
    cache_dir.mkdir(parents=True, exist_ok=True)
    for cache_file in cache_dir.glob("*.html"):
        try:
            if now - cache_file.stat().st_mtime > CACHE_MAX_AGE_SECONDS:
                cache_file.unlink()
        except FileNotFoundError:
            continue


def fetch_article(url: str, cache_dir: Path = CACHE_DIR) -> tuple[str, bool]:
    cache_file = cache_dir / f"{hashlib.sha256(url.encode('utf-8')).hexdigest()}.html"
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
        _REQUEST_LIMITER.wait()
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
