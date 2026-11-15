from app.security.html_sanitizer import canvas_image_slots, canvas_image_sources, safe_canvas_html


def test_canvas_html_removes_native_renderer_risk_content():
    unsafe = '<style>*{x:y}</style><script>alert(1)</script><iframe src="x">bad</iframe><svg><path/></svg><img src="https://x"><h2>Week 1</h2><p>Hello <strong>student</strong></p>'
    result = safe_canvas_html(unsafe)
    lowered = result.lower()
    assert "script" not in lowered and "iframe" not in lowered and "svg" not in lowered and "src=" not in lowered
    assert "Week 1" in result and "Hello" in result and "student" in result
    assert "Image omitted" in result


def test_canvas_html_handles_empty_and_truncates_large_content():
    assert "No page" in safe_canvas_html(None, "No page")
    assert "Content truncated" in safe_canvas_html("x" * 250_001)


def test_canvas_image_sources_only_allows_same_origin_https():
    html = '<img src="/files/1/download"><img src="https://canvas.sydney.edu.au/files/2"><img src="https://evil.invalid/x.png"><img src="data:image/png;base64,x">'
    assert canvas_image_sources(html, "https://canvas.sydney.edu.au") == [
        "https://canvas.sydney.edu.au/files/1/download",
        "https://canvas.sydney.edu.au/files/2",
    ]


def test_canvas_image_slots_preserve_original_positions():
    html = '<p>Before<img src="/a.png">Middle<img src="https://evil.invalid/x">After<img src="/b.png"></p>'
    safe = safe_canvas_html(html, preserve_image_slots=True)
    assert safe.index("Before") < safe.index("CANVAS_IMAGE_SLOT_0000") < safe.index("Middle") < safe.index("CANVAS_IMAGE_SLOT_0001") < safe.index("After")
    assert canvas_image_slots(html, "https://canvas.sydney.edu.au") == [(0, "https://canvas.sydney.edu.au/a.png"), (2, "https://canvas.sydney.edu.au/b.png")]
