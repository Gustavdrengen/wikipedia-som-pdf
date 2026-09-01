"""Generic article fetching and caching, driven by registered sites."""

import hashlib
import json
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .config import CACHE_DIR, CACHE_MAX_AGE_SECONDS, MAX_RETRIES, MAX_RETRY_WAIT_SECONDS, USER_AGENT
from .rate_limit import GLOBAL_REQUEST_LIMITER, wait_with_progress
from .sites import canonical_url as resolve_canonical_url, site_for

_CACHE_LOCK = threading.Lock()
_CACHE_INDEX: OrderedDict[tuple[str, str], tuple[float, dict]] = OrderedDict()
_CACHE_MAX_ENTRIES = 256
_IN_FLIGHT: dict[tuple[str, str], tuple[threading.Event, dict]] = {}
_PERMANENT_FAILURES = {400, 401, 403, 404, 410, 451}
_TRANSIENT_FAILURES = {500, 502, 503, 504}


def canonical_url(url: str) -> str:
    return resolve_canonical_url(url)


def article_title(url: str) -> str:
    return site.title(url) if (site := site_for(url)) else url


def safe_filename(title: str) -> str:
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


def fetch_article(url: str, cache_dir: Path = CACHE_DIR, request_delay: float = 1.0) -> str:
    site = site_for(url)
    if site is None:
        raise ValueError(f"Unsupported site: {url}")
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
        return result["content"]
    try:
        failure_path = failure_cache_path(url, cache_dir)
        if _fresh(failure_path):
            try:
                status = json.loads(failure_path.read_text(encoding="utf-8"))["status"]
                raise HTTPError(url, status, "Cached failure", {}, None)
            except (OSError, ValueError, KeyError, TypeError):
                pass
        api_url = site.api_url(url)
        retry_waited = 0.0
        for attempt in range(MAX_RETRIES + 1):
            if request_delay:
                GLOBAL_REQUEST_LIMITER.wait(request_delay)
            request = Request(api_url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
            try:
                with urlopen(request, timeout=30) as response:  # noqa: S310
                    content = response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace")
                GLOBAL_REQUEST_LIMITER.success()
                if not site.accepts_content(content):
                    _write_json(failure_path, {"status": 404})
                    raise HTTPError(url, 404, "Unsupported page type", {}, None)
                result["content"] = content
                return content
            except HTTPError as exc:
                if exc.code in _PERMANENT_FAILURES:
                    _write_json(failure_path, {"status": exc.code})
                    raise
                if exc.code == 429:
                    # Do not spend the retry budget on a title that the server is
                    # refusing. The failure is cached for the normal cache period.
                    _write_json(failure_path, {"status": 429})
                    raise
                if exc.code not in _TRANSIENT_FAILURES or attempt >= MAX_RETRIES:
                    raise
                retry_after = exc.headers.get("Retry-After")
                try:
                    wait_seconds = max(5.0, float(retry_after)) if retry_after else 5.0 * (2 ** attempt)
                except (TypeError, ValueError):
                    wait_seconds = 5.0 * (2 ** attempt)
                wait_seconds = min(wait_seconds, MAX_RETRY_WAIT_SECONDS - retry_waited)
                if wait_seconds <= 0:
                    raise
                retry_waited += wait_seconds
                GLOBAL_REQUEST_LIMITER.rate_limited(wait_seconds)

                print(f"  HTTP {exc.code} for {url}; retrying in {wait_seconds:g}s", flush=True)
                wait_with_progress(wait_seconds, f"HTTP {exc.code} retry for {url}", log=lambda message, **_: print(message, flush=True))
        raise RuntimeError(f"Could not fetch {url}")
    except Exception as exc:
        result["error"] = exc
        raise
    finally:
        with _CACHE_LOCK:
            entry = _IN_FLIGHT.pop(key, None)
            if entry:
                entry[0].set()
