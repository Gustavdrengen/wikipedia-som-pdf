import hashlib
import json
import threading
import time
from collections import OrderedDict
from html import unescape
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

from .config import CACHE_DIR, CACHE_MAX_AGE_SECONDS, MAX_RETRIES, SKIP_NAMESPACES, USER_AGENT
from .rate_limit import RequestLimiter
from .utils import ascii_safe

_REQUEST_LIMITER = RequestLimiter(1.0)
_CACHE_LOCK = threading.Lock()
_CACHE_INDEX: OrderedDict[tuple[str, str], tuple[float, dict]] = OrderedDict()
_CACHE_MAX_ENTRIES = 256
_IN_FLIGHT: dict[tuple[str, str], tuple[threading.Event, dict]] = {}
_PERMANENT_FAILURES = {400, 401, 403, 404, 410, 451}
_TRANSIENT_FAILURES = {429, 500, 502, 503, 504}


def canonical_url(url: str, base: str = "https://en.wikipedia.org/wiki/") -> str:
    parsed = urlparse(urljoin(base, url))
    if parsed.scheme not in {"http", "https"} or not (parsed.hostname or "").lower().endswith("wikipedia.org") or "/wiki/" not in parsed.path:
        return ""
    title = unquote(parsed.path.split("/wiki/", 1)[1]).replace("_", " ").strip()
    namespace = title.split(":", 1)[0].lower() if ":" in title else ""
    if not title or namespace in SKIP_NAMESPACES:
        return ""
    return urlunparse(("https", parsed.hostname.lower(), "/wiki/" + quote(title.replace(" ", "_"), safe="/:"), "", "", ""))


def article_title(url: str) -> str:
    return unquote(urlparse(url).path.split("/wiki/", 1)[-1]).replace("_", " ").strip() or "Wikipedia article"


def safe_filename(title: str) -> str:
    import re
    cleaned = re.sub(r"[^\w. -]+", "", title, flags=re.UNICODE)
    return (re.sub(r"\s+", " ", cleaned).strip(" .") or "article")[:180] + ".pdf"


def cache_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def processed_cache_path(url: str, cache_dir: Path = CACHE_DIR) -> Path:
    return cache_dir / f"{cache_key(url)}.parsed.json"


def failure_cache_path(url: str, cache_dir: Path = CACHE_DIR) -> Path:
    return cache_dir / f"{cache_key(url)}.failure.json"


def _fresh(path: Path, now: float | None = None) -> bool:
    try:
        return (time.time() if now is None else now) - path.stat().st_mtime <= CACHE_MAX_AGE_SECONDS
    except (FileNotFoundError, OSError):
        return False


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def clean_response_cache(cache_dir: Path = CACHE_DIR, now: float | None = None) -> None:
    now = time.time() if now is None else now
    cache_dir.mkdir(parents=True, exist_ok=True)
    with _CACHE_LOCK:
        _CACHE_INDEX.clear()
    for cache_file in cache_dir.iterdir():
        if cache_file.suffix not in {".json", ".html", ".404"}:
            continue
        try:
            if cache_file.suffix in {".html", ".404"} or now - cache_file.stat().st_mtime > CACHE_MAX_AGE_SECONDS:
                cache_file.unlink()
        except FileNotFoundError:
            continue


def load_processed_article(url: str, cache_dir: Path = CACHE_DIR) -> tuple[str, set[str]] | None:
    key = (str(cache_dir.resolve()), cache_key(url))
    now = time.time()
    with _CACHE_LOCK:
        cached = _CACHE_INDEX.get(key)
        if cached and now - cached[0] <= CACHE_MAX_AGE_SECONDS:
            _CACHE_INDEX.move_to_end(key)
            value = cached[1]
            return value["content"], set(value["links"])
    path = processed_cache_path(url, cache_dir)
    if not _fresh(path, now):
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        content, links = value["content"], value["links"]
        if not isinstance(content, str) or not isinstance(links, list) or not all(isinstance(link, str) for link in links):
            return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    with _CACHE_LOCK:
        _CACHE_INDEX[key] = (now, {"content": content, "links": links})
        _CACHE_INDEX.move_to_end(key)
        while len(_CACHE_INDEX) > _CACHE_MAX_ENTRIES:
            _CACHE_INDEX.popitem(last=False)
    return content, set(links)


def store_processed_article(url: str, content: str, links: set[str], cache_dir: Path = CACHE_DIR) -> None:
    value = {"content": content, "links": sorted(links)}
    path = processed_cache_path(url, cache_dir)
    _write_json(path, value)
    key = (str(cache_dir.resolve()), cache_key(url))
    with _CACHE_LOCK:
        _CACHE_INDEX[key] = (time.time(), value)
        _CACHE_INDEX.move_to_end(key)
        while len(_CACHE_INDEX) > _CACHE_MAX_ENTRIES:
            _CACHE_INDEX.popitem(last=False)


def fetch_article(url: str, cache_dir: Path = CACHE_DIR, request_delay: float = 1.0) -> tuple[str, bool]:
    cached = load_processed_article(url, cache_dir)
    if cached is not None:
        print(f"Using cached processed article {ascii_safe(article_title(url))}", flush=True)
        return cached[0], True
    key = (str(cache_dir.resolve()), cache_key(url))
    with _CACHE_LOCK:
        existing = _IN_FLIGHT.get(key)
        if existing is None:
            event, result = threading.Event(), {}
            _IN_FLIGHT[key] = (event, result)
            owner = True
        else:
            event, result = existing
            owner = False
    if not owner:
        event.wait()
        if "error" in result:
            raise result["error"]
        return result["content"], result["cache_hit"]
    try:
        failure_path = failure_cache_path(url, cache_dir)
        if _fresh(failure_path):
            try:
                status = json.loads(failure_path.read_text(encoding="utf-8"))["status"]
                raise HTTPError(url, status, "Cached permanent failure", {}, None)
            except (OSError, ValueError, KeyError, TypeError):
                pass
        parsed = urlparse(url)
        title = unquote(parsed.path.split("/wiki/", 1)[1])
        api_url = urlunparse((parsed.scheme, parsed.netloc, "/api/rest_v1/page/html/" + quote(title, safe=""), "", "", ""))
        for attempt in range(MAX_RETRIES + 1):
            if request_delay:
                _REQUEST_LIMITER.wait(request_delay)
            request = Request(api_url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
            try:
                with urlopen(request, timeout=30) as response:  # noqa: S310
                    content = response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace")
                result.update(content=content, cache_hit=False)
                return content, False
            except HTTPError as exc:
                if exc.code in _PERMANENT_FAILURES:
                    _write_json(failure_path, {"status": exc.code})
                    raise
                if exc.code not in _TRANSIENT_FAILURES or attempt >= MAX_RETRIES:
                    raise
                retry_after = exc.headers.get("Retry-After")
                try:
                    wait_seconds = max(5.0, float(retry_after)) if retry_after else 5.0 * (2 ** attempt)
                except (TypeError, ValueError):
                    wait_seconds = 5.0 * (2 ** attempt)
                wait_seconds = min(wait_seconds, 300.0)
                print(f"  HTTP {exc.code}; retrying in {wait_seconds:g}s", flush=True)
                time.sleep(wait_seconds)
        raise RuntimeError(f"Could not fetch {url}")
    except Exception as exc:
        result["error"] = exc
        raise
    finally:
        with _CACHE_LOCK:
            entry = _IN_FLIGHT.pop(key, None)
            if entry:
                entry[0].set()


def unescape_url(value: str) -> str:
    return unescape(value)
