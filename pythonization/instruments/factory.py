"""InstrumentConfig 의 class_name 문자열로 드라이버 인스턴스를 만든다.

instruments.yaml 에는 드라이버가 'pkg.module.ClassName' 문자열로 저장된다.
즉 **드라이버 파일을 옮기면 기존 사용자 설정이 깨진다** — 그래서 구 경로를 현재
경로로 옮겨주는 매핑을 둔다(_LEGACY_MODULES). 사용자가 Instrument Settings 에서
한 번 저장하면 새 경로로 갱신된다.
"""
import importlib
import logging
from typing import Any

from pythonization.config.models import InstrumentConfig
from pythonization.util.network import resolve_address

log = logging.getLogger(__name__)

_DRIVER_PKG = "pythonization.instruments.drivers"

# 새 장비를 등록할 때 Instrument Settings 가 채워 넣는 기본 드라이버.
DEFAULT_DRIVER_CLASS_PATH = f"{_DRIVER_PKG}.lakeshore_m81.M81Instrument"

# 구 레이아웃(driver/*.py, 그 이전 core/*.py)에서 저장된 모듈 경로 → 현재 경로.
# 기존 랩 PC의 instruments.yaml 을 그대로 읽기 위한 하위 호환 계층이다.
_LEGACY_MODULES = {
    "driver.generic_scpi":     f"{_DRIVER_PKG}.generic_scpi",
    "driver.dummy_instrument": f"{_DRIVER_PKG}.dummy",
    "driver.keithley_2636a":   f"{_DRIVER_PKG}.keithley_2636a",
    "driver.oxford_itc":       f"{_DRIVER_PKG}.oxford_itc",
    "driver.oxford_ips":       f"{_DRIVER_PKG}.oxford_ips",
    "driver.m81":              f"{_DRIVER_PKG}.lakeshore_m81",
    "driver.sr830":            f"{_DRIVER_PKG}.srs_sr830",
    "driver.mfli":             f"{_DRIVER_PKG}.zurich_mfli",
    # 드라이버가 core/ 에 있던 더 오래된 레이아웃
    "core.dummy_instrument":   f"{_DRIVER_PKG}.dummy",
    "core.generic_scpi":       f"{_DRIVER_PKG}.generic_scpi",
}


def resolve_class_path(class_name: str) -> str:
    """저장된 class_name 을 현재 레이아웃 기준 경로로 옮긴다.

    이미 현재 경로면 그대로 돌려준다. 모르는 경로도 그대로 둔다 — 사용자가 직접
    적어 넣은 외부 드라이버일 수 있으므로 여기서 판단하지 않는다.
    """
    module_path, _, cls = class_name.rpartition(".")
    new_module = _LEGACY_MODULES.get(module_path)
    if new_module is None:
        return class_name
    log.info("구 드라이버 경로 '%s' 를 '%s' 로 해석했습니다 "
             "(Instrument Settings 에서 저장하면 갱신됩니다).", module_path, new_module)
    return f"{new_module}.{cls}"


class InstrumentFactory:
    """설정의 클래스 경로를 동적으로 import 해 드라이버를 인스턴스화한다."""

    def __init__(self, resource_manager: Any):
        self.rm = resource_manager

    def create_instrument(self, config: InstrumentConfig):
        class_name = resolve_class_path(config.class_name)
        module_path, _, class_only = class_name.rpartition(".")
        if not module_path or not class_only:
            raise ValueError(
                f"class_name 형식이 잘못되었습니다: '{config.class_name}'. "
                "'module_path.ClassName' 형식이어야 합니다.")

        # try 범위를 좁게 유지한다. 예전에는 인스턴스 생성까지 한 try 안에 있어서
        # 드라이버 __init__ 이 던진 ValueError/AttributeError 가 'class_name 형식
        # 오류'/'클래스 없음' 으로 둔갑해 원인 파악을 방해했다.
        try:
            module = importlib.import_module(module_path)
        except ImportError as e:
            raise ImportError(f"드라이버 모듈을 불러오지 못했습니다 '{module_path}': {e}")

        try:
            instrument_class = getattr(module, class_only)
        except AttributeError:
            raise AttributeError(
                f"클래스 '{class_only}' 가 모듈 '{module_path}' 에 없습니다.")

        # DHCP/동적 IP 방어 — MAC 주소 기반 실시간 IP 추적
        real_address = resolve_address(
            config.interface_type, config.address, config.mac_address)

        return instrument_class(
            resource_manager=self.rm,
            alias=config.alias,
            interface_type=config.interface_type,
            address=real_address,
            port=config.port,
            **config.extra_params
        )
