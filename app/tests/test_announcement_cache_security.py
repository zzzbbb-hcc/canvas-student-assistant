from app.ui.course_detail import CourseDetailDialog


def test_cached_announcement_omits_embedded_urls_and_private_author_fields():
    cached = CourseDetailDialog._cacheable_announcement(
        {
            "id": 42,
            "title": "Week 5",
            "message": (
                '<p>Read this update.</p><img src="https://canvas.example/file?token=secret">'
                '<a href="https://canvas.example/private?session=secret">link</a>'
            ),
            "posted_at": "2026-10-12T01:00:00Z",
            "author": {
                "display_name": "Teaching team",
                "email": "private@example.test",
                "id": 123,
            },
            "html_url": "https://canvas.example/announcement?token=secret",
        }
    )

    encoded = repr(cached)
    assert cached["message"] == "<p>Read this update.</p><p><i>[Image omitted in safe viewer]</i></p>link"
    assert cached["author"] == {"display_name": "Teaching team"}
    assert "https://" not in encoded
    assert "private@example.test" not in encoded
    assert "secret" not in encoded
