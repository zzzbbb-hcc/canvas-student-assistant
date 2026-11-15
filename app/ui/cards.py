from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QVBoxLayout
from app.ui.theme import PALETTE as P


class CardList(QListWidget):
    def __init__(self, parent=None, *, compact=False):
        super().__init__(parent)
        self.compact = compact
        self.setObjectName("cardList")
        self.setSpacing(4 if compact else 10)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.itemSelectionChanged.connect(self._update_selection_style)

    def add_card(self, title, subtitle="", meta="", badge="", accent=P.accent, *, status="", status_color=P.text_secondary):
        item = QListWidgetItem(self)
        card = QFrame(); card.setObjectName("contentCard")
        card.setProperty("selected", False)
        card.setProperty("minimumCardHeight", 64 if self.compact else 116 if status else 94)
        outer = QHBoxLayout(card)
        if self.compact:
            outer.setContentsMargins(12, 7, 12, 7)
        else:
            outer.setContentsMargins(18, 13, 18, 13)
        outer.setSpacing(10 if self.compact else 14)
        stripe = QFrame(); stripe.setFixedWidth(3 if self.compact else 5); stripe.setStyleSheet(f"background:{accent}; border-radius:2px;"); outer.addWidget(stripe)
        text = QVBoxLayout(); text.setSpacing(2 if self.compact else 4)
        heading = QLabel(title); heading.setObjectName("cardTitle"); heading.setTextFormat(Qt.TextFormat.PlainText); heading.setWordWrap(True); text.addWidget(heading)
        if self.compact:
            heading.setStyleSheet("font-size: 15px; font-weight: 600;")
        if subtitle:
            sub = QLabel(subtitle); sub.setObjectName("cardSubtitle"); sub.setTextFormat(Qt.TextFormat.PlainText); sub.setWordWrap(True); text.addWidget(sub)
            if self.compact:
                sub.setStyleSheet("font-size: 13px;")
        if meta:
            detail = QLabel(meta); detail.setObjectName("cardMeta"); detail.setTextFormat(Qt.TextFormat.PlainText); detail.setWordWrap(True); text.addWidget(detail)
            if self.compact:
                detail.setStyleSheet("font-size: 12px;")
        outer.addLayout(text, 1)
        badges = QVBoxLayout(); badges.setSpacing(6)
        if status:
            status_label = QLabel(status); status_label.setObjectName("submissionBadge")
            status_label.setTextFormat(Qt.TextFormat.PlainText); status_label.setWordWrap(True)
            status_label.setAlignment(Qt.AlignmentFlag.AlignCenter); status_label.setMaximumWidth(180)
            padding = "3px 8px" if self.compact else "6px 10px"
            status_background = {P.success: P.success_bg, P.danger: P.danger_bg, P.warning: P.warning_bg}.get(status_color, P.neutral_bg)
            status_label.setStyleSheet(f"color: {status_color}; background: {status_background}; border: 1px solid {P.border}; border-radius: 8px; padding: {padding}; font-weight: 500;" + ("font-size: 12px;" if self.compact else ""))
            if self.compact:
                status_label.setMinimumWidth(108); status_label.setMaximumWidth(150)
            badges.addWidget(status_label)
        if badge and not self.compact:
            label = QLabel(badge); label.setObjectName("cardBadge"); label.setAlignment(Qt.AlignmentFlag.AlignCenter); badges.addWidget(label)
        if status or (badge and not self.compact):
            badges.addStretch(); outer.addLayout(badges)
        self.setItemWidget(item, card)
        self._size_item(item, card)
        item.setToolTip("\n".join(str(value) for value in (title, subtitle, status, meta, badge) if value))
        return item

    def add_section(self, title, subtitle="", *, level=0):
        """Add a non-interactive heading without changing card/detail identity."""
        item = QListWidgetItem(self)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        section = QFrame(); section.setObjectName("courseSection" if level == 0 else "statusSection")
        section.setProperty("minimumCardHeight", (44 if subtitle else 26) if self.compact else (64 if subtitle else 38))
        box = QVBoxLayout(section)
        box.setContentsMargins(4, (6 if self.compact else 12) if level == 0 else 0, 4, 2)
        box.setSpacing(2 if self.compact else 3)
        label = QLabel(title); label.setTextFormat(Qt.TextFormat.PlainText); label.setWordWrap(True)
        label.setStyleSheet(f"font-size: {16 if self.compact else 18}px; font-weight: 600; color: {P.text};" if level == 0 else f"font-size: 13px; font-weight: 500; color: {P.text_secondary};")
        box.addWidget(label)
        if subtitle:
            detail = QLabel(subtitle); detail.setTextFormat(Qt.TextFormat.PlainText); detail.setWordWrap(True)
            detail.setStyleSheet(f"color: {P.text_muted}; font-size: {12 if self.compact else 13}px;"); box.addWidget(detail)
        self.setItemWidget(item, section)
        self._size_item(item, section)
        return item

    def _update_selection_style(self):
        for row in range(self.count()):
            item = self.item(row)
            card = self.itemWidget(item)
            if card is not None and card.objectName() == "contentCard" and card.property("selected") != item.isSelected():
                card.setProperty("selected", item.isSelected())
                card.style().unpolish(card)
                card.style().polish(card)
                card.update()

    def _size_item(self, item, widget):
        width = max(240, self.viewport().width() - 2 * self.spacing() - 4)
        height = widget.layout().totalHeightForWidth(width)
        item.setSizeHint(QSize(width, max(int(widget.property("minimumCardHeight") or 0), height, widget.minimumSizeHint().height())))

    def _resize_cards(self):
        for row in range(self.count()):
            item = self.item(row); widget = self.itemWidget(item)
            if widget is not None:
                self._size_item(item, widget)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self._resize_cards)
