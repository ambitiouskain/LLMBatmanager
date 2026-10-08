from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


MODEL_ROLE_COLORS = {
    "dark": {
        "mmproj": "#68c7cb",
        "draft": "#b49add",
        "adapter": "#e3b66f",
        "control": "#91a9c4",
        "embedding": "#75b8dd",
        "auxiliary": "#9ba9bc",
        "unknown": "#9ba9bc",
    },
    "light": {
        "mmproj": "#16777d",
        "draft": "#70519c",
        "adapter": "#8a5a12",
        "control": "#4f6987",
        "embedding": "#246b95",
        "auxiliary": "#66758a",
        "unknown": "#66758a",
    },
}


def model_role_color(app: QApplication | None, role: str) -> QColor:
    """Return a centralized, theme-aware semantic color for a model-file role."""
    theme = (
        str(app.property("llmbatdeskTheme"))
        if app is not None and app.property("llmbatdeskTheme")
        else "dark"
    )
    return QColor(
        MODEL_ROLE_COLORS.get(theme, MODEL_ROLE_COLORS["dark"]).get(
            role, MODEL_ROLE_COLORS.get(theme, MODEL_ROLE_COLORS["dark"])["unknown"]
        )
    )


DARK_QSS = """
QWidget { background: #10151d; color: #e7edf6; }
QMainWindow, QDialog { background: #10151d; }
QFrame#Card { background: #171e29; border: 1px solid #263244; border-radius: 9px; }
QLabel#Hero { font-size: 18pt; font-weight: 650; }
QLabel#Muted { color: #9ba9bc; }
QLabel#Badge { background: #233047; border: 1px solid #344867; border-radius: 8px; padding: 3px 8px; }
QLabel#GoodBadge { background: #173c35; color: #8ee6c4; border-radius: 8px; padding: 3px 8px; }
QLabel#WarnBadge { background: #49361e; color: #ffd38a; border-radius: 8px; padding: 3px 8px; }
QLabel#ErrorBadge { background: #4b252b; color: #ffadb5; border-radius: 8px; padding: 3px 8px; }
QLineEdit, QPlainTextEdit, QTextEdit, QTreeView, QTableView, QTabWidget::pane, QSpinBox {
  background: #141b25; border: 1px solid #2b384b; border-radius: 6px; padding: 5px;
  selection-background-color: #3468b2;
}
QLineEdit:focus, QPlainTextEdit:focus, QTreeView:focus, QTableView:focus {
  border: 1px solid #5b9cff;
}
QTreeView::item, QTableView::item { padding: 5px; }
QTreeView::item:selected, QTableView::item:selected { background: #315f9f; color: white; }
QTreeView::item:hover, QTableView::item:hover { background: #202c3d; }
QTableView#LaunchHistoryTable::item { background: #141b25; }
QTableView#LaunchHistoryTable::item:hover { background: #202c3d; }
QTableView#LaunchHistoryTable::item:selected { background: #315f9f; color: white; }
QHeaderView::section { background: #1d2735; border: 0; border-right: 1px solid #2b384b; padding: 7px; }
QTabBar::tab { background: #171f2b; color: #aeb9c8; padding: 8px 13px; border: 0; }
QTabBar::tab:selected { background: #2e65ad; color: white; }
QPushButton, QToolButton {
  background: #202b3a; border: 1px solid #334258; border-radius: 6px; padding: 6px 11px;
}
QPushButton:hover, QToolButton:hover { background: #2a3a50; border-color: #4f77aa; }
QPushButton:pressed, QToolButton:pressed { background: #182232; }
QPushButton:disabled, QToolButton:disabled { color: #647084; background: #161c26; border-color: #242d3b; }
QPushButton#PrimaryButton, QToolButton#PrimaryButton { background: #3478d4; color: white; border-color: #4d91ea; font-weight: 600; }
QPushButton#PrimaryButton:hover, QToolButton#PrimaryButton:hover { background: #4288e4; }
QPushButton[semanticRole="start"]:enabled { background: #237a52; color: white; border-color: #3fa978; font-weight: 650; }
QPushButton[semanticRole="start"]:hover { background: #2d8d61; }
QPushButton[semanticRole="danger"]:enabled { background: #8f3540; color: white; border-color: #c45a67; font-weight: 650; }
QPushButton[semanticRole="danger"]:hover { background: #a6414d; }
QPushButton[semanticRole="destructive"]:enabled { background: #40252c; color: #ffb4bd; border-color: #9d4652; font-weight: 600; }
QPushButton[semanticRole="destructive"]:hover { background: #573039; color: white; border-color: #bd5663; }
QPushButton[semanticRole="warning"]:enabled { background: #80551e; color: #fff3d6; border-color: #bb8135; font-weight: 600; }
QPushButton[semanticRole="warning"]:hover { background: #966526; }
QPushButton[semanticRole="info"]:enabled { background: #285f9d; color: white; border-color: #4a86c9; }
QPushButton[semanticRole="error_hint"]:enabled { border-color: #a94b56; color: #ffbdc4; }
QToolBar { background: #151c27; border-bottom: 1px solid #283548; spacing: 4px; padding: 6px; }
QStatusBar { background: #151c27; color: #aeb9c8; }
QSplitter::handle { background: #263244; width: 4px; }
QSplitter::handle:hover { background: #4a83ce; }
QScrollBar:vertical { background: transparent; width: 11px; }
QScrollBar::handle:vertical { background: #3a4a60; border-radius: 5px; min-height: 28px; }
QMenu { background: #18212d; border: 1px solid #34445a; }
QMenu::item:selected { background: #315f9f; }
"""


