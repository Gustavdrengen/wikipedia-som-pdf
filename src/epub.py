"""Reflowable EPUB export of the prepared article collection.

The PDF pipeline already produces clean HTML with locally cached images, so the
EPUB writer only converts that HTML to XHTML, rewrites cross-article links to
chapter files and packages the result with the standard EPUB 3 metadata.
"""

import html
import re
import uuid
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlparse

_NAMESPACE = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")
_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}
_KNOWN_LANGUAGES = frozenset({
    "af", "ar", "az", "be", "bg", "bs", "ca", "cs", "cy", "da", "de", "el",
    "en", "eo", "es", "et", "eu", "fa", "fi", "fr", "ga", "gl", "he", "hi",
    "hr", "hu", "hy", "id", "is", "it", "ja", "ka", "kk", "ko", "la", "lt",
    "lv", "mk", "ms", "nl", "no", "nb", "nn", "pl", "pt", "ro", "ru", "sk",
    "sl", "sq", "sr", "sv", "sw", "th", "tr", "uk", "uz", "vi", "zh",
})
_CSS = """\
body {
    font-family: Georgia, "Times New Roman", serif;
    line-height: 1.55;
    margin: 0 5%;
    color: #202124;
}
h1, h2, h3, h4, h5, h6 {
    font-family: "Segoe UI", Helvetica, Arial, sans-serif;
    line-height: 1.25;
    margin: 1.2em 0 0.5em;
}
h1.article-title {
    border-bottom: 1px solid #3b6ea5;
    padding-bottom: 0.3em;
}
p { margin: 0 0 0.8em; }
a { color: #245a94; }
blockquote { margin: 0.8em 1.5em; color: #444; }
pre { white-space: pre-wrap; word-wrap: break-word; }
img { max-width: 100%; height: auto; }
table { border-collapse: collapse; width: 100%; margin: 1em 0; font-size: 0.92em; }
th, td { border: 1px solid #999; padding: 0.25em 0.4em; vertical-align: top; }
th { background: #eee; }
.center { text-align: center; }
"""


class EpubArticle(NamedTuple):
    url: str
    title: str
    content: str


def _language_for(url: str) -> str:
    prefix = (urlparse(url).hostname or "").lower().split(".", 1)[0]
    if prefix in _KNOWN_LANGUAGES:
        return prefix
    return "en" if "wikipedia.org" in (urlparse(url).hostname or "") else "da"


def _slug(title: str, fallback: str) -> str:
    import unicodedata

    normalized = unicodedata.normalize("NFKD", title)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug[:60] or fallback


def _strip_control_characters(content: str) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", content)


