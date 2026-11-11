import hashlib
import html
import json
import re
import time
from functools import lru_cache
from pathlib import Path
from tempfile import NamedTemporaryFile

from .config import CACHE_DIR
from .utils import find_unicode_font

_RENDER_CACHE_VERSION = "3"


def fingerprint_of(title: str, content: str) -> str:
    """Public so app.py can persist all fingerprints in one shared file."""
    digest = hashlib.sha256()
    digest.update(_RENDER_CACHE_VERSION.encode("ascii"))
    digest.update(title.encode("utf-8"))
    digest.update(b"\0")
    digest.update(content.encode("utf-8"))
    digest.update(str(_font_path()).encode("utf-8"))
    return digest.hexdigest()


@lru_cache(maxsize=1)
def _font_path() -> Path:
    return find_unicode_font()


def _render_cache_path() -> Path:
    # One shared fingerprints file inside the article cache keeps the output
    # folder free of sidecar files.
    return CACHE_DIR / "render-fingerprints.json"


# The fingerprint file grows to hundreds of KB, so re-reading and re-parsing it
# for every article dominated warm re-runs. Keep the parsed mapping in memory
# for the lifetime of the process instead. render_pdf re-checks is_current after
# app.py has already filtered unchanged PDFs, so this cache sees the same file
# twice per article; save_fingerprints keeps it in sync.
_FINGERPRINTS: dict[str, str] | None = None


def _load_fingerprints() -> dict[str, str]:
    global _FINGERPRINTS
    if _FINGERPRINTS is None:
        try:
            fingerprints = json.loads(_render_cache_path().read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            fingerprints = {}
        _FINGERPRINTS = fingerprints if isinstance(fingerprints, dict) else {}
    return _FINGERPRINTS


def is_current(output: Path, title: str, content: str, *, fingerprint: str | None = None) -> bool:
    if not output.is_file():
        return False
    expected = fingerprint or fingerprint_of(title, content)
    return _load_fingerprints().get(str(output.resolve())) == expected


def _write_fingerprints(fingerprints: dict[str, str]) -> None:
    path = _render_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(fingerprints), encoding="utf-8")
    temporary.replace(path)


def save_fingerprints(entries: dict[str, str]) -> None:
    """Merge rendered fingerprints into the shared cache file atomically."""
    fingerprints = _load_fingerprints()
    fingerprints.update(entries)
    _write_fingerprints(fingerprints)


def prune_fingerprints(valid_paths: set[str] | None = None) -> int:
    """Drop fingerprint entries whose PDF no longer exists.

    ``valid_paths`` can restrict the prune to the PDFs produced by the current
    run, which is what ``--prune-output`` wants.
    """
    fingerprints = _load_fingerprints()
    stale = [
        path
        for path in fingerprints
        if (path not in valid_paths if valid_paths is not None else not Path(path).is_file())
    ]
    for path in stale:
        del fingerprints[path]
    if stale:
        _write_fingerprints(fingerprints)
    return len(stale)


def _simplify_html(content: str) -> str:
    content = re.sub(r"\s+", " ", content)
    content = re.sub(r"<(?:font|center)(?:\s[^>]*)?>", "<span>", content, flags=re.IGNORECASE)
    content = re.sub(r"</(?:font|center)>", "</span>", content, flags=re.IGNORECASE)
    return content.strip()


_HEADING = re.compile(r"<h([1-6])[^>]*>(.*?)</h\1>", flags=re.IGNORECASE | re.DOTALL)


def _flatten_headings(content: str) -> str:
    """Reduce headings to plain text before rendering.

    fpdf2 starts one outline entry every time it receives text while inside a
    heading. Inline markup would therefore split "First section" into several
    bookmarks, so headings are flattened to a single text node.
    """

    def flatten(match: re.Match[str]) -> str:
        level = match.group(1)
        text = html.unescape(re.sub(r"<[^>]+>", " ", match.group(2)))
        return f"<h{level}>{html.escape(re.sub(r'\s+', ' ', text).strip())}</h{level}>"

    return _HEADING.sub(flatten, content)


def _write_html(pdf, content: str) -> None:
    # table_line_separators draws the full grid instead of just a top border;
    # pre_code_font keeps <code>/<pre> on our Unicode font instead of fpdf2's
    # latin-1 "Courier" core font, which crashes on characters like "→".
    pdf.write_html(
        _simplify_html(_flatten_headings(content)),
        font_family=_FONT_FAMILY,
        pre_code_font=_FONT_FAMILY,
        table_line_separators=True,
        warn_on_tags_not_matching=False,
    )


