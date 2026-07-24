from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Sequence

from PySide6.QtCore import QCoreApplication, Qt, QTimer
from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication, QIcon
from PySide6.QtWidgets import QApplication

from .. import __version__
from ..domain.models import OperationEvent
from ..services import ApplicationService
from .main_window import MainWindow
from . import resources_rc  # noqa: F401 - registers the embedded Qt resources
from .theme import apply_theme


PREFERRED_FONTS = ("Microsoft YaHei UI", "Segoe UI Variable", "Segoe UI")
APPLICATION_ICON_PATH = ":/assets/LLMBatDesk.ico"


def configure_high_dpi() -> None:
    """Configure scaling before QApplication exists."""
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    if os.name == "nt":
        try:
            # Per-monitor-v2; the packaged manifest declares the same policy.
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except (AttributeError, OSError):
            pass


def select_application_font(app: QApplication, point_size: float = 10.0) -> QFont:
    available = set(QFontDatabase.families())
    family = next((candidate for candidate in PREFERRED_FONTS if candidate in available), "")
    font = QFont(family, point_size) if family else app.font()
    font.setPointSizeF(point_size)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    app.setFont(font)
    return font


def apply_application_icon(app: QApplication) -> QIcon:
    icon = QIcon(APPLICATION_ICON_PATH)
    if icon.isNull():
        raise RuntimeError("内置应用图标资源无法加载")
    app.setWindowIcon(icon)
    return icon


def dpi_diagnostics(app: QApplication) -> str:
    screen = app.primaryScreen()
    font = app.font()
    if not screen:
        return (
            f"屏幕：不可用\n字体：{font.family()}\n有效字号：{font.pointSizeF():.2f} pt"
        )
    return (
        f"活动屏幕：{screen.name()}\n"
        f"逻辑 DPI：{screen.logicalDotsPerInch():.2f}\n"
        f"物理 DPI：{screen.physicalDotsPerInch():.2f}\n"
        f"设备像素比：{screen.devicePixelRatio():.3f}\n"
        f"应用字体：{font.family()}\n"
        f"有效字号：{font.pointSizeF():.2f} pt\n"
        f"高 DPI 舍入策略：PassThrough\n"
        f"Qt 平台：{QGuiApplication.platformName()}"
    )


def create_application(argv: Sequence[str] | None = None) -> QApplication:
    configure_high_dpi()
    app = QApplication(list(argv if argv is not None else sys.argv))
    QCoreApplication.setOrganizationName("LLMBatDesk")
    QCoreApplication.setApplicationName("LLMBatDesk")
    QCoreApplication.setApplicationVersion(__version__)
    apply_application_icon(app)
    select_application_font(app)
    return app


def run(argv: Sequence[str] | None = None, service: ApplicationService | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv)
    app = create_application(arguments)
    actual_service = service or ApplicationService()
    apply_theme(app, actual_service.settings.theme)
    diagnostics = dpi_diagnostics(app)
    actual_service.store.add_event(OperationEvent(kind="display_diagnostics", data={
        "details": diagnostics,
    }))
    window = MainWindow(actual_service, dpi_diagnostics=diagnostics)
    window.show()
    if "--smoke-test" in arguments:
        QTimer.singleShot(150, app.quit)
    return app.exec()
