"""주소 해석 — MAC 이 IP 보다 우선한다는 규칙과 그 위험.

`resolve_address` 는 DHCP 로 IP 가 바뀌어도 장비를 따라가려고 MAC 을 먼저 본다.
그런데 **MAC 을 잘못 적으면 조용히 다른 기기에 연결된다.** 명령은 정상 응답하고
값도 그럴듯하게 나오므로 엉뚱한 값을 한참 기록한 뒤에야 드러난다.

실제로 ITC 와 IPS 에 같은 MAC 이 적혀 있어서, ITC 자리에서 iPS 온도를 읽고 있었다.
그래서 중복 MAC 을 설정만 보고 잡아내는 검사를 둔다.
"""
import tempfile
import unittest
from pathlib import Path

import yaml

from pythonization.instruments.registry import InstrumentRegistry
from pythonization.util.network import resolve_address

DRIVER = "pythonization.instruments.drivers.generic_scpi.GenericSCPIInstrument"


def entry(alias, address, mac=""):
    return {"alias": alias, "class_name": DRIVER, "interface_type": "LAN",
            "address": address, "mac_address": mac, "port": 7020,
            "extra_params": {}}


class TestResolveAddress(unittest.TestCase):
    def test_plain_ip_passes_through(self):
        self.assertEqual("192.168.0.7",
                         resolve_address("LAN", "192.168.0.7", ""))

    def test_non_lan_is_untouched(self):
        self.assertEqual("GPIB::9", resolve_address("GPIB", "GPIB::9", "00-11-22-33-44-55"))

    def test_unknown_mac_falls_back_to_configured_ip(self):
        # ARP 에 없는 MAC — 설정된 IP 를 그대로 써야 한다
        self.assertEqual("192.168.0.7",
                         resolve_address("LAN", "192.168.0.7", "FF-FF-FF-FF-FF-FE"))


class TestDuplicateMacDetection(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "instruments.yaml"
        self.addCleanup(self._tmp.cleanup)

    def registry(self, entries) -> InstrumentRegistry:
        self.path.write_text(yaml.dump(entries, allow_unicode=True), encoding="utf-8")
        return InstrumentRegistry(settings_file=self.path)

    def test_same_mac_different_address_is_reported(self):
        """이번 사고의 재발 방지 — ITC 와 IPS 에 같은 MAC, 주소는 다름."""
        reg = self.registry([
            entry("ITC", "192.168.0.7", "00-10-A5-20-30-4A"),
            entry("IPS", "192.168.0.5", "00-10-A5-20-30-4A"),
        ])
        warnings = reg.config_warnings()
        self.assertEqual(1, len(warnings), warnings)
        self.assertIn("ITC", warnings[0])
        self.assertIn("IPS", warnings[0])
        self.assertIn("한 기기로 연결", warnings[0])

    def test_same_mac_same_address_is_not_reported(self):
        """같은 기기에 alias 를 여러 개 붙이는 건 정상 (예: MFLI / Zurich)."""
        reg = self.registry([
            entry("MFLI", "192.168.0.13", "80-2F-DE-00-58-B0"),
            entry("Zurich", "192.168.0.13", "80-2F-DE-00-58-B0"),
        ])
        self.assertEqual([], reg.config_warnings())

    def test_distinct_macs_are_clean(self):
        reg = self.registry([
            entry("ITC", "192.168.0.7", "00-10-A5-20-30-4A"),
            entry("IPS", "192.168.0.5", "00-10-A5-20-31-91"),
        ])
        self.assertEqual([], reg.config_warnings())

    def test_empty_macs_do_not_count_as_duplicates(self):
        # MAC 을 안 적는 건 정상 — 이때는 설정된 IP 를 그대로 쓴다
        reg = self.registry([
            entry("A", "192.168.0.7", ""),
            entry("B", "192.168.0.5", ""),
        ])
        self.assertEqual([], reg.config_warnings())

    def test_mac_format_differences_still_match(self):
        # 콜론/하이픈, 대소문자가 달라도 같은 MAC 이다
        reg = self.registry([
            entry("A", "192.168.0.7", "00:10:a5:20:30:4a"),
            entry("B", "192.168.0.5", "00-10-A5-20-30-4A"),
        ])
        self.assertEqual(1, len(reg.config_warnings()))

    def test_warning_is_logged_once_per_config(self):
        """설정이 그대로면 reload 를 반복해도 경고를 다시 찍지 않는다."""
        reg = self.registry([
            entry("A", "192.168.0.7", "00-10-A5-20-30-4A"),
            entry("B", "192.168.0.5", "00-10-A5-20-30-4A"),
        ])
        before = set(reg._logged_warnings)
        reg.reload()
        reg.reload()
        self.assertEqual(before, reg._logged_warnings)
        self.assertEqual(1, len(reg.config_warnings()), "경고 자체는 계속 조회 가능")


if __name__ == "__main__":
    unittest.main()
