"""
전역 앱 설정 (프로파일과 무관하게 공유되는 설정).
저장 위치: SETTINGS_DIR / app_config.yaml
"""
import yaml
from pathlib import Path

from pydantic import BaseModel

from core.app_dirs import SETTINGS_DIR

APP_CONFIG_PATH = SETTINGS_DIR / "app_config.yaml"


class AppConfig(BaseModel):
    # 측정값 abs 상한선: |value| > global_threshold → np.nan 처리
    global_threshold: float = 1e38


def load_app_config() -> AppConfig:
    try:
        if APP_CONFIG_PATH.exists():
            with open(APP_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
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