_FONT_FAMILY = "ArticleFont"
_FONT_STYLES = ("", "B", "I", "BI")
# add_font() re-parses the whole TrueType file for every style of every PDF, and
# the glyph-name table for a 1 MB Arial dominates that cost (~100 ms per PDF).
# Parsing once per process and reusing the immutable parts cuts that to ~2 ms.
#
# The prototype cannot be reused directly: fpdf2 subsets each font as the
# document is written, which mutates the parsed table in place and accumulates
# glyph IDs. Sharing the prototype therefore corrupts later documents. Instead we
# clone the prototype and reset exactly the per-document state (subset map, glyph
# IDs, missing-glyph list, size marker) while handing each document a fresh lazy
# reader, so subsetting works on its own copy. Verified to produce identical page
# content and embedded font subsets versus add_font().
_FONT_PROTOTYPES: dict[str, object] | None = None


def _font_prototypes() -> dict[str, object]:
    """Parse the four font styles once per process and return the prototypes."""
    global _FONT_PROTOTYPES
    if _FONT_PROTOTYPES is None:
        from fpdf import FPDF
        from fpdf.fonts import TTFFont

        font_path = _font_path()
        scratch = FPDF()
        prototypes: dict[str, object] = {}
        for style in _FONT_STYLES:
            normalized = "".join(sorted(style.upper()))
            # Mirrors fpdf2's own fontkey convention: lowercase family + style.
            fontkey = f"{_FONT_FAMILY.lower()}{normalized}"
            prototypes[fontkey] = TTFFont(scratch, font_path, fontkey, normalized)
        _FONT_PROTOTYPES = prototypes
    return _FONT_PROTOTYPES


def _add_article_font(pdf) -> None:
    """Register the article font in all four styles on ``pdf``."""
    from fpdf.fonts import SubsetMap
    from fontTools.ttLib import TTFont as FontToolsTTFont

    font_path = _font_path()
    for index, prototype in enumerate(_font_prototypes().values(), start=1):
        font = type(prototype).__new__(type(prototype))
        for attribute in type(prototype).__slots__:
            if hasattr(prototype, attribute):
                setattr(font, attribute, getattr(prototype, attribute))
        # Per-document state that must not be shared with the prototype.
        font.i = index
        font.subset = SubsetMap(font)
        font.glyph_ids = dict(prototype.glyph_ids)
        font.missing_glyphs = []
        font.biggest_size_pt = 0.0
        # A fresh lazy reader per document: cheap (well under a millisecond) and
        # it guarantees subsetting mutates only this document's tables.
        font.ttfont = FontToolsTTFont(str(font_path), lazy=True, recalcTimestamp=False)
        pdf.fonts[font.fontkey] = font


def render_pdf(
    title: str,
    content: str,
    output: Path,
    *,
    fingerprint: str | None = None,
    source_url: str | None = None,
) -> None:
    if is_current(output, title, content, fingerprint=fingerprint):
        return

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

    started = time.monotonic()
    pdf = ArticlePDF()
    _add_article_font(pdf)
    font_setup_duration = time.monotonic() - started

    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_title(title)
    pdf.set_author("Wikipedia contributors")
    pdf.set_creator("Article PDF Generator")
    pdf.set_subject(f"Offline article: {title}")
    keywords = "Wikipedia, offline, PDF"
    if source_url:
        keywords += f", {source_url}"
    pdf.set_keywords(keywords)
    pdf.add_page()
    pdf.set_font("ArticleFont", "B", 22)
    pdf.set_text_color(24, 33, 43)
    pdf.multi_cell(0, 12, title)
    pdf.set_draw_color(59, 110, 165)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(6)
    pdf.set_font("ArticleFont", size=10)
    pdf.set_text_color(32, 33, 36)

    html_started = time.monotonic()
    _write_html(pdf, content)
    html_duration = time.monotonic() - html_started

    output.parent.mkdir(parents=True, exist_ok=True)
    output_started = time.monotonic()
    with NamedTemporaryFile(prefix=output.stem + ".", suffix=output.suffix, dir=output.parent, delete=False) as temporary:
        temporary_path = Path(temporary.name)
    try:
        pdf.output(temporary_path)
        temporary_path.replace(output)
    finally:
        temporary_path.unlink(missing_ok=True)
    output_duration = time.monotonic() - output_started
    print(
        f"  PDF timing {title}: fonts={font_setup_duration:.3f}s, "
        f"layout={html_duration:.3f}s, output={output_duration:.3f}s",
        flush=True,
    )