def _rewrite_article_links(content: str, chapter_by_url: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        article_url = html.unescape(match.group(1))
        chapter = chapter_by_url.get(article_url)
        if chapter is None:
            # Links to articles outside this collection still work as normal
            # web links in an EPUB reader.
            return f' href="{html.escape(article_url, quote=True)}"'
        return f' href="{html.escape(chapter, quote=True)}"'

    return re.sub(r' href="[^"]*" data-article-url="([^"]+)"', replace, content)


def _rewrite_images(content: str, assets: dict[str, tuple[str, str]]) -> str:
    def replace(match: re.Match[str]) -> str:
        raw_path = html.unescape(match.group(1))
        path = Path(raw_path)
        media_type = _MEDIA_TYPES.get(path.suffix.lower())
        if media_type is None or not path.is_file():
            return ""
        key = str(path.resolve())
        asset = assets.get(key)
        if asset is None:
            asset = (f"image-{len(assets) + 1:04d}{path.suffix.lower()}", media_type)
            assets[key] = asset
        return f' src="images/{asset[0]}"'

    content = re.sub(r' src="([^"]+)"', replace, content)
    # Drop <img> elements whose image could not be cached at all.
    return re.sub(r"<img\b(?![^>]*\bsrc=)[^>]*>", "", content, flags=re.IGNORECASE)


def _to_xhtml(content: str, chapter_by_url: dict[str, str], assets: dict[str, tuple[str, str]]) -> str:
    content = _strip_control_characters(content)
    content = _rewrite_article_links(content, chapter_by_url)
    # Width/height are point-sized for the PDF page and must not constrain the
    # reflowable EPUB layout.
    content = re.sub(
        r"<img\b[^>]*>",
        lambda match: re.sub(r'\s+(?:width|height)="[^"]*"', "", match.group(0)),
        content,
        flags=re.IGNORECASE,
    )
    content = _rewrite_images(content, assets)
    # The PDF-specific full-width/grid attributes are not reflowable; the EPUB
    # stylesheet provides the table appearance instead.
    content = re.sub(r"<table\b[^>]*>", "<table>", content, flags=re.IGNORECASE)
    content = re.sub(r"<font\b[^>]*>", "<span>", content, flags=re.IGNORECASE)
    content = re.sub(r"</font>", "</span>", content, flags=re.IGNORECASE)
    content = re.sub(r"<center\b[^>]*>", '<div class="center">', content, flags=re.IGNORECASE)
    content = re.sub(r"</center>", "</div>", content, flags=re.IGNORECASE)
    content = re.sub(r"<(br|hr)\b([^>]*?)\s*/?>", r"<\1\2/>", content, flags=re.IGNORECASE)
    content = re.sub(r"<img\b([^>]*?)\s*/?>", r"<img\1/>", content, flags=re.IGNORECASE)
    return re.sub(r'\s+data-article-url="[^"]*"', "", content)


def _chapter_xhtml(
    title: str,
    content: str,
    language: str,
    chapter_by_url: dict[str, str],
    assets: dict[str, tuple[str, str]],
) -> str:
    body = _to_xhtml(content, chapter_by_url, assets)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<!DOCTYPE html>\n"
        f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{language}" lang="{language}">\n'
        "<head>\n"
        '  <meta charset="utf-8" />\n'
        f"  <title>{html.escape(title)}</title>\n"
        '  <link rel="stylesheet" type="text/css" href="style.css" />\n'
        "</head>\n"
        "<body>\n"
        f'  <h1 class="article-title">{html.escape(title)}</h1>\n'
        f"  {body}\n"
        "</body>\n"
        "</html>\n"
    )


def _nav_xhtml(
    title: str,
    grouping: list[tuple[str, list[tuple[str, str]]]],
    language: str,
) -> str:
    groups = []
    for subject, entries in grouping:
        links = "".join(
            f'<li><a href="{html.escape(chapter, quote=True)}">{html.escape(article_title)}</a></li>'
            for chapter, article_title in entries
        )
        groups.append(f"<li><span>{html.escape(subject)}</span><ol>{links}</ol></li>")
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<!DOCTYPE html>\n"
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops" '
        f'xml:lang="{language}" lang="{language}">\n'
        "<head>\n"
        '  <meta charset="utf-8" />\n'
        f"  <title>{html.escape(title)}</title>\n"
        '  <link rel="stylesheet" type="text/css" href="style.css" />\n'
        "</head>\n"
        "<body>\n"
        '  <nav epub:type="toc" id="toc">\n'
        f"    <h1>{html.escape(title)}</h1>\n"
        f"    <ol>{''.join(groups)}</ol>\n"
        "  </nav>\n"
        "</body>\n"
        "</html>\n"
    )


def _content_opf(
    title: str,
    identifier: str,
    languages: list[str],
    chapters: list[tuple[str, str, str]],
    assets: dict[str, tuple[str, str]],
    modified: str,
) -> str:
    manifest = [
        '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav" />',
        '    <item id="css" href="style.css" media-type="text/css" />',
    ]
    for chapter_index, (filename, _, _) in enumerate(chapters, 1):
        manifest.append(
            f'    <item id="chapter-{chapter_index:03d}" href="{html.escape(filename, quote=True)}" '
            'media-type="application/xhtml+xml" />'
        )
    for asset_index, (filename, media_type) in enumerate(assets.values(), 1):
        manifest.append(
            f'    <item id="image-{asset_index:03d}" href="images/{html.escape(filename, quote=True)}" '
            f'media-type="{media_type}" />'
        )
    spine = "".join(f'    <itemref idref="chapter-{index:03d}" />\n' for index in range(1, len(chapters) + 1))
    language = languages[0] if languages else "da"
    language_elements = "".join(f"    <dc:language>{language_code}</dc:language>\n" for language_code in languages)
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        f'unique-identifier="pub-id" xml:lang="{language}">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f'    <dc:identifier id="pub-id">{identifier}</dc:identifier>\n'
        f"    <dc:title>{html.escape(title)}</dc:title>\n"
        '    <dc:creator>Wikipedia contributors</dc:creator>\n'
        '    <dc:description>Offline article collection</dc:description>\n'
        f"{language_elements}"
        f'    <meta property="dcterms:modified">{modified}</meta>\n'
        "  </metadata>\n"
        "  <manifest>\n"
        f"{chr(10).join(manifest)}\n"
        "  </manifest>\n"
        "  <spine>\n"
        f"{spine}"
        "  </spine>\n"
        "</package>\n"
    )


