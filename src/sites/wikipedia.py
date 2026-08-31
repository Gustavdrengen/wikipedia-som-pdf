"""Wikipedia site adapter."""

import re
from urllib.parse import quote, unquote, urlparse, urlunparse

from .registry import Site, register

_PATH = "/wiki/"
_API_PATH = "/api/rest_v1/page/html/"
_CONTENT_MARKERS = (("id", "mw-content-text"), ("class", "mw-parser-output"))
_NAMESPACE_META = re.compile(r'<meta[^>]*property="mw:pageNamespace"[^>]*content="(-?\d+)"[^>]*/?>')
_SKIP_NAMESPACES = frozenset({
    "category", "file", "help", "special", "template", "talk", "portal",
    "wikipedia", "module", "book", "draft", "mediawiki", "timedtext",
    "topic", "user", "education program", "gadget", "gadget definition",
})


def _title(url: str) -> str:
    return unquote(urlparse(url).path.split(_PATH, 1)[-1]).replace("_", " ").strip() or "Wikipedia article"


def _canonicalize(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or _PATH not in parsed.path:
        return ""
    title = _title(url)
    namespace = title.split(":", 1)[0].lower() if ":" in title else ""
    if not title or namespace in _SKIP_NAMESPACES:
        return ""
    host = (parsed.hostname or "").lower()
    parts = host.split(".")
    if len(parts) >= 3 and parts[1] == "m":
        parts.pop(1)
        host = ".".join(parts)
    return urlunparse(("https", host, _PATH + quote(title.replace(" ", "_"), safe="/:"), "", "", ""))


def _api_url(url: str) -> str:
    parsed = urlparse(url)
    return parsed._replace(path=_API_PATH + quote(_title(url), safe=""), params="", query="", fragment="").geturl()


def _is_content_start(attributes: dict[str, str | None]) -> bool:
    classes = attributes.get("class", "") or ""
    return any(
        (kind == "id" and attributes.get("id") == marker) or (kind == "class" and marker in classes)
        for kind, marker in _CONTENT_MARKERS
    )


def _accepts_content(content: str) -> bool:
    match = _NAMESPACE_META.search(content)
    return match is None or match.group(1) == "0"


register(Site(
    name="wikipedia",
    domains=("wikipedia.org",),
    canonicalize=_canonicalize,
    title=_title,
    api_url=_api_url,
    is_content_start=_is_content_start,
    accepts_content=_accepts_content,
))
