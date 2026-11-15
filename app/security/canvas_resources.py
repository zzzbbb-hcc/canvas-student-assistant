"""Extract Canvas file identities without preserving authenticated download URLs."""

from dataclasses import dataclass
from html.parser import HTMLParser
import re
from urllib.parse import urljoin, urlsplit

DEFAULT_CANVAS_URL = "https://canvas.sydney.edu.au"
_FILE_PATH = re.compile(r"/(?:courses/[0-9]+/)?files/([0-9]+)(?:/(?:download|preview))?")
_API_FILE_PATH = re.compile(r"/api/v1/(?:courses/[0-9]+/)?files/([0-9]+)")
_IGNORED_CONTENT = {"script", "style", "svg", "math", "iframe", "object", "embed", "canvas", "video", "audio", "template", "noscript", "textarea", "title", "xmp", "plaintext"}
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_MAX_HTML_LENGTH = 250_000


@dataclass(frozen=True)
class CanvasFileLink:
    file_id: int
    title: str


def _same_origin_path(url: str | None, base_url: str) -> str | None:
    if not url or not isinstance(url, str):
        return None
    # Browsers normalize some of these characters before interpreting a URL.
    if any(ord(character) <= 32 or ord(character) == 127 for character in url) or "\\" in url:
        return None
    if not url.startswith("/") and not url.lower().startswith("https://"):
        return None
    if url.startswith("///"):
        return None
    try:
        origin = urlsplit(base_url)
        parsed = urlsplit(urljoin(base_url, url))
        if origin.scheme != "https" or not origin.hostname or origin.username is not None or origin.password is not None:
            return None
        if parsed.scheme != "https" or parsed.username is not None or parsed.password is not None:
            return None
        parsed_port = 443 if parsed.port is None else parsed.port
        origin_port = 443 if origin.port is None else origin.port
        if (parsed.hostname, parsed_port) != (origin.hostname, origin_port):
            return None
    except (TypeError, ValueError):
        return None
    return parsed.path


def _path_file_id(path: str | None, api_only: bool = False) -> int | None:
    if path is None:
        return None
    match = _API_FILE_PATH.fullmatch(path)
    if match is None and not api_only:
        match = _FILE_PATH.fullmatch(path)
    if match is None:
        return None
    try:
        file_id = int(match.group(1))
    except ValueError:
        return None
    return file_id if file_id > 0 else None


def canvas_file_id(url: str | None, base_url: str = DEFAULT_CANVAS_URL) -> int | None:
    """Return a file ID only for an HTTPS file URL on the configured Canvas origin."""
    return _path_file_id(_same_origin_path(url, base_url))


class _CanvasFileParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: list[CanvasFileLink] = []
        self.seen: set[int] = set()
        self.blocked: list[str] = []
        self.anchor_id: int | None = None
        self.anchor_text: list[str] = []

    def _finish_anchor(self):
        if self.anchor_id is not None and self.anchor_id not in self.seen:
            title = " ".join("".join(self.anchor_text).split()) or f"File {self.anchor_id}"
            self.links.append(CanvasFileLink(self.anchor_id, title))
            self.seen.add(self.anchor_id)
        self.anchor_id = None
        self.anchor_text = []

    def handle_starttag(self, tag, attrs):
        if tag in _IGNORED_CONTENT:
            if tag not in _VOID_TAGS:
                self.blocked.append(tag)
            return
        if self.blocked:
            return
        if tag == "a":
            self._finish_anchor()
            attributes = dict(attrs)
            self.anchor_id = canvas_file_id(attributes.get("href"), self.base_url)
            if self.anchor_id is None:
                endpoint_path = _same_origin_path(attributes.get("data-api-endpoint"), self.base_url)
                self.anchor_id = _path_file_id(endpoint_path, api_only=True)
        elif tag in {"br", "hr"} and self.anchor_id is not None:
            self.anchor_text.append(" ")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.blocked:
            if tag in self.blocked:
                matching_index = len(self.blocked) - 1 - self.blocked[::-1].index(tag)
                del self.blocked[matching_index:]
            return
        if tag == "a":
            self._finish_anchor()

    def handle_data(self, data):
        if not self.blocked and self.anchor_id is not None:
            self.anchor_text.append(data)


def canvas_file_links(html: str | None, base_url: str = DEFAULT_CANVAS_URL) -> list[CanvasFileLink]:
    """Extract unique file IDs and visible anchor labels from assignment HTML."""
    if not html:
        return []
    parser = _CanvasFileParser(base_url)
    try:
        parser.feed(html[:_MAX_HTML_LENGTH])
        parser.close()
        parser._finish_anchor()
    except Exception:
        return []
    return parser.links