def write_epub(
    path: Path,
    title: str,
    articles: list[EpubArticle],
    subjects: dict[str, set[str]],
) -> Path:
    """Write the provided articles as one reflowable EPUB 3 file."""
    if not articles:
        raise ValueError("Cannot write an EPUB without articles")

    chapter_by_url = {
        article.url: f"chapter-{index:03d}-{_slug(article.title, 'article')}.xhtml"
        for index, article in enumerate(articles, 1)
    }
    article_by_url = {article.url: article for article in articles}
    chapters = [
        (chapter_by_url[article.url], article.title, article.content)
        for article in articles
    ]

    languages = sorted({_language_for(article.url) for article in articles})
    primary_language = Counter(_language_for(article.url) for article in articles).most_common(1)[0][0]
    identifier = f"urn:uuid:{uuid.uuid5(_NAMESPACE, title + chr(0) + chr(0).join(chapter_by_url))}"
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    grouping: list[tuple[str, list[tuple[str, str]]]] = []
    covered: set[str] = set()
    for subject, urls in subjects.items():
        entries = [
            (chapter_by_url[url], article_by_url[url].title)
            for url in urls
            if url in chapter_by_url
        ]
        entries.sort(key=lambda entry: entry[1].casefold())
        if entries:
            grouping.append((subject, entries))
            covered.update(url for url in urls if url in chapter_by_url)
    remaining = [
        (chapter_by_url[url], article.title)
        for url, article in article_by_url.items()
        if url not in covered
    ]
    remaining.sort(key=lambda entry: entry[1].casefold())
    if remaining:
        grouping.append(("Andre artikler", remaining))

    assets: dict[str, tuple[str, str]] = {}
    chapter_files = {
        filename: _chapter_xhtml(
            article_title,
            content,
            _language_for(article.url),
            chapter_by_url,
            assets,
        )
        for (filename, article_title, content), article in zip(chapters, articles)
    }
    nav = _nav_xhtml("Indhold", grouping, primary_language)
    opf = _content_opf(title, identifier, languages, chapters, assets, modified)

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with zipfile.ZipFile(temporary, "w") as archive:
            mimetype = zipfile.ZipInfo("mimetype")
            mimetype.compress_type = zipfile.ZIP_STORED
            archive.writestr(mimetype, "application/epub+zip")
            archive.writestr(
                "META-INF/container.xml",
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
                "  <rootfiles>\n"
                '    <rootfile full-path="OEBPS/content.opf" '
                'media-type="application/oebps-package+xml" />\n'
                "  </rootfiles>\n"
                "</container>\n",
                compress_type=zipfile.ZIP_DEFLATED,
            )
            archive.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
            archive.writestr("OEBPS/nav.xhtml", nav, compress_type=zipfile.ZIP_DEFLATED)
            archive.writestr("OEBPS/style.css", _CSS, compress_type=zipfile.ZIP_DEFLATED)
            for filename, chapter in chapter_files.items():
                archive.writestr(f"OEBPS/{filename}", chapter, compress_type=zipfile.ZIP_DEFLATED)
            for source, (filename, _) in assets.items():
                archive.write(source, f"OEBPS/images/{filename}", compress_type=zipfile.ZIP_DEFLATED)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path
