from pathlib import Path

USER_AGENT = "ArticlePdfGenerator/1.0 (https://github.com/your-name/article-pdf-generator; contact: replace-with-your-email@example.com)"
MAX_RETRIES = 5
MAX_CONCURRENCY = 2
DEFAULT_MEDIA_REQUEST_DELAY = 1.0
CACHE_MAX_AGE_SECONDS = 31 * 24 * 60 * 60
CACHE_DIR = Path(".article-cache")
MEDIA_CACHE_DIR = Path(".article-media-cache")
DEFAULT_REQUEST_DELAY = 1.0
SUPPORTED_TAGS = {
    "p", "br", "hr", "b", "i", "s", "u", "font", "center", "a", "pre",
    "code", "ol", "ul", "li", "dl", "dt", "dd", "table", "thead", "tbody",
    "tfoot", "tr", "th", "td", "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "sup", "sub", "img",
}
VOID_TAGS = {"br", "hr", "img"}
