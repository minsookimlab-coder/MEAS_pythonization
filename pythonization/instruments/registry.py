"""
InstrumentRegistry: instruments.yaml 기반 alias → VISA 주소 조회 클래스.
InstrumentFactory와 달리 실제 연결 없이 설정 조회만 수행합니다.
"""
import yaml
from pathlib import Path
from typing import Dict, List, Optional

from pythonization.config.models import InstrumentConfig
from pythonization.util.network import resolve_address, build_visa_string
from pythonization.app.paths import SETTINGS_DIR


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
        self._visa_addr_cache: Dict[str, str] = {}   # alias → VISA 주소 (ARP 결과 캐시)
        self._load()

    def _load(self):
        self._configs.clear()
        self._visa_addr_cache.clear()   # 설정 변경 시 캐시 초기화
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

    def update_address(self, alias: str, new_ip: str) -> bool:
        """
        instruments.yaml에서 alias 장비의 저장된 IP(address)를 new_ip로 갱신하고
        파일에 영구 저장합니다. DHCP 등으로 IP가 바뀐 것이 확인됐을 때 사용합니다.

        - 기존 yaml의 다른 필드는 그대로 보존한 채 address만 교체합니다.
        - 성공 시 in-memory 설정과 VISA 주소 캐시도 즉시 갱신합니다.
        반환값: 실제로 변경·저장됐으면 True.
        """
        if not self._path.exists():
            return False
        try:
            with open(self._path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
            if not isinstance(data, list):
                return False
            changed = False
            for item in data:
                if isinstance(item, dict) and item.get("alias") == alias:
                    if item.get("address") != new_ip:
                        item["address"] = new_ip
                        changed = True
                    break
            if not changed:
                return False
            with open(self._path, 'w', encoding='utf-8') as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        except Exception as e:
            print(f"[Registry] update_address('{alias}') 실패: {e}")
            return False

        # in-memory 반영
        cfg = self._configs.get(alias)
        if cfg is not None:
            try:
                cfg.address = new_ip
            except Exception:
                pass
        self._visa_addr_cache.pop(alias, None)
        return True

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
        결과는 캐시되므로 ARP subprocess는 alias당 최초 1회만 실행됩니다.
        장비가 없으면 None을 반환합니다.
        """
        if alias in self._visa_addr_cache:
            return self._visa_addr_cache[alias]
        config = self._configs.get(alias)
        if not config:
            return None
        real_address = resolve_address(config.interface_type, config.address, config.mac_address or "")
        result = build_visa_string(config.interface_type, real_address, config.port)
        self._visa_addr_cache[alias] = result
        return result
