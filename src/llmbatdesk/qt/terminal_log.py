from __future__ import annotations

import html
import re
from enum import StrEnum

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QFontDatabase, QTextCursor, QTextDocument
from PySide6.QtWidgets import QApplication, QMenu, QTextBrowser


ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
URL_RE = re.compile(r"https?://[^\s<>'\"\]\[()]+")


class LogSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    SUCCESS = "success"
    DEBUG = "debug"
    INFO = "info"


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def detect_urls(text: str) -> list[str]:
    return [match.group(0).rstrip(".,;:!?") for match in URL_RE.finditer(strip_ansi(text))]


def classify_severity(line: str) -> LogSeverity:
    value = strip_ansi(line).casefold()
    if re.search(r"(^|\W)(error|failed|fatal|e)(\W|$)", value) or "错误" in value or "失败" in value:
        return LogSeverity.ERROR
    if re.search(r"(^|\W)(warning|warn|w)(\W|$)", value) or "警告" in value:
        return LogSeverity.WARNING
    if any(token in value for token in ("success", "ready", "loaded", "listening", "成功", "就绪")):
        return LogSeverity.SUCCESS
    if re.search(r"(^|\W)(debug|trace|d)(\W|$)", value):
        return LogSeverity.DEBUG
    return LogSeverity.INFO


SEVERITY_COLORS = {
    LogSeverity.ERROR: "#ff6b76",
    LogSeverity.WARNING: "#f0b84b",
    LogSeverity.SUCCESS: "#55d69e",
    LogSeverity.DEBUG: "#7f8b9b",
    LogSeverity.INFO: "#d9e2ee",
}
PREFERRED_MONOSPACE_FONTS = ("Cascadia Mono", "Consolas")


def default_terminal_font_family() -> str:
    families = set(QFontDatabase.families())
    return next(
        (family for family in PREFERRED_MONOSPACE_FONTS if family in families),
        QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family(),
    )


