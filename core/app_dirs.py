"""
App directory 관리.

저장소 2분할:
  - 전역 설정(app_config.yaml): 프로그램이 들어있는 폴더(APP_DIR)에서 통합 관리.
    측정 임계값·병렬 측정 토글 등 단순 앱 설정 + 사용자 데이터 폴더(data_dir) 포인터.
  - 사용자 데이터(SETTINGS_DIR): 프로파일·계측기·VNA 설정·resume 등 개인 정보가
    담길 수 있는 파일. 기본값은 ~/Documents/pythonization/settings 이며,
    Settings → Config 에서 다른 경로로 지정 가능(변경 후 재시작 시 적용).

SETTINGS_DIR 는 프로그램 시작 시 전역 config의 data_dir 값으로 한 번 확정된다.
경로를 바꾸면 set_data_dir()로 전역 config에 기록되고, 재시작 후 반영된다.
"""
import sys
from pathlib import Path

import yaml


def _app_dir() -> Path:
    """프로그램이 들어있는 폴더.
    - PyInstaller 등으로 frozen된 경우: 실행 파일 폴더
    - 일반 실행: 프로젝트 루트 (core/ 의 상위)
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


APP_DIR = _app_dir()
# 전역 설정 파일 — 프로그램 폴더에서 통합 관리
GLOBAL_CONFIG_PATH = APP_DIR / "app_config.yaml"
# 사용자 데이터 기본 폴더 (개인 정보 가능)
DEFAULT_DATA_DIR = Path.home() / "Documents" / "pythonization" / "settings"


def _read_data_dir() -> Path:
    """전역 config에서 data_dir 값을 읽는다. 없거나 오류면 기본 폴더."""
    try:
        if GLOBAL_CONFIG_PATH.exists():
            with open(GLOBAL_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            d = data.get("data_dir")
            if d:
                return Path(str(d)).expanduser()
    except Exception:
        pass
    return DEFAULT_DATA_DIR


# 사용자 데이터 폴더 — 시작 시 한 번 확정 (변경은 재시작 후 적용)
SETTINGS_DIR = _read_data_dir()


def get_data_dir() -> Path:
    """현재(시작 시점에 확정된) 사용자 데이터 폴더."""
    return SETTINGS_DIR


def get_configured_data_dir() -> str:
    """전역 config에 저장된 data_dir 문자열(비어 있으면 기본값)."""
    try:
        if GLOBAL_CONFIG_PATH.exists():
            with open(GLOBAL_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            d = data.get("data_dir")
            if d:
                return str(d)
    except Exception:
        pass
    return str(DEFAULT_DATA_DIR)


def set_data_dir(path: str) -> None:
    """전역 config(app_config.yaml)에 data_dir 를 기록한다. 재시작 후 적용.

    빈 문자열이면 키를 제거하여 기본 폴더로 되돌린다.
    """
    try:
        data = {}
        if GLOBAL_CONFIG_PATH.exists():
            with open(GLOBAL_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        if path and path.strip():
            data["data_dir"] = path.strip()
        else:
            data.pop("data_dir", None)
        GLOBAL_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(GLOBAL_CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True)
    except Exception as e:
        print(f"[app_dirs] set_data_dir failed: {e}")
