"""AI 자동 측정 — 코드가 강제하는 안전 검사와 하네스(채널 목록) 검증.

LLM 이 무엇을 요청하든 허용 안 된 채널·범위 밖 값·속도 초과·경로 탈출은 여기서
막혀야 한다. 이 테스트가 깨지면 AI 가 장비를 위험하게 움직일 수 있다.
"""
import tempfile
import unittest
from pathlib import Path

from pythonization.automation.lab_context import build_lab_context, measurement_column_label
from pythonization.automation.safety import (
    SafetyPolicy,
    SafetyViolation,
    sanitize_file_prefix,
    sanitize_sub_folder,
)
from pythonization.automation.settings import (
    AutomationSettings,
    load_automation_settings,
    save_automation_settings,
)
from pythonization.automation.summarize import summarize_dat, summarize_series
from pythonization.config.models import MeasType
from tests.automation_fakes import make_profile, make_settings


class TestSweepLimits(unittest.TestCase):
    def setUp(self):
        self.policy = SafetyPolicy(make_settings(), make_profile())

    def test_allowed_value_passes(self):
        self.assertEqual(-20.0, self.policy.check_sweep_value(0, -20, "start"))

    def test_value_outside_range_rejected(self):
        with self.assertRaises(SafetyViolation) as ctx:
            self.policy.check_sweep_value(0, 25, "stop")
        self.assertIn("허용 범위", str(ctx.exception))

    def test_channel_not_allowed_rejected(self):
        with self.assertRaises(SafetyViolation) as ctx:
            self.policy.check_sweep_value(1, 0.5)
        self.assertIn("허용되지 않았", str(ctx.exception))

    def test_allowed_without_limits_rejected(self):
        cfg = make_settings()
        cfg.sweep_channels[0].max_value = None
        with self.assertRaises(SafetyViolation):
            SafetyPolicy(cfg, make_profile()).check_sweep_value(0, 1)

    def test_unknown_index_rejected(self):
        for bad in (5, -1, "0", None):
            with self.subTest(idx=bad), self.assertRaises(SafetyViolation):
                self.policy.check_sweep_value(bad, 0)

    def test_nan_and_inf_rejected(self):
        for bad in (float("nan"), float("inf"), "abc"):
            with self.subTest(v=bad), self.assertRaises(SafetyViolation):
                self.policy.check_sweep_value(0, bad)

    def test_rate_above_max_rejected(self):
        with self.assertRaises(SafetyViolation):
            self.policy.check_rate(0, 121, 1.0)
        self.assertEqual((120.0, 1.0), self.policy.check_rate(0, 120, 1.0))

    def test_rate_must_be_positive_and_tpp_above_minimum(self):
        with self.assertRaises(SafetyViolation):
            self.policy.check_rate(0, 0, 1.0)
        with self.assertRaises(SafetyViolation):
            self.policy.check_rate(0, 1, 0.01)

    def test_current_outside_range_blocks_start(self):
        with self.assertRaises(SafetyViolation) as ctx:
            self.policy.check_current_in_range(0, 30.0)
        self.assertIn("건드리지 않았", str(ctx.exception))

    def test_point_budget(self):
        cfg = make_settings(max_points_per_sweep=100)
        policy = SafetyPolicy(cfg, make_profile())
        self.assertEqual(41, policy.check_points(-20, 20, 60, 1.0))   # 1 V/점
        with self.assertRaises(SafetyViolation):
            policy.check_points(-20, 20, 6, 1.0)                       # 0.1 V/점 → 401


class TestOtherLimits(unittest.TestCase):
    def setUp(self):
        self.policy = SafetyPolicy(make_settings(), make_profile())

    def test_second_axis_range(self):
        self.assertEqual(10.0, self.policy.check_second_value(0, 10))
        with self.assertRaises(SafetyViolation):
            self.policy.check_second_value(0, 0.5)

    def test_write_command_without_value(self):
        wc, cmd = self.policy.write_command(0, None)
        self.assertEqual("OUT 1", cmd)
        with self.assertRaises(SafetyViolation):
            self.policy.write_command(0, 3)

    def test_write_command_value_range(self):
        self.assertEqual("RANGE 5.0", self.policy.write_command(1, 5)[1])
        with self.assertRaises(SafetyViolation):
            self.policy.write_command(1, 11)

    def test_write_command_not_allowed(self):
        cfg = make_settings()
        cfg.write_cmds[0].allowed = False
        with self.assertRaises(SafetyViolation):
            SafetyPolicy(cfg, make_profile()).write_command(0, None)

    def test_measurements_required_and_validated(self):
        self.assertEqual([0, 1], self.policy.measurement_indices([0, 1, 0]))
        with self.assertRaises(SafetyViolation):
            self.policy.measurement_indices([])
        with self.assertRaises(SafetyViolation):
            self.policy.measurement_indices([7])

    def test_abort_limits_llm_can_only_tighten(self):
        limits = self.policy.abort_limits([0, 1], [{"measurement": 1, "limit": 5e-9}])
        self.assertEqual({1: 1e-9}, limits, "사용자 한계보다 느슨한 값은 무시")
        limits = self.policy.abort_limits([0, 1], [{"measurement": 1, "limit": 1e-10},
                                                   {"measurement": 0, "limit": 0.5}])
        self.assertEqual({1: 1e-10, 0: 0.5}, limits)


