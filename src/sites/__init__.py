"""Auto-discovered site adapters."""

import importlib
import pkgutil
from urllib.parse import urljoin

from .registry import Site, get_site, register

_LOADED = False


def _load_sites() -> None:
    global _LOADED
    if _LOADED:
        return
    for module_info in pkgutil.iter_modules(__path__):
        importlib.import_module(f"{__name__}.{module_info.name}")
    _LOADED = True


def site_for(url: str) -> Site | None:
    _load_sites()
    return get_site(url)


def canonical_url(url: str, base: str = "") -> str:
    absolute = urljoin(base, url)
    site = site_for(absolute)
    return site.canonicalize(absolute) if site else ""
