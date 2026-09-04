import hashlib
import json
import re
import time
from functools import lru_cache
from pathlib import Path
from tempfile import NamedTemporaryFile

from .config import CACHE_DIR
from .utils import find_unicode_font

_RENDER_CACHE_VERSION = "2"


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


def is_current(output: Path, title: str, content: str) -> bool:
    if not output.is_file():
        return False
    try:
        fingerprints = json.loads(_render_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    return fingerprints.get(str(output.resolve())) == fingerprint_of(title, content)


def save_fingerprints(entries: dict[str, str]) -> None:
    """Merge rendered fingerprints into the shared cache file atomically."""
    path = _render_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fingerprints = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(fingerprints, dict):
            fingerprints = {}
    except (OSError, ValueError, TypeError):
        fingerprints = {}
    fingerprints.update(entries)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(fingerprints), encoding="utf-8")
    temporary.replace(path)


def _simplify_html(content: str) -> str:
    content = re.sub(r"\s+", " ", content)
    content = re.sub(r"<(?:font|center)(?:\s[^>]*)?>", "<span>", content, flags=re.IGNORECASE)
    content = re.sub(r"</(?:font|center)>", "</span>", content, flags=re.IGNORECASE)
    return content.strip()


def render_pdf(title: str, content: str, output: Path) -> None:
    if is_current(output, title, content):
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
    font_path = _font_path()
    for style in ("", "B", "I", "BI"):
        pdf.add_font("ArticleFont", style=style, fname=str(font_path))
    font_setup_duration = time.monotonic() - started

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

    html_started = time.monotonic()
    # pre_code_font keeps <code>/<pre> on our Unicode font instead of fpdf2's
    # latin-1 "Courier" core font, which crashes on characters like "→".
    pdf.write_html(_simplify_html(content), font_family="ArticleFont", pre_code_font="ArticleFont")
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
