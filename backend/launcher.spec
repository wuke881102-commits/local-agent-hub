# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Local Agent Hub backend (tray mode, --onedir)."""
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

hiddenimports = [
    "app.agents.html_page",
    "app.agents.document_map",
    "app.agents.index_enrich",
    "app.agents.knowledge_governance",
    "app.agents.meeting_minutes",
    "app.agents.base_analysis",
    "app.agents.pdf_recognition",
    "app.agents.collab_dispatch",
    "app.agents.local_image",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.logging",
    "pydantic.deprecated.decorator",
    "openai",
    "aiosqlite",
    # tray dependencies
    "pystray._win32",
    "PIL.Image",
    "PIL.ImageDraw",
    "PIL.ImageFont",
    # 本地目录：截图（活动窗口）+ 全局快捷键
    "PIL.ImageGrab",
    "keyboard",
    # 文件解析：PDF + Office/HTML（云盘上传文件）
    "fitz",
    "docx",
    "pptx",
    "et_xmlfile",
    "bs4",
    "soupsieve",
    "lxml",
    "lxml.etree",
    "lxml._elementpath",
]
# openpyxl 纯 Python，子模块较多，全量收集以防漏导。
hiddenimports += collect_submodules("openpyxl")

# Bundled data: HTML templates + the tray icon.
datas = [
    ("app/html/templates", "app/html/templates"),
    ("../build/icon.png", "."),
    ("../build/icon.ico", "."),
]
# python-docx 读取时会用到包内 templates/default.docx 等数据文件。
datas += collect_data_files("docx")
# python-pptx 同样自带 templates/default.pptx 等数据文件。
datas += collect_data_files("pptx")

a = Analysis(
    ["launcher.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "matplotlib", "numpy", "pandas", "scipy",
        "tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6",
        "IPython", "jupyter",
        "pytest", "pytest_asyncio",
    ],
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LocalAgentHub",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # console=False -> no black cmd window. App lives in the tray.
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="../build/icon.ico",
    version="version_info.txt",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="LocalAgentHub",
)
