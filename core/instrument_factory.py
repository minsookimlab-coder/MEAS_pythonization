import importlib
from typing import Any

from config.config_models import InstrumentConfig
from core.network_utils import resolve_address


class InstrumentFactory:
    """
    Factory for instantiating instrument classes dynamically based on their configuration.
    """

    def __init__(self, resource_manager: Any):
        self.rm = resource_manager

    def create_instrument(self, config: InstrumentConfig):
        """
        Dynamically loads and instantiates an instrument class based on the given configuration.
        """
        try:
            module_path, class_name = config.class_name.rsplit('.', 1)
            module = importlib.import_module(module_path)
            instrument_class = getattr(module, class_name)

            # DHCP/동적 IP 방어 — MAC 주소 기반 실시간 IP 추적
            real_address = resolve_address(config.interface_type, config.address, config.mac_address)

            return instrument_class(
                resource_manager=self.rm,
                alias=config.alias,
                interface_type=config.interface_type,
                address=real_address,
                port=config.port,
                **config.extra_params
            )

        except ValueError:
            raise ValueError(f"Invalid class_name format: '{config.class_name}'. Must be 'module_path.ClassName'")
        except ImportError as e:
            raise ImportError(f"Failed to load module '{module_path}': {e}")
        except AttributeError:
            raise AttributeError(f"Class '{class_name}' not found in module '{module_path}'")
