# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — Pythonization.

빌드:  pyinstaller build_exe.spec
결과:  dist/Pythonization/Pythonization.exe

사용자 데이터(프로파일·instruments.yaml·로그)는 번들에 넣지 않는다. 실행 시
~/Documents/pythonization/settings 아래에 만들어지며, 위치는 app_config.yaml 의
data_dir 로 바꿀 수 있다 (pythonization/app/paths.py 참고).
"""
from pathlib import Path

ROOT = Path(SPECPATH)

a = Analysis(
    [str(ROOT / 'main.py')],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        # GUI 정적 자산 (whale.png 등). 소스 레이아웃과 같은 위치에 둬야
        # pythonization/ui/assets/asset_path() 가 그대로 찾는다.
        (str(ROOT / 'pythonization' / 'ui' / 'assets'), 'pythonization/ui/assets'),
    ],
    hiddenimports=[
        # pyvisa 백엔드 — 런타임에 문자열로 선택되므로 정적 분석에 안 잡힌다
        'pyvisa',
        'pyvisa.resources',
        'pyvisa.highlevel',
        'pyvisa_py',
        # PySide6
        'PySide6.QtCore',
        'PySide6.QtWidgets',
        'PySide6.QtGui',
        'PySide6.QtOpenGL',
        'PySide6.QtOpenGLWidgets',
        # pydantic
        'pydantic',
        # yaml
        'yaml',
        # 드라이버는 instruments.yaml 의 문자열로 importlib 로딩된다 →
        # 정적 분석이 못 보므로 패키지째 포함해야 한다.
        'pythonization.instruments.drivers',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'test',
        # 주의: email / urllib / http / smtplib 는 제외하면 안 된다.
        # notify/alarm_manager.py 가 이메일(smtplib+email.mime)과
        # 텔레그램(urllib.request)에 쓴다 — 제외하면 알람이 exe 에서만 깨진다.
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
    console=False,          # GUI 전용 — 콘솔 창 없음
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version='version_info.txt',
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
