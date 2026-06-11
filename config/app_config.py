"""
전역 앱 설정 (프로파일과 무관하게 공유되는 설정).
저장 위치: 프로그램 폴더의 app_config.yaml (core.app_dirs.GLOBAL_CONFIG_PATH).

- global_threshold / parallel_measurement : 단순 앱 설정
- data_dir : 사용자 데이터(프로파일·계측기·VNA·resume) 폴더 경로.
             빈 문자열이면 기본값(~/Documents/pythonization/settings).
             변경은 재시작 후 반영된다.
"""
import yaml

from pydantic import BaseModel

from core.app_dirs import GLOBAL_CONFIG_PATH, DEFAULT_DATA_DIR

APP_CONFIG_PATH = GLOBAL_CONFIG_PATH
# 구 버전 위치 (Documents) — 신규 위치에 파일이 없을 때 1회 폴백 로드용
_LEGACY_CONFIG_PATH = DEFAULT_DATA_DIR / "app_config.yaml"


class AppConfig(BaseModel):
    # 측정값 abs 상한선: |value| > global_threshold → np.nan 처리
    global_threshold: float = 1e38
    # 병렬 측정: 서로 다른 계측기를 동시에 측정 (False면 기존처럼 순차)
    parallel_measurement: bool = False
    # 사용자 데이터 폴더 경로 (빈 문자열 = 기본 Documents 폴더). 재시작 후 적용.
    data_dir: str = ""


def load_app_config() -> AppConfig:
    # 1순위: 신규 위치(프로그램 폴더)
    try:
        if APP_CONFIG_PATH.exists():
            with open(APP_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            return AppConfig(**data)
    except Exception:
        pass
    # 2순위: 구 위치(Documents) — threshold/parallel 등 기존 설정 보존 (data_dir 제외)
    try:
        if _LEGACY_CONFIG_PATH.exists():
            with open(_LEGACY_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            data.pop("data_dir", None)   # 구 파일의 data_dir은 무시 (신규는 기본 폴더)
            return AppConfig(**data)
    except Exception:
        pass
    return AppConfig()


def save_app_config(cfg: AppConfig) -> None:
    try:
        APP_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(APP_CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.dump(cfg.model_dump(), f, allow_unicode=True)
    except Exception as e:
        print(f"[AppConfig] Save failed: {e}")
