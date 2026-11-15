import pytest

from app.security.canvas_resources import CanvasFileLink, canvas_file_id, canvas_file_links


@pytest.mark.parametrize("url", [
    "/files/123",
    "/files/123/download?download_frd=1&verifier=private-value",
    "/files/123/preview#page=2",
    "/courses/456/files/123",
    "/courses/456/files/123/download?token=private-value",
    "/courses/456/files/123/preview",
    "/api/v1/files/123?access_token=private-value",
    "/api/v1/courses/456/files/123?access_token=private-value",
    "https://canvas.sydney.edu.au/files/123",
    "https://CANVAS.SYDNEY.EDU.AU:443/api/v1/files/123",
    "//canvas.sydney.edu.au/files/123",
])
def test_recognizes_canvas_file_url_shapes(url):
    assert canvas_file_id(url) == 123


@pytest.mark.parametrize("url", [
    None, "", "#files/123", "files/123", "?file=/files/123",
    "/files/-123", "/files/0", "/files/not-a-number", "/files/１２３",
    "/courses/abc/files/123", "/files/123/unexpected", "/api/v1/files/123/download",
    "/api/v1/courses/abc/files/123", "///files/123",
    "/files/123/preview/extra", "/files/%31%32%33",
    "http://canvas.sydney.edu.au/files/123",
    "https://evil.invalid/files/123", "//evil.invalid/files/123",
    "https://canvas.sydney.edu.au.evil.invalid/files/123",
    "https://canvas.sydney.edu.au:444/files/123",
    "https://canvas.sydney.edu.au:0/files/123",
    "https://user:secret@canvas.sydney.edu.au/files/123",
    "https://@canvas.sydney.edu.au/files/123",
    "https://canvas.sydney.edu.au@evil.invalid/files/123",
    "https://canvas.sydney.edu.au:bad/files/123",
    "https://[invalid/files/123", "https://canvas.sydney.edu.au\\@evil.invalid/files/123",
    "javascript:/files/123", "data:text/html,/files/123", "file:///files/123",
    " https://canvas.sydney.edu.au/files/123", "https://canvas.sydney.edu.au/fi\nles/123",
])
def test_rejects_non_file_urls_and_untrusted_origins(url):
    assert canvas_file_id(url) is None


def test_respects_configured_https_origin():
    assert canvas_file_id("/files/42", "https://canvas.example.edu:8443") == 42
    assert canvas_file_id("https://canvas.example.edu:8443/files/42", "https://canvas.example.edu:8443") == 42
    assert canvas_file_id("https://canvas.example.edu/files/42", "https://canvas.example.edu:8443") is None
    assert canvas_file_id("/files/42", "http://canvas.example.edu") is None
    assert canvas_file_id("/files/42", "https://user:secret@canvas.example.edu") is None


def test_extracts_labels_deduplicates_ids_and_discards_url_credentials():
    html = '''
        <p><a href="/courses/9/files/71/download?verifier=private-signature&amp;token=private-token">
            Assignment <strong>brief</strong> &amp; rubric
        </a></p>
        <a href="https://canvas.sydney.edu.au/api/v1/files/71">Duplicate label</a>
        <a href="/files/72/preview?access_token=private-access"></a>
        <a href="/files/73">Marking<br>guide</a>
        <img src="/files/74/download" alt="Not an anchor">
    '''
    links = canvas_file_links(html)
    assert links == [
        CanvasFileLink(71, "Assignment brief & rubric"),
        CanvasFileLink(72, "File 72"),
        CanvasFileLink(73, "Marking guide"),
    ]
    assert all("private-" not in repr(link) and "https://" not in repr(link) for link in links)


def test_uses_only_trusted_file_api_endpoint_as_fallback():
    html = '''
        <a href="/courses/9/pages/resources" data-api-endpoint="https://canvas.sydney.edu.au/api/v1/files/81?token=secret">Template</a>
        <a data-api-endpoint="/api/v1/files/82">Checklist</a>
        <a data-api-endpoint="/api/v1/courses/9/files/87">Course checklist</a>
        <a href="/files/83" data-api-endpoint="/api/v1/files/999">Prefer href</a>
        <a href="https://evil.invalid/files/84" data-api-endpoint="https://evil.invalid/api/v1/files/84">External</a>
        <a data-api-endpoint="/files/85/download">Not an API endpoint</a>
        <a data-api-endpoint="http://canvas.sydney.edu.au/api/v1/files/86">HTTP endpoint</a>
    '''
    assert canvas_file_links(html) == [
        CanvasFileLink(81, "Template"), CanvasFileLink(82, "Checklist"),
        CanvasFileLink(87, "Course checklist"), CanvasFileLink(83, "Prefer href"),
    ]


@pytest.mark.parametrize("tag", ["script", "style", "iframe", "object", "svg", "math", "canvas", "video", "audio", "template", "noscript", "textarea", "title", "xmp"])
def test_ignores_anchors_inside_blocked_content(tag):
    html = f'<{tag}><a href="/files/91">Hidden</a></{tag}><a href="/files/92">Visible</a>'
    assert canvas_file_links(html) == [CanvasFileLink(92, "Visible")]


def test_nested_blocked_content_cannot_be_escaped_by_unmatched_closing_tag():
    html = '''
        <iframe></style><object><a href="/files/101">Hidden</a></object>
        <a href="/files/102">Still hidden</a></iframe>
        <embed src="/files/103"><a href="/files/104">Visible after void element</a>
        <svg/><a href="/files/105">Visible after self-closing block</a>
        <a href="/files/106">Visible<script>ignored</script> label</a>
    '''
    assert canvas_file_links(html) == [
        CanvasFileLink(104, "Visible after void element"),
        CanvasFileLink(105, "Visible after self-closing block"),
        CanvasFileLink(106, "Visible label"),
    ]


def test_empty_unclosed_and_oversized_html():
    assert canvas_file_links(None) == []
    assert canvas_file_links("") == []
    assert canvas_file_links('<a href="/files/111">Unclosed') == [CanvasFileLink(111, "Unclosed")]
    assert canvas_file_links(" " * 250_000 + '<a href="/files/112">Outside limit</a>') == []
