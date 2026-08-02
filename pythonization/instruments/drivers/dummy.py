from pythonization.instruments.base import BaseInstrument


class DummyInstrument(BaseInstrument):
    """
    A dummy instrument class for testing dynamic loading and parameter passing
    without requiring actual hardware connections.
    """

    def connect(self):
        print(f"[DummyInstrument '{self.alias}'] Connected.")
        print(f"  - Resource Manager: {self.rm}")
        print(f"  - Interface: {self.interface_type}")
        print(f"  - Address: {self.address}:{self.port}")
        if self.extra_params:
            print("  - Extra parameters:")
            for k, v in self.extra_params.items():
                print(f"    - {k}: {v}")

    def disconnect(self):
        print(f"[DummyInstrument '{self.alias}'] Disconnected.")

    def write(self, cmd: str):
        print(f"[DummyInstrument '{self.alias}'] Write: {cmd}")

    def read(self) -> str:
        print(f"[DummyInstrument '{self.alias}'] Read requested.")
        return "Dummy Response"

    def query(self, cmd: str) -> str:
        print(f"[DummyInstrument '{self.alias}'] Query: {cmd}")
        return f"Dummy Response to '{cmd}'"