class TerminalLogView(QTextBrowser):
    urlActivated = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.raw_text = ""
        self.auto_scroll = True
        self.visual_updates_paused = False
        self.case_sensitive = False
        self.enabled_severities = set(LogSeverity)
        self.configured_font_size = 11
        self.line_spacing_percent = 100
        self.setReadOnly(True)
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.configure_font("", 11, 100)
        self.anchorClicked.connect(self._activate_url)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        reset_action = QAction(self)
        reset_action.setShortcut("Ctrl+0")
        reset_action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        reset_action.triggered.connect(self.reset_font_size)
        self.addAction(reset_action)

    def configure_font(self, family: str, point_size: int, line_spacing_percent: int) -> None:
        selected = family if family in QFontDatabase.families() else default_terminal_font_family()
        self.configured_font_size = min(24, max(8, int(point_size)))
        self.line_spacing_percent = min(200, max(80, int(line_spacing_percent)))
        font = self.font()
        font.setFamily(selected)
        font.setPointSize(self.configured_font_size)
        self.setFont(font)
        self.render_log()

    def change_font_size(self, delta: int) -> None:
        font = self.font()
        font.setPointSize(min(24, max(8, font.pointSize() + delta)))
        self.setFont(font)
        self.render_log()

    def reset_font_size(self) -> None:
        font = self.font()
        font.setPointSize(self.configured_font_size)
        self.setFont(font)
        self.render_log()

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.change_font_size(1 if event.angleDelta().y() > 0 else -1)
            event.accept()
            return
        super().wheelEvent(event)

    def set_log_text(self, text: str) -> None:
        self.raw_text = text
        if not self.visual_updates_paused:
            self.render_log()

    def render_log(self) -> None:
        scroll = self.verticalScrollBar()
        previous = scroll.value()
        lines = []
        for raw_line in self.raw_text.splitlines():
            clean = strip_ansi(raw_line)
            severity = classify_severity(clean)
            if severity not in self.enabled_severities:
                continue
            lines.append(
                f'<div style="white-space:pre;color:{SEVERITY_COLORS[severity]}">'
                f"{self._linkify(clean)}</div>"
            )
        family = html.escape(self.font().family(), quote=True)
        self.setHtml(
            f"<html><body style=\"font-family:'{family}';font-size:{self.font().pointSize()}pt;"
            f"line-height:{self.line_spacing_percent}%;margin:5px\">"
            + "".join(lines) + "</body></html>"
        )
        if self.auto_scroll:
            scroll.setValue(scroll.maximum())
        else:
            scroll.setValue(min(previous, scroll.maximum()))

    def set_visual_updates_paused(self, paused: bool) -> None:
        self.visual_updates_paused = paused
        if not paused:
            self.render_log()

    def set_auto_scroll(self, enabled: bool) -> None:
        self.auto_scroll = enabled
        if enabled:
            self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    def set_severity_enabled(self, severity: LogSeverity, enabled: bool) -> None:
        if enabled:
            self.enabled_severities.add(severity)
        else:
            self.enabled_severities.discard(severity)
        self.render_log()

    def find_text(self, query: str, *, previous: bool = False) -> bool:
        if not query:
            return False
        flags = QTextDocument.FindFlag(0)
        if previous:
            flags |= QTextDocument.FindFlag.FindBackward
        if self.case_sensitive:
            flags |= QTextDocument.FindFlag.FindCaseSensitively
        if self.find(query, flags):
            return True
        cursor = self.textCursor()
        cursor.movePosition(
            QTextCursor.MoveOperation.End if previous else QTextCursor.MoveOperation.Start
        )
        self.setTextCursor(cursor)
        return self.find(query, flags)

    def jump_to_latest_error(self) -> bool:
        lines = strip_ansi(self.raw_text).splitlines()
        error = next(
            (line for line in reversed(lines) if classify_severity(line) == LogSeverity.ERROR),
            "",
        )
        return bool(error) and self.find_text(error, previous=True)

    def clear_view(self) -> None:
        self.clear()

    def copy_complete_log(self) -> None:
        QApplication.clipboard().setText(self.raw_text)

    @staticmethod
    def _linkify(line: str) -> str:
        output: list[str] = []
        position = 0
        for match in URL_RE.finditer(line):
            url = match.group(0).rstrip(".,;:!?")
            suffix = match.group(0)[len(url):]
            output.append(html.escape(line[position:match.start()]))
            escaped = html.escape(url, quote=True)
            output.append(
                f'<a href="{escaped}" style="color:#64a9ff;text-decoration:underline">'
                f"{escaped}</a>{html.escape(suffix)}"
            )
            position = match.end()
        output.append(html.escape(line[position:]))
        return "".join(output)

    def _activate_url(self, url: QUrl) -> None:
        value = url.toString()
        self.urlActivated.emit(value)
        QDesktopServices.openUrl(url)

    def _show_context_menu(self, point) -> None:
        menu = QMenu(self)
        cursor = self.cursorForPosition(point)
        link = cursor.charFormat().anchorHref()
        if link:
            open_action = menu.addAction("打开链接")
            open_action.triggered.connect(lambda: self._activate_url(QUrl(link)))
            copy_link = menu.addAction("复制链接")
            copy_link.triggered.connect(lambda: QApplication.clipboard().setText(link))
            menu.addSeparator()
        copy_line = menu.addAction("复制整行")
        copy_line.triggered.connect(lambda: self._copy_line(cursor))
        selected = menu.addAction("复制选中内容")
        selected.setEnabled(self.textCursor().hasSelection())
        selected.triggered.connect(self.copy)
        complete = menu.addAction("复制完整日志")
        complete.triggered.connect(self.copy_complete_log)
        menu.exec(self.mapToGlobal(point))

    @staticmethod
    def _copy_line(cursor: QTextCursor) -> None:
        cursor.select(QTextCursor.SelectionType.LineUnderCursor)
        QApplication.clipboard().setText(cursor.selectedText())
