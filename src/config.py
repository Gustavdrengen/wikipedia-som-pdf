import subprocess
from pathlib import Path


def _git_email() -> str:
    try:
        return subprocess.run(
            ["git", "config", "--get", "user.email"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


_REPOSITORY_URL = "https://github.com/Gustavdrengen/wikipedia-som-pdf"
_GIT_EMAIL = _git_email()
USER_AGENT = f"ArticlePdfGeneratorBot/1.0 ({_REPOSITORY_URL}; {_GIT_EMAIL})" if _GIT_EMAIL else f"ArticlePdfGeneratorBot/1.0 ({_REPOSITORY_URL})"
MAX_RETRIES = 3
MAX_RETRY_WAIT_SECONDS = 30.0
MAX_CONCURRENCY = 1
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
