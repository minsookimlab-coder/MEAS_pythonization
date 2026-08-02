from abc import ABC
from core.network_utils import build_visa_string


class BaseInstrument(ABC):
    """
    Abstract Base Class for all instruments.
    Provides default implementations for common VISA operations.
    Subclasses override _post_connect() for device-specific setup,
    and may override query() or test_connection() for non-standard protocols.
    """

    # --- Subclass-configurable defaults ---
    _default_timeout = 5000
    _default_read_termination = '\n'
    _default_write_termination = '\n'

    def __init__(self, resource_manager, alias="", interface_type="LAN", address="", port=None, **kwargs):
        self.rm = resource_manager
        self.alias = alias
        self.interface_type = interface_type
        self.address = address
        self.port = port
        self.extra_params = kwargs
        self.inst = None

        self.timeout = int(self.extra_params.get("timeout", self._default_timeout))
        self.read_termination = self.extra_params.get("read_termination", self._default_read_termination)
        self.write_termination = self.extra_params.get("write_termination", self._default_write_termination)

    def _build_resource_string(self) -> str:
        """Builds the VISA resource string based on interface type and address."""
        return build_visa_string(self.interface_type, self.address, self.port)

    def _post_connect(self):
        """
        Hook called after the VISA resource is opened.
        Override in subclasses to apply device-specific settings
        (termination characters, baud rate, etc.).
        """
        pass

    def connect(self):
        """Establish connection to the hardware."""
        resource_str = self._build_resource_string()
        print(f"[{self.alias}] Connecting to {resource_str} ...")
        self.inst = self.rm.open_resource(resource_str)
        self.inst.timeout = self.timeout
        self.inst.read_termination  = self.read_termination
        self.inst.write_termination = self.write_termination
        self._post_connect()

    def disconnect(self):
        """Terminate connection to the hardware."""
        if self.inst:
            try:
                self.inst.close()
            finally:
                # 닫힌 VISA 핸들에 대한 직접 I/O 방지: 이후 write/read/query의
                # `if not self.inst` 가드가 통과돼 closed handle에 접근하던 문제 차단.
                self.inst = None

    def write(self, cmd: str):
        """Send a command string to the instrument."""
        if not self.inst:
            raise ConnectionError(f"[{self.alias}] Not connected.")
        self.inst.write(cmd)

    def read(self) -> str:
        """Read a response string from the instrument."""
        if not self.inst:
            raise ConnectionError(f"[{self.alias}] Not connected.")
        return self.inst.read()

    def query(self, cmd: str) -> str:
        """Send a command and read the response."""
        if not self.inst:
            raise ConnectionError(f"[{self.alias}] Not connected.")
        return self.inst.query(cmd)

    def test_connection(self) -> str:
        """
        Test the hardware connection and return an identification string.
        By default, uses the standard SCPI '*IDN?'.
        Override in subclasses for non-SCPI instruments.
        """
        return self.query("*IDN?")
