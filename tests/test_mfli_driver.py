"""ZurichMFLI 드라이버 — 연결 판정과 실패 진단.

MFLI 는 장비 자체가 Data Server 를 돌린다. 그래서 `server_host` 를 로컬(127.0.0.1)로
두면 장비가 `/zi/devices/visible` 에는 보이지만 `/zi/devices/connected` 에는 없고,
connectDevice 는 'in use' 로 거부된다. 메시지만 보면 장비가 고장난 것처럼 읽혀서
실제로 시간을 버렸다 — 그래서 진단 문구를 테스트로 고정한다.

zhinst 없이 돌도록 daq 객체를 가짜로 끼운다.
"""
import unittest

from pythonization.instruments.drivers.zurich_mfli import ZurichMFLI


class FakeDaq:
    """필요한 노드만 흉내 내는 가짜 Data Server 클라이언트."""

    def __init__(self, visible="", connected="", nodes=None, connect_error=None):
        self._lists = {"/zi/devices/visible": visible,
                       "/zi/devices/connected": connected}
        self._nodes = nodes or {}
        self._connect_error = connect_error
        self.connect_calls = []
        self.disconnect_calls = []

    def getString(self, path):
        if path in self._lists:
            return self._lists[path]
        if path in self._nodes:
            return str(self._nodes[path])
        raise RuntimeError(f"DeviceNotFoundError: Device not found ({path})")

    def getDouble(self, path):
        if path in self._nodes:
            return float(self._nodes[path])
        raise RuntimeError(f"DeviceNotFoundError: Device not found ({path})")

    def connectDevice(self, dev, iface):
        self.connect_calls.append((dev, iface))
        if self._connect_error:
            raise self._connect_error

    def disconnectDevice(self, dev):
        self.disconnect_calls.append(dev)


def make_driver(daq: FakeDaq, dev="dev32704", host="127.0.0.1") -> ZurichMFLI:
    """connect() 의 zhinst 초기화를 건너뛰고 판정 부분만 검사할 수 있게 만든다."""
    driver = ZurichMFLI(
        resource_manager=None, alias="MFLI", interface_type="LAN",
        address=host, port=8004, device_id=dev, server_host=host,
        server_port=8004, api_level=6, iface="PCIe", settle_s=0.0)
    driver.dev_id = dev
    driver.server_host = host
    driver.server_port = 8004
    driver.api_level = 6
    driver.iface = "PCIe"
    driver._settle_s = 0.0
    driver.daq = daq
    driver.inst = daq
    driver._we_connected = False
    return driver


class TestDeviceAccessible(unittest.TestCase):
    def test_readable_node_means_accessible(self):
        daq = FakeDaq(nodes={"/dev32704/features/serial": "32704"})
        self.assertTrue(make_driver(daq)._device_accessible())

    def test_missing_node_means_not_accessible(self):
        self.assertFalse(make_driver(FakeDaq())._device_accessible())


class TestConnectFailureHint(unittest.TestCase):
    """실패 원인별로 다음에 무엇을 할지 알려 줘야 한다."""

    def hint(self, daq, dev="dev32704"):
        error = RuntimeError("DeviceInUseError: already in use")
        return make_driver(daq, dev=dev)._connect_failure_hint(error)

    def test_visible_but_not_connected_points_at_server_host(self):
        """가장 흔한 원인 — 엉뚱한 Data Server 에 접속."""
        message = self.hint(FakeDaq(visible="DEV32704", connected=""))
        self.assertIn("server_host", message)
        self.assertIn("장비 IP", message)
        self.assertIn("connected=''", message)

    def test_not_visible_points_at_hardware(self):
        message = self.hint(FakeDaq(visible="DEV11111", connected=""))
        self.assertIn("보이지 않습니다", message)
        self.assertIn("device_id", message)
        self.assertNotIn("server_host 를 LabOne", message,
                         "장비가 안 보이는데 server_host 를 고치라고 하면 안 된다")

    def test_connected_but_unreadable_points_at_api_level(self):
        message = self.hint(FakeDaq(visible="DEV32704", connected="DEV32704"))
        self.assertIn("api_level", message)

    def test_case_insensitive_device_matching(self):
        # zhinst 는 목록을 대문자로 준다 (DEV32704) — 소문자 dev_id 와 맞춰야 한다
        message = self.hint(FakeDaq(visible="DEV32704", connected=""))
        self.assertIn("server_host", message)


class TestConnectSkipsWhenAlreadyConnected(unittest.TestCase):
    def test_no_connect_device_call_when_readable(self):
        """이미 연결된 장비에 connectDevice 를 부르면 LabOne 측정을 방해한다."""
        daq = FakeDaq(nodes={"/dev32704/features/serial": "32704"})
        driver = make_driver(daq)
        self.assertTrue(driver._device_accessible())
        self.assertEqual([], daq.connect_calls)

    def test_disconnect_leaves_foreign_device_alone(self):
        """우리가 붙이지 않은 장비는 떼지 않는다 — 떼면 LabOne 이 끊긴다."""
        daq = FakeDaq(nodes={"/dev32704/features/serial": "32704"})
        driver = make_driver(daq)
        driver._we_connected = False
        driver.disconnect()
        self.assertEqual([], daq.disconnect_calls)

    def test_disconnect_releases_device_we_connected(self):
        daq = FakeDaq(nodes={"/dev32704/features/serial": "32704"})
        driver = make_driver(daq)
        driver._we_connected = True
        driver.disconnect()
        self.assertEqual(["dev32704"], daq.disconnect_calls)


class TestNodeCommands(unittest.TestCase):
    def test_dev_placeholder_is_substituted(self):
        daq = FakeDaq(nodes={"/dev32704/oscs/0/freq": 100.0})
        self.assertEqual("100.0", make_driver(daq).query("/{dev}/oscs/0/freq"))

    def test_write_parses_path_and_value(self):
        class Recorder(FakeDaq):
            def __init__(self):
                super().__init__()
                self.written = []

            def setDouble(self, path, value):
                self.written.append((path, value))

            def sync(self):
                pass

        daq = Recorder()
        make_driver(daq).write("/{dev}/oscs/0/freq = 1234.5")
        self.assertEqual([("/dev32704/oscs/0/freq", 1234.5)], daq.written)

    def test_write_without_equals_is_rejected(self):
        with self.assertRaises(ValueError):
            make_driver(FakeDaq()).write("/{dev}/oscs/0/freq")

    def test_query_without_connection_raises(self):
        driver = make_driver(FakeDaq())
        driver.daq = None
        with self.assertRaises(ConnectionError):
            driver.query("/{dev}/oscs/0/freq")


if __name__ == "__main__":
    unittest.main()
