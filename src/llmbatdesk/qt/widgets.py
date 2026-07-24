from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QAction, QFontMetrics, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QMenu, QPushButton, QSizePolicy, QVBoxLayout,
    QWidget,
)


UNKNOWN_VALUES = {"", "—", "未知", "未确定", "未指定"}


class ElidedLabel(QLabel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.full_text = ""
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def set_full_text(self, value: str) -> None:
        self.full_text = value
        self.setToolTip(value)
        self._update_elision()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_elision()

    def _update_elision(self) -> None:
        if not self.full_text:
            super().setText("—")
            return
        super().setText(
            QFontMetrics(self.font()).elidedText(
                self.full_text, Qt.TextElideMode.ElideMiddle, max(40, self.width())
            )
        )


class CopyField(QFrame):
    copied = Signal(str)
    open_file = Signal(str)
    open_folder = Signal(str)

    def __init__(self, title: str, shortcut: QKeySequence | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.value = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 7, 8, 7)
        layout.setSpacing(2)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("Muted")
        layout.addWidget(self.title_label)
        row = QHBoxLayout()
        self.value_label = ElidedLabel()
        row.addWidget(self.value_label, 1)
        self.copy_button = QPushButton("复制")
        self.copy_button.setAccessibleName(f"复制{title}")
        self.copy_button.clicked.connect(self.copy)
        row.addWidget(self.copy_button)
        layout.addLayout(row)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)
        if shortcut:
            action = QAction(self)
            action.setShortcut(shortcut)
            action.triggered.connect(self.copy)
            self.addAction(action)
        self.set_value("")

    def set_value(self, value: object, path_kind: str = "") -> None:
        raw = "" if value is None else str(value)
        self.value = "" if raw in UNKNOWN_VALUES else raw
        self.path_kind = path_kind
        self.value_label.set_full_text(self.value)
        available = bool(self.value)
        self.copy_button.setEnabled(available)
        self.copy_button.setToolTip("复制完整值" if available else "当前没有可复制的值")

    def copy(self) -> None:
        if not self.value:
            return
        QApplication.clipboard().setText(self.value)
        self.copy_button.setText("已复制")
        self.copied.emit(self.value)
        QTimer.singleShot(2000, lambda: self.copy_button.setText("复制"))

    def _context_menu(self, point) -> None:
        menu = QMenu(self)
        copy_action = menu.addAction("复制")
        copy_action.setEnabled(bool(self.value))
        copy_action.triggered.connect(self.copy)
        if self.value and self.path_kind in {"file", "folder"}:
            menu.addSeparator()
            open_action = menu.addAction("打开文件" if self.path_kind == "file" else "打开文件夹")
            open_action.triggered.connect(
                lambda: self.open_file.emit(self.value)
                if self.path_kind == "file" else self.open_folder.emit(self.value)
            )
            if self.path_kind == "file":
                folder_action = menu.addAction("打开所在文件夹")
                folder_action.triggered.connect(lambda: self.open_folder.emit(str(Path(self.value).parent)))
        menu.exec(self.mapToGlobal(point))


class EmptyState(QWidget):
    def __init__(self, title: str, detail: str, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.addStretch()
        heading = QLabel(title)
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        heading.setObjectName("Hero")
        layout.addWidget(heading)
        body = QLabel(detail)
        body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body.setWordWrap(True)
        body.setObjectName("Muted")
        layout.addWidget(body)
        layout.addStretch()
