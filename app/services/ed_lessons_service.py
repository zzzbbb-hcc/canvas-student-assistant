"""Explicit unsupported boundary for Ed Lessons.

The service intentionally performs no HTTP requests until Ed publishes or the
institution supplies a stable, authorised API contract.
"""

UNSUPPORTED_API = "UNSUPPORTED_API"


class EdLessonsService:
    status = UNSUPPORTED_API
    reason = (
        "Ed Lessons content API is not available for this account. "
        "Lesson, module and content endpoints have not been officially confirmed."
    )

    def get_lesson_courses(self):
        return UNSUPPORTED_API

    def get_lesson_modules(self, course_id):
        return UNSUPPORTED_API

    def get_lessons(self, course_id):
        return UNSUPPORTED_API

    def get_lesson(self, lesson_id):
        return UNSUPPORTED_API

    def get_lesson_content(self, lesson_id):
        return UNSUPPORTED_API
