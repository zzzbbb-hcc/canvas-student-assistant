"""Small original SVG doodles: warm ink, soft fills, and rounded hand-drawn paths.

The vector sources live in code so frozen builds need no new data collection.
"""

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer


_DRAWINGS = {
    "dashboard": '<path fill="#FFF1BC" d="M4 11Q3 10 5 8L10 3Q12 1.8 14 4l6 5q2 2 0 3v7q0 2-2 2H6q-2 0-2-2z"/><path d="M9 21v-6q3-2 6 0v6M3 11Q8 7 12 3q5 4 9 8"/>',
    "courses": '<path fill="#FFF0C8" d="M12 6Q8 2.5 3 4v15q5-1.4 9 1.5Q16 18 21 19V4q-5-1.3-9 2z"/><path d="M12 6v14M6 8q2-.2 3 1M6 12q2-.2 3 1M15 9q1.5-1 3-1M15 13q1.5-1 3-1"/>',
    "assignments": '<path fill="#FFF7DC" d="M7 4H5q-1.5 0-1.5 2v14q0 2 2 2h13q2 0 2-2V6q0-2-2-2h-2"/><path fill="#F4D9C7" d="M9 3q0-2 3-2t3 2l1 3H8z"/><path d="m7 11 1 1 2-2M12 11h5m-10 6 1 1 2-2m2 1h5"/>',
    "quizzes": '<path fill="#FFF1CA" d="M6 2h9l4 4v14q0 2-2 2H6q-2 0-2-2V4q0-2 2-2z"/><path d="M14 2v5h5M9 11q0-3 3-3t3 3q0 1-2 2t-1 3"/><circle cx="12" cy="19" r=".8" fill="#88611E" stroke="none"/>',
    "deadlines": '<path fill="#FFF1C0" d="M21 12q.5 8-9 9-8-.2-9-9 .1-8 9-9 8 .5 9 9z"/><path d="M12 6q-.5 4 0 7l4 2M4 3 2 6M20 3l2 3M6 20l-1 2M18 20l1 2"/>',
    "announcements": '<path fill="#F8DECD" d="M4 10q8-1 15-6v15q-6-4-15-5z"/><path fill="#FFF3CB" d="m7 15 2 6h4l-2-6"/><path d="M3 10q-2 2 0 4M21 8l1-1M21 15l1 1M7 9v6"/>',
    "assistant": '<path fill="#FFF0CA" d="M4 4q8-3 16 0 2 6 0 12l-9 1-5 4v-5H4Q1 10 4 4z"/><path fill="#F1D5AF" d="m12 6 1.5 3.5L17 11l-3.5 1.5L12 16l-1.5-3.5L7 11l3.5-1.5z"/>',
    "settings": '<path fill="#F4E4C5" d="m10 2 4 .1.5 3 2 1.2 3-.9 2 3.5-2.3 2.1.1 2.4 2.1 2-2.1 3.5-3-.8-2 1.1-.5 3-4-.1-.6-2.8-2-1.3-2.8.9L2.3 16l2.2-2.2V11L2.2 9l2-3.5 3 .9 2-1.3z"/><path fill="#FFFEF8" d="M16 12q0 4-4 4t-4-4 4-4 4 4z"/>',
}


def navigation_icon(key: str) -> QIcon:
    """Render real-size frames without fonts, emoji, files, or network access."""
    drawing = _DRAWINGS.get(key, _DRAWINGS["assistant"])
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
           f'<g fill="none" stroke="#88611E" stroke-width="1.55" '
           f'stroke-linecap="round" stroke-linejoin="round">{drawing}</g></svg>')
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    icon = QIcon()
    for size in (16, 20, 24, 32, 48, 64):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        renderer.render(painter, QRectF(1, 1, size - 2, size - 2))
        painter.end()
        icon.addPixmap(pixmap)
    return icon
