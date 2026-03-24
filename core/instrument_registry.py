"""
InstrumentRegistry: instruments.yaml 기반 alias → VISA 주소 조회 클래스.
InstrumentFactory와 달리 실제 연결 없이 설정 조회만 수행합니다.
"""
import yaml
from pathlib import Path
from typing import Dict, List, Optional

from config.config_models import InstrumentConfig
from core.network_utils import resolve_address, build_visa_string
from core.app_dirs import SETTINGS_DIR


def _default_settings_path() -> Path:
    return SETTINGS_DIR / "instruments.yaml"


class InstrumentRegistry:
    """
    instruments.yaml을 로드하여 alias 기반으로 장비 설정과 VISA 주소를 제공합니다.
    호출 시점마다 파일을 다시 읽으므로 설정 변경이 즉시 반영됩니다.
    """

    def __init__(self, settings_file: Optional[Path] = None):
        self._path = settings_file or _default_settings_path()
        self._configs: Dict[str, InstrumentConfig] = {}
        self._load()

    def _load(self):
        self._configs.clear()
        if not self._path.exists():
            return
        try:
            with open(self._path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
            if isinstance(data, list):
                for item in data:
                    try:
                        config = InstrumentConfig(**item)
                        self._configs[config.alias] = config
                    except Exception as e:
                        print(f"[Registry] Skipping invalid entry: {e}")
        except Exception as e:
            print(f"[Registry] Failed to load {self._path}: {e}")

    def reload(self):
        """설정 파일을 다시 읽습니다."""
        self._load()

    def list_aliases(self) -> List[str]:
        """등록된 모든 alias 목록을 반환합니다."""
        return list(self._configs.keys())

    def get_config(self, alias: str) -> Optional[InstrumentConfig]:
        """alias에 해당하는 InstrumentConfig를 반환합니다."""
        return self._configs.get(alias)

    def get_visa_address(self, alias: str) -> Optional[str]:
        """
        alias에 해당하는 VISA 리소스 문자열을 반환합니다.
        MAC 주소가 등록된 경우 ARP로 현재 IP를 추적합니다.
        장비가 없으면 None을 반환합니다.
        """
        config = self._configs.get(alias)
        if not config:
            return None
        real_address = resolve_address(config.interface_type, config.address, config.mac_address or "")
        return build_visa_string(config.interface_type, real_address, config.port)
