# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_submodules

model_library_hiddenimports = [
    "llmbatdesk.extensions.model_library",
    "llmbatdesk.extensions.model_library.models",
    "llmbatdesk.extensions.model_library.storage",
    "llmbatdesk.extensions.model_library.gguf_reader",
    "llmbatdesk.extensions.model_library.quantization",
    "llmbatdesk.extensions.model_library.presentation",
    "llmbatdesk.extensions.model_library.scanner",
    "llmbatdesk.extensions.model_library.references",
    "llmbatdesk.extensions.model_library.service",
    "llmbatdesk.extensions.model_library.qt_models",
    "llmbatdesk.extensions.model_library.page",
]
hiddenimports = collect_submodules("pydantic") + model_library_hiddenimports
qt_excludes = [
    "tkinter",
    "PySide6.Qt3DAnimation", "PySide6.Qt3DCore", "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DRender",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtLocation", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtPositioning",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtSensors",
    "PySide6.QtSerialBus", "PySide6.QtSerialPort", "PySide6.QtSpatialAudio",
    "PySide6.QtSql", "PySide6.QtTextToSpeech", "PySide6.QtWebChannel",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebSockets",
]

a = Analysis(
    ["src/llmbatdesk/__main__.py"],
    pathex=["src"],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=qt_excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="LLMBatDesk-Portable",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    manifest="windows_dpi.manifest",
    icon="assets/LLMBatDesk.ico",
)