class TestNames(unittest.TestCase):
    def test_sub_folder_kept_relative(self):
        self.assertEqual("sampleA/gate_sweep/T_2K", sanitize_sub_folder("sampleA\\gate sweep/T 2K"))
        self.assertEqual("", sanitize_sub_folder("  "))

    def test_sub_folder_escape_rejected(self):
        for bad in ("../x", "a/../../b", "C:/Windows", "/abs", "\\\\server\\share",
                    "a/b/c/d/e"):
            with self.subTest(bad=bad), self.assertRaises(SafetyViolation):
                sanitize_sub_folder(bad)

    def test_sub_folder_invalid_chars_and_reserved(self):
        self.assertEqual("a_b_/_CON", sanitize_sub_folder('a<b>/CON'))

    def test_file_prefix(self):
        self.assertEqual("Vg_m20_p20_2K_", sanitize_file_prefix("Vg_m20_p20_2K_"))
        self.assertEqual("a_b_c", sanitize_file_prefix("a/b\\c"))
        self.assertEqual("x_y", sanitize_file_prefix('x:y.'))


class TestLabContext(unittest.TestCase):
    def test_roles_limits_and_indices_visible(self):
        ctx = build_lab_context(make_profile(), make_settings(),
                                {"SRC": {"driver": "Keithley2636A", "interface": "LAN"}},
                                {"rate_per_min": 1.0})
        gate = ctx["sweep_channels"][0]
        self.assertEqual((0, "gate", True), (gate["index"], gate["role"], gate["ai_allowed"]))
        self.assertEqual(-20, gate["limits"]["min"])
        self.assertFalse(ctx["sweep_channels"][1]["ai_allowed"])
        self.assertIsNone(ctx["sweep_channels"][1]["limits"], "허용 안 된 채널은 범위 비공개")
        self.assertEqual(1e-9, ctx["measurements"][1]["abort_if_abs_above"])
        self.assertTrue(ctx["write_commands"][1]["takes_value"])
        self.assertEqual("Keithley2636A", ctx["instruments"][0]["driver"])

    def test_column_label_matches_main_window_rule(self):
        m = make_profile().measurements[0]
        self.assertEqual("X", measurement_column_label(m))
        m.axis_suffix = "s1"
        self.assertEqual("X_s1", measurement_column_label(m))
        m.meas_type = MeasType.CONTACT
        self.assertEqual("s1", measurement_column_label(m))


class TestSettingsIO(unittest.TestCase):
    def test_round_trip_and_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_settings(lab_notes="시료 A")
            save_automation_settings(cfg, "my:profile", Path(tmp))
            back = load_automation_settings("my:profile", Path(tmp))
            self.assertEqual(cfg.model_dump(), back.model_dump())
            self.assertTrue((Path(tmp) / "profiles" / "automation" / "my_profile.yaml").exists())

    def test_sync_adds_new_entries_as_not_allowed(self):
        cfg = AutomationSettings()
        cfg.sync_with_profile(make_profile())
        self.assertEqual(2, len(cfg.sweep_channels))
        self.assertFalse(any(p.allowed for p in cfg.sweep_channels + cfg.second_channels
                             + cfg.write_cmds))


class TestSummaries(unittest.TestCase):
    def test_series_stats_and_steepest_point(self):
        x = [float(i) for i in range(11)]
        y = [0, 0, 0, 0, 0, 5, 10, 10, 10, 10, 10]
        s = summarize_series(x, {"R": y}, x_name="Vg", max_curve_points=5)
        col = s["columns"]["R"]
        self.assertEqual((0.0, 10.0, 11), (col["min"], col["max"], col["n"]))
        self.assertEqual(4.5, col["x_at_max_abs_slope"])
        self.assertEqual(5, len(s["curve"]["Vg"]))

    def test_invalid_values_counted(self):
        s = summarize_series([0.0, 1.0, 2.0], {"R": [1.0, None, float("nan")]})
        self.assertEqual((1, 2), (s["columns"]["R"]["n"], s["columns"]["R"]["n_invalid"]))

    def test_reads_datasaver_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a.dat"
            p.write_text("Vg\tX\nV\tV\n0\t1\n1\tERR\n2\tnan\n3\t4\n", encoding="utf-8")
            s = summarize_dat(p)
            self.assertEqual(4, s["points"])
            self.assertEqual(2, s["columns"]["X (V)"]["n"])
            self.assertIn("Vg (V)", s["curve"])


if __name__ == "__main__":
    unittest.main()
