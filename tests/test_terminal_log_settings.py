from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QPushButton

from llmbatdesk.qt.dialogs import DeleteUserDataDialog, SettingsDialog, TrustDialog
from llmbatdesk.qt.terminal_log import (
    LogSeverity, TerminalLogView, classify_severity, detect_urls, strip_ansi,
)
from llmbatdesk.settings import AppSettings


def test_url_detection_ansi_and_severity_classification() -> None:
    text = "\x1b[31mERROR failed\x1b[0m http://127.0.0.1:9878 https://example.com."
    assert strip_ansi(text) == "ERROR failed http://127.0.0.1:9878 https://example.com."
    assert detect_urls(text) == ["http://127.0.0.1:9878", "https://example.com"]
    assert classify_severity(text) == LogSeverity.ERROR
    assert classify_severity("W: low memory") == LogSeverity.WARNING
    assert classify_severity("model loaded, listening") == LogSeverity.SUCCESS
    assert classify_severity("DEBUG trace") == LogSeverity.DEBUG


def test_terminal_preserves_raw_search_pause_auto_scroll_and_clear(
    qtbot, qapp, tmp_path: Path
) -> None:
    view = TerminalLogView()
    qtbot.addWidget(view)
    raw = "\x1b[32mmodel loaded\x1b[0m\nERROR failure\nhttp://127.0.0.1:9878"
    view.set_log_text(raw)
    assert view.raw_text == raw
    assert "\x1b" not in view.toPlainText()
    assert view.find_text("failure")
    view.set_auto_scroll(False)
    assert not view.auto_scroll
    view.set_visual_updates_paused(True)
    view.set_log_text(raw + "\nnew line")
    assert "new line" not in view.toPlainText()
    view.set_visual_updates_paused(False)
    assert "new line" in view.toPlainText()
    stored = tmp_path / "runtime.log"
    stored.write_text(view.raw_text, encoding="utf-8")
    view.clear_view()
    assert view.toPlainText() == ""
    assert view.raw_text.endswith("new line")
    assert stored.exists() and stored.read_text(encoding="utf-8") == view.raw_text
    view.copy_complete_log()
    assert qapp.clipboard().text() == view.raw_text


def test_url_click_signal(monkeypatch, qtbot) -> None:
    view = TerminalLogView()
    qtbot.addWidget(view)
    opened: list[str] = []
    emitted: list[str] = []
    monkeypatch.setattr(
        "llmbatdesk.qt.terminal_log.QDesktopServices.openUrl",
        lambda url: opened.append(url.toString()) or True,
    )
    view.urlActivated.connect(emitted.append)
    view._activate_url(QUrl("http://127.0.0.1:9878"))
    assert emitted == opened == ["http://127.0.0.1:9878"]


def test_settings_dialog_sections_storage_actions_and_defaults(qtbot, tmp_path: Path) -> None:
    dialog = SettingsDialog(AppSettings(), tmp_path)
    qtbot.addWidget(dialog)
    labels = [dialog.tabs.tabText(index) for index in range(dialog.tabs.count())]
    assert labels == ["常规", "启动", "日志", "存储与清理", "外观", "功能扩展"]
    assert dialog.minimumWidth() >= 600
    buttons = {button.objectName() for button in dialog.findChildren(QPushButton)}
    assert {
        "settings_open_data", "settings_clear_all_logs", "settings_cleanup_logs",
        "settings_clear_history", "settings_clear_temporary", "settings_reset_ui",
        "settings_delete_all_data",
    } <= buttons
    assert dialog.settings.log_retention_days == 30
    assert dialog.settings.log_max_total_mb == 200
    assert dialog.settings.log_max_count == 100


def test_delete_data_dialog_requires_exact_typed_confirmation(qtbot, tmp_path: Path) -> None:
    dialog = DeleteUserDataDialog(tmp_path)
    qtbot.addWidget(dialog)
    delete_button = next(
        button for button in dialog.findChildren(QPushButton)
        if button.text() == "永久删除"
    )
    assert not delete_button.isEnabled()
    dialog.confirmation.setText("删除")
    assert not delete_button.isEnabled()
    dialog.confirmation.setText("删除全部数据")
    assert delete_button.isEnabled()
