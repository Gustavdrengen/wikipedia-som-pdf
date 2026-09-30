"""Local rendering of MediaWiki math (TeX) into crisp PNGs for PDF embedding.

Math blocks are captured by the article parser as ``<img data-math=...>``
placeholders. This module replaces those with PNGs rendered locally by
matplotlib's mathtext, so no request is ever made to Wikimedia's math SVG
service. Formulas mathtext cannot parse degrade to readable plain text
instead of raw TeX.
"""

import hashlib
import html
import re
import threading
from pathlib import Path

from .config import MEDIA_CACHE_DIR

_MATH_PLACEHOLDER = re.compile(
    r'<img data-math="(?P<math>[^"]*)" data-math-fallback="(?P<fallback>[^"]*)" data-math-display="(?P<display>[01])">'
)

_RENDER_DPI = 300
_INLINE_PT = 10.0
_DISPLAY_PT = 12.0
# Approximate usable width of an A4 page in points; keep wide display formulas
# from overflowing the page.
_MAX_WIDTH_PT = 470.0
_MATH_DIR = MEDIA_CACHE_DIR / "math"

# matplotlib is not thread-safe, so all rendering happens under _LOCK. Rendered
# files are cached on disk under .article-media-cache/math/ and reused across
# articles and runs.
_LOCK = threading.Lock()
_RENDERED: dict[str, tuple[Path, int, int]] = {}
_FAILED: set[str] = set()


def _sanitize(tex: str) -> str:
    """Strip the ``{\\displaystyle ...}`` wrapper MediaWiki puts around formulas."""
    tex = tex.strip()
    match = re.match(r"^\{\s*\\displaystyle\b\s*(.*)\}$", tex, flags=re.DOTALL)
    if match:
        tex = match.group(1).strip()
    tex = re.sub(r"\\displaystyle\b", "", tex)
    return tex.strip()


def _tex_to_text(tex: str) -> str:
    """Best-effort conversion of raw TeX into readable plain text."""
    text = _sanitize(tex)
    for _ in range(6):
        rewritten = re.sub(
            r"\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}",
            lambda m: f"({m.group(1).strip()})/({m.group(2).strip()})",
            text,
        )
        if rewritten == text:
            break
        text = rewritten
    text = re.sub(r"\\text\{([^{}]*)\}", r"\1", text)
    text = re.sub(
        r"\\(?:mathbf|mathrm|mathit|mathsf|mathtt|boldsymbol|vec|hat|bar|dot|ddot|operatorname|text)\b",
        "",
        text,
    )
    text = re.sub(r"[{}^_]", " ", text)
    text = re.sub(r"\\[a-zA-Z]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _render_tex(tex: str, path: Path, display: bool) -> None:
    from matplotlib import mathtext, rcParams
    from matplotlib.font_manager import FontProperties

    rcParams["mathtext.fontset"] = "cm"
    prop = FontProperties(size=_DISPLAY_PT if display else _INLINE_PT)
    mathtext.math_to_image(
        f"${tex}$",
        str(path),
        prop=prop,
        dpi=_RENDER_DPI,
        format="png",
        color="black",
    )


def _resolve(tex: str, display: bool) -> tuple[Path, int, int] | None:
    """Return the cached (path, width_px, height_px) for a formula, rendering on miss."""
    key = hashlib.sha256(f"{tex}\0{int(display)}".encode("utf-8")).hexdigest()
    if key in _FAILED:
        return None
    rendered = _RENDERED.get(key)
    if rendered is not None:
        return rendered
    path = _MATH_DIR / f"{key}.png"
    with _LOCK:
        rendered = _RENDERED.get(key)
        if rendered is not None:
            return rendered
        if not path.is_file():
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                _render_tex(tex, path, display)
            except Exception:
                _FAILED.add(key)
                return None
        try:
            from PIL import Image

            with Image.open(path) as image:
                width, height = image.size
        except Exception:
            _FAILED.add(key)
            return None
        if width <= 0 or height <= 0:
            _FAILED.add(key)
            return None
        rendered = (path, width, height)
        _RENDERED[key] = rendered
        return rendered


def _image_tag(rendered: tuple[Path, int, int]) -> str:
    path, width, height = rendered
    # math_to_image renders at 1 point = _RENDER_DPI/72 pixels, and fpdf2's HTML
    # renderer interprets <img width/height> attributes as points, so the PDF
    # displays the formula at its natural text size while the PNG stays crisp.
    width_pt = width * 72 / _RENDER_DPI
    height_pt = height * 72 / _RENDER_DPI
    if width_pt > _MAX_WIDTH_PT:
        scale = _MAX_WIDTH_PT / width_pt
        width_pt *= scale
        height_pt *= scale
    return (
        f'<img src="{html.escape(str(path), quote=True)}" '
        f'width="{width_pt:.2f}" height="{height_pt:.2f}">'
    )


def render_math_in_content(content: str) -> str:
    """Replace math placeholders in parsed article content with local images/text."""

    def replace(match: re.Match[str]) -> str:
        tex = html.unescape(match.group("math"))
        fallback = html.unescape(match.group("fallback"))
        display = match.group("display") == "1"
        if not tex:
            return html.escape(fallback) if fallback else ""
        rendered = _resolve(_sanitize(tex), display)
        if rendered is not None:
            return _image_tag(rendered)
        if fallback:
            return html.escape(fallback)
        readable = _tex_to_text(tex)
        return html.escape(readable) if readable else ""

    return _MATH_PLACEHOLDER.sub(replace, content)
