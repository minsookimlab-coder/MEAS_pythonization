# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec — Pythonization v1.0

import sys
from pathlib import Path

ROOT = Path(SPECPATH)

a = Analysis(
    [str(ROOT / 'main.py')],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        # settings 폴더 (프로파일 YAML 등) 를 번들에 포함
        (str(ROOT / 'settings'), 'settings'),
    ],
    hiddenimports=[
        # pyvisa 백엔드
        'pyvisa',
        'pyvisa.resources',
        'pyvisa.highlevel',
        'pyvisa_py',
        # PySide6 플러그인
        'PySide6.QtCore',
        'PySide6.QtWidgets',
        'PySide6.QtGui',
        'PySide6.QtCharts',
        'PySide6.QtOpenGL',
        'PySide6.QtOpenGLWidgets',
        # pydantic
        'pydantic',
        'pydantic.v1',
        # yaml
        'yaml',
        # matplotlib (graph_window이 사용할 경우 대비)
        'matplotlib',
        'matplotlib.backends.backend_qt5agg',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'unittest',
        'email',
        'html',
        'http',
        'urllib',
        'xmlrpc',
        'test',
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Pythonization',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,          # 콘솔 창 없음 (GUI 전용)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version='version_info.txt',   # 버전 정보 (아래 파일 생성)
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Pythonization',
)
