from html import escape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from app.security.canvas_resources import canvas_file_id

ALLOWED_TAGS = {"p", "br", "h1", "h2", "h3", "h4", "h5", "h6", "b", "strong", "i", "em", "u", "ul", "ol", "li", "pre", "code", "blockquote", "hr", "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption"}
BLOCKED_CONTENT = {"script", "style", "svg", "math", "iframe", "object", "embed", "canvas", "video", "audio"}


class _SafeCanvasParser(HTMLParser):
    def __init__(self, preserve_image_slots: bool = False, preserve_file_links: bool = False):
        super().__init__(convert_charrefs=True); self.output = []; self.blocked_depth = 0; self.preserve_image_slots = preserve_image_slots; self.image_index = 0
        self.preserve_file_links = preserve_file_links
        self.anchors = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in BLOCKED_CONTENT:
            self.blocked_depth += 1; return
        if self.blocked_depth: return
        if tag == "a":
            values = dict(attrs)
            file_id = canvas_file_id(values.get("href") or "")
            if file_id is None:
                file_id = canvas_file_id(values.get("data-api-endpoint") or "")
            keep = self.preserve_file_links and file_id is not None
            self.anchors.append(keep)
            if keep:
                self.output.append(f'<a href="canvas-file:{file_id}">')
            return
        if tag == "img":
            if self.preserve_image_slots:
                self.output.append(f"CANVAS_IMAGE_SLOT_{self.image_index:04d}")
            else:
                self.output.append("<p><i>[Image omitted in safe viewer]</i></p>")
            self.image_index += 1
            return
        if tag in ALLOWED_TAGS:
            self.output.append(f"<{tag}>")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in BLOCKED_CONTENT:
            if self.blocked_depth: self.blocked_depth -= 1
            return
        if tag == "a" and not self.blocked_depth:
            if self.anchors and self.anchors.pop():
                self.output.append("</a>")
            return
        if not self.blocked_depth and tag in ALLOWED_TAGS and tag not in {"br", "hr"}:
            self.output.append(f"</{tag}>")

    def handle_data(self, data):
        if not self.blocked_depth: self.output.append(escape(data))


def safe_canvas_html(value: str | None, empty_message: str = "No content provided.", preserve_image_slots: bool = False, *, preserve_file_links: bool = False) -> str:
    if not value: return f"<p><i>{escape(empty_message)}</i></p>"
    parser = _SafeCanvasParser(preserve_image_slots, preserve_file_links)
    try:
        parser.feed(value[:250_000]); parser.close()
    except Exception:
        return f"<pre>{escape(value[:250_000])}</pre>"
    result = "".join(parser.output)
    if len(value) > 250_000: result += "<p><i>[Content truncated for safe display]</i></p>"
    return result or f"<p><i>{escape(empty_message)}</i></p>"


class _ImageSourceParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True); self.sources = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "img": return
        source = dict(attrs).get("src")
        if source: self.sources.append(source)


def canvas_image_sources(value: str | None, base_url: str, limit: int = 6) -> list[str]:
    if not value: return []
    parser = _ImageSourceParser()
    try: parser.feed(value[:250_000]); parser.close()
    except Exception: return []
    origin = urlparse(base_url)
    result = []
    for source in parser.sources:
        absolute = urljoin(base_url, source)
        parsed = urlparse(absolute)
        if parsed.scheme == "https" and parsed.netloc == origin.netloc and absolute not in result:
            result.append(absolute)
        if len(result) >= limit: break
    return result


def canvas_image_slots(value: str | None, base_url: str, limit: int = 6) -> list[tuple[int, str]]:
    if not value: return []
    parser = _ImageSourceParser()
    try: parser.feed(value[:250_000]); parser.close()
    except Exception: return []
    origin = urlparse(base_url); result = []
    for index, source in enumerate(parser.sources):
        absolute = urljoin(base_url, source); parsed = urlparse(absolute)
        if parsed.scheme == "https" and parsed.netloc == origin.netloc:
            result.append((index, absolute))
        if len(result) >= limit: break
    return result
