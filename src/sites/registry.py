"""Generic registry for pluggable site adapters."""

from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse


@dataclass(frozen=True)
class Site:
    """A registered domain handler supplied by a site adapter."""

    name: str
    domains: tuple[str, ...]
    canonicalize: Callable[[str], str]
    title: Callable[[str], str]
    api_url: Callable[[str], str]
    is_content_start: Callable[[dict[str, str | None]], bool]
    accepts_content: Callable[[str], bool]
    accepts_media: Callable[[str], bool]

    def matches(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return any(host == domain or host.endswith("." + domain) for domain in self.domains)


_SITES: list[Site] = []


def register(site: Site) -> None:
    _SITES.append(site)


def get_site(url: str) -> Site | None:
    return next((site for site in _SITES if site.matches(url)), None)