LIGHT_QSS = """
QWidget { background: #f3f6fa; color: #182233; }
QMainWindow, QDialog { background: #f3f6fa; }
QFrame#Card { background: white; border: 1px solid #d9e1eb; border-radius: 9px; }
QLabel#Hero { font-size: 18pt; font-weight: 650; }
QLabel#Muted { color: #66758a; }
QLabel#Badge { background: #e9eef6; border: 1px solid #d1dbe9; border-radius: 8px; padding: 3px 8px; }
QLabel#GoodBadge { background: #dff5ec; color: #12664f; border-radius: 8px; padding: 3px 8px; }
QLabel#WarnBadge { background: #fff0d3; color: #87570c; border-radius: 8px; padding: 3px 8px; }
QLabel#ErrorBadge { background: #ffe2e5; color: #982b38; border-radius: 8px; padding: 3px 8px; }
QLineEdit, QPlainTextEdit, QTextEdit, QTreeView, QTableView, QTabWidget::pane, QSpinBox {
  background: white; border: 1px solid #cfd9e6; border-radius: 6px; padding: 5px;
  selection-background-color: #7db1ee;
}
QLineEdit:focus, QPlainTextEdit:focus, QTreeView:focus, QTableView:focus { border: 1px solid #3178ce; }
QTreeView::item, QTableView::item { padding: 5px; }
QTreeView::item:selected, QTableView::item:selected { background: #bfdcff; color: #10233e; }
QTreeView::item:hover, QTableView::item:hover { background: #e8f2ff; }
QTableView#LaunchHistoryTable::item { background: white; }
QTableView#LaunchHistoryTable::item:hover { background: #edf2f7; }
QTableView#LaunchHistoryTable::item:selected { background: #bfdcff; color: #10233e; }
QHeaderView::section { background: #e7edf5; border: 0; border-right: 1px solid #d1dae6; padding: 7px; }
QTabBar::tab { background: #e7edf5; color: #526176; padding: 8px 13px; border: 0; }
QTabBar::tab:selected { background: #3478d4; color: white; }
QPushButton, QToolButton { background: #edf2f8; border: 1px solid #c9d5e3; border-radius: 6px; padding: 6px 11px; }
QPushButton:hover, QToolButton:hover { background: #deebfa; border-color: #75a4db; }
QPushButton:disabled, QToolButton:disabled { color: #9aa6b5; background: #eef1f5; }
QPushButton#PrimaryButton, QToolButton#PrimaryButton { background: #276fc7; color: white; border-color: #276fc7; font-weight: 600; }
QPushButton[semanticRole="start"]:enabled { background: #218653; color: white; border-color: #187044; font-weight: 650; }
QPushButton[semanticRole="danger"]:enabled { background: #b43846; color: white; border-color: #982e3a; font-weight: 650; }
QPushButton[semanticRole="destructive"]:enabled { background: #fff1f2; color: #a52d3b; border-color: #c95c68; font-weight: 600; }
QPushButton[semanticRole="destructive"]:hover { background: #ffe1e4; color: #8f2632; border-color: #b7424f; }
QPushButton[semanticRole="warning"]:enabled { background: #c27616; color: white; border-color: #a9620e; font-weight: 600; }
QPushButton[semanticRole="info"]:enabled { background: #3478d4; color: white; border-color: #2767bb; }
QPushButton[semanticRole="error_hint"]:enabled { border-color: #bf4b58; color: #9d2936; }
QToolBar { background: white; border-bottom: 1px solid #d5deea; spacing: 4px; padding: 6px; }
QStatusBar { background: white; color: #637188; }
QSplitter::handle { background: #d1dae6; width: 4px; }
QSplitter::handle:hover { background: #5792d5; }
"""


def apply_theme(app: QApplication, theme: str) -> None:
    app.setStyle("Fusion")
    app.setStyleSheet(LIGHT_QSS if theme == "light" else DARK_QSS)
    app.setProperty("llmbatdeskTheme", theme)
    palette = QPalette()
    if theme == "light":
        palette.setColor(QPalette.ColorRole.Window, QColor("#f3f6fa"))
        palette.setColor(QPalette.ColorRole.WindowText, QColor("#182233"))
        palette.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
        palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#f7f9fc"))
        palette.setColor(QPalette.ColorRole.Text, QColor("#182233"))
    else:
        palette.setColor(QPalette.ColorRole.Window, QColor("#10151d"))
        palette.setColor(QPalette.ColorRole.WindowText, QColor("#e7edf6"))
        palette.setColor(QPalette.ColorRole.Base, QColor("#141b25"))
        palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#171e29"))
        palette.setColor(QPalette.ColorRole.Text, QColor("#e7edf6"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#3478d4"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    app.setPalette(palette)
