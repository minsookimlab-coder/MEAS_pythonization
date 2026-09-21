"""AddEntryDialog — 라이브러리 항목 + 사용자 입력 → 측정 항목 인스턴스.

여기서 만들어진 cmd_set 이 그대로 계측기로 나간다. placeholder 를 잘못 채우면
엉뚱한 값을 장비에 쓰게 되므로, 치환 규칙과 검증을 고정한다.

핵심 규칙: sweep 축으로 지정한 자리만 `{v}` 로 남기고(측정 중 값이 들어간다)
나머지는 사용자가 넣은 고정값으로 바꾼다.
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QComboBox, QLineEdit   # noqa: E402

from pythonization.config.models import (                          # noqa: E402
    MeasurementParamDef,
    SecondSweepAdvanceType,
    SweepValueDef,
    WriteCmdDef,
)
from pythonization.instruments.command_library import VisaLibraryRegistry  # noqa: E402
from pythonization.ui.dialogs.parameter_manager import (           # noqa: E402
    AddEntryDialog,
    _EntryMeta,
    _fill_command,
    _fill_read_command,
    _SWEEP_MARKER,
    _ValidationError,
)

_app = QApplication.instance() or QApplication([])

META = _EntryMeta(alias="dev", description="설명", figure_axis="axis", unit="V")


class TestFillCommand(unittest.TestCase):
    """순수 치환 규칙 — 다이얼로그 없이 검증 가능."""

    def test_sweep_placeholder_becomes_v(self):
        self.assertEqual(
            "smua.source.levelv = {v}",
            _fill_command("smua.source.levelv = {volt}",
                          {"volt": _SWEEP_MARKER}, "volt"))

    def test_other_placeholders_take_fixed_values(self):
        self.assertEqual(
            "FETCh:SENSe2:LIA:X?",
            _fill_command("FETCh:SENSe{m}:LIA:{ax}?",
                          {"m": "2", "ax": "X"}, None))

    def test_mixed_sweep_and_fixed(self):
        self.assertEqual(
            "SET ch1 = {v}",
            _fill_command("SET ch{n} = {val}",
                          {"n": "1", "val": _SWEEP_MARKER}, "val"))

    def test_template_without_placeholders_is_unchanged(self):
        self.assertEqual("OUTP ON", _fill_command("OUTP ON", {}, None))

    def test_read_command_never_gets_v(self):
        # 읽기 명령에는 sweep 축이 없다 — 전부 고정값
        self.assertEqual(
            "print(smua.measure.v())",
            _fill_read_command("print(smua.measure.{q}())", {"q": "v"}))


class DialogTestCase(unittest.TestCase):
    """섹션별 다이얼로그를 만들고 빌더를 직접 부른다 (경고 창을 띄우지 않는 경로)."""

    def make(self, section: str) -> AddEntryDialog:
        lib = VisaLibraryRegistry()
        dialog = AddEntryDialog(lib, section)
        self.addCleanup(dialog.deleteLater)
        return dialog


class TestBuildMeasurement(DialogTestCase):
    def test_resolves_placeholders(self):
        dialog = self.make("measurement")
        entry = MeasurementParamDef(description="R", cmd_query="FETCh:SENSe{m}:DC?")
        result = dialog._build_measurement(entry, META, {"m": "2"}, None)
        self.assertEqual("FETCh:SENSe2:DC?", result.resolved_cmd)
        self.assertEqual({"m": "2"}, result.fill_params, "재편집용으로 원본 입력 보관")
        self.assertEqual("dev", result.alias)
        self.assertEqual("axis", result.figure_axis)

    def test_no_placeholder(self):
        dialog = self.make("measurement")
        entry = MeasurementParamDef(description="R", cmd_query="MEAS:RES?")
        self.assertEqual("MEAS:RES?",
                         dialog._build_measurement(entry, META, {}, None).resolved_cmd)

    def test_wrong_entry_type_is_rejected(self):
        dialog = self.make("measurement")
        entry = WriteCmdDef(description="ON", cmd_set="OUTP ON")
        with self.assertRaises(_ValidationError):
            dialog._build_measurement(entry, META, {}, None)


class TestBuildSweepValue(DialogTestCase):
    def entry(self):
        return SweepValueDef(description="V", cmd_set="smua.source.levelv = {volt}",
                             paired_read_cmd="print(smua.measure.v())")

    def test_sweep_placeholder_required(self):
        dialog = self.make("sweep")
        with self.assertRaises(_ValidationError) as ctx:
            dialog._build_sweep_value(self.entry(), META, {"volt": "1"}, None)
        self.assertIn("[SWEEP]", str(ctx.exception))

    def test_builds_cmd_set_and_paired_read(self):
        dialog = self.make("sweep")
        result = dialog._build_sweep_value(
            self.entry(), META, {"volt": _SWEEP_MARKER}, "volt")
        self.assertEqual("smua.source.levelv = {v}", result.cmd_set)
        self.assertEqual("print(smua.measure.v())", result.paired_read_cmd)

    def test_safety_off_writes_zeros(self):
        dialog = self.make("sweep")
        dialog._cb_safety.setChecked(False)
        dialog._sb_safety_steps.setValue(7)
        dialog._sb_safety_interval.setValue(25.0)
        result = dialog._build_sweep_value(
            self.entry(), META, {"volt": _SWEEP_MARKER}, "volt")
        self.assertEqual(0, result.safety_steps, "체크 해제면 입력값이 있어도 0")
        self.assertAlmostEqual(0.0, result.safety_interval_ms)

    def test_safety_on_uses_entered_values(self):
        dialog = self.make("sweep")
        dialog._cb_safety.setChecked(True)
        dialog._sb_safety_steps.setValue(7)
        dialog._sb_safety_interval.setValue(25.0)
        result = dialog._build_sweep_value(
            self.entry(), META, {"volt": _SWEEP_MARKER}, "volt")
        self.assertEqual(7, result.safety_steps)
        self.assertAlmostEqual(25.0, result.safety_interval_ms)


class TestBuildWriteCmd(DialogTestCase):
    def test_fixed_command_without_sweep(self):
        dialog = self.make("write")
        entry = WriteCmdDef(description="ON", cmd_set="OUTP ON")
        result = dialog._build_write_cmd(entry, META, {}, None)
        self.assertEqual("OUTP ON", result.cmd_set)

    def test_sweep_placeholder_is_optional_but_honored(self):
        dialog = self.make("write")
        entry = WriteCmdDef(description="SET", cmd_set="LEVEL {lvl}")
        result = dialog._build_write_cmd(
            entry, META, {"lvl": _SWEEP_MARKER}, "lvl")
        self.assertEqual("LEVEL {v}", result.cmd_set)

    def test_fixed_value_is_substituted(self):
        dialog = self.make("write")
        entry = WriteCmdDef(description="SET", cmd_set="LEVEL {lvl}")
        result = dialog._build_write_cmd(entry, META, {"lvl": "3"}, None)
        self.assertEqual("LEVEL 3", result.cmd_set)


class TestBuildSecondChannel(DialogTestCase):
    def sweep_entry(self):
        return SweepValueDef(description="B", cmd_set="FIELD {t}",
                             paired_read_cmd="READ:FLD")

    def _select_advance(self, dialog, advance_type):
        idx = dialog._combo_advance.findData(advance_type)
        self.assertGreaterEqual(idx, 0, f"{advance_type} 선택지가 없다")
        dialog._combo_advance.setCurrentIndex(idx)

    def test_simple_hop_needs_no_extra_settings(self):
        dialog = self.make("second")
        self._select_advance(dialog, SecondSweepAdvanceType.SIMPLE_HOP)
        result = dialog._build_second_channel(
            self.sweep_entry(), META, {"t": _SWEEP_MARKER}, "t")
        self.assertEqual("FIELD {v}", result.cmd_set)
        self.assertEqual("READ:FLD", result.paired_read_cmd)
        self.assertEqual(SecondSweepAdvanceType.SIMPLE_HOP, result.advance_type)

    def test_feedback_reuses_paired_read_for_sweep_value(self):
        dialog = self.make("second")
        self._select_advance(dialog, SecondSweepAdvanceType.FEEDBACK)
        dialog._le_fb_poll.setText("2.5")
        dialog._le_fb_tol.setText("98")
        dialog._le_fb_noisefloor.setText("0.001")
        dialog._le_fb_std_threshold.setText("0.0003")
        result = dialog._build_second_channel(
            self.sweep_entry(), META, {"t": _SWEEP_MARKER}, "t")
        self.assertEqual("READ:FLD", result.feedback_read_cmd,
                         "sweep value 는 짝꿍 read 를 그대로 쓴다")
        self.assertAlmostEqual(2.5, result.feedback_poll_interval)
        self.assertAlmostEqual(98.0, result.feedback_tolerance_pct)
        self.assertAlmostEqual(0.001, result.feedback_noisefloor)

    def test_feedback_rejects_non_numeric(self):
        dialog = self.make("second")
        self._select_advance(dialog, SecondSweepAdvanceType.FEEDBACK)
        dialog._le_fb_poll.setText("빠르게")
        with self.assertRaises(_ValidationError) as ctx:
            dialog._build_second_channel(
                self.sweep_entry(), META, {"t": _SWEEP_MARKER}, "t")
        self.assertIn("numbers", str(ctx.exception))

    def test_wait_for_time(self):
        dialog = self.make("second")
        self._select_advance(dialog, SecondSweepAdvanceType.WAIT_FOR_TIME)
        dialog._le_wait.setText("12.5")
        result = dialog._build_second_channel(
            self.sweep_entry(), META, {"t": _SWEEP_MARKER}, "t")
        self.assertAlmostEqual(12.5, result.wait_time)

    def test_wait_rejects_non_numeric(self):
        dialog = self.make("second")
        self._select_advance(dialog, SecondSweepAdvanceType.WAIT_FOR_TIME)
        dialog._le_wait.setText("잠깐")
        with self.assertRaises(_ValidationError):
            dialog._build_second_channel(
                self.sweep_entry(), META, {"t": _SWEEP_MARKER}, "t")


class TestCollectMeta(DialogTestCase):
    def test_missing_description_is_rejected(self):
        dialog = self.make("measurement")
        dialog._combo_alias.addItem("dev")
        dialog._le_desc.setText("   ")
        with self.assertRaises(_ValidationError) as ctx:
            dialog._collect_meta()
        self.assertIn("Description", str(ctx.exception))

    def test_strips_whitespace(self):
        dialog = self.make("measurement")
        dialog._combo_alias.addItem("dev")
        dialog._combo_alias.setCurrentText("dev")
        dialog._le_desc.setText("  R  ")
        dialog._le_axis.setText("  R_axis ")
        dialog._le_unit.setText(" ohm ")
        meta = dialog._collect_meta()
        self.assertEqual("R", meta.description)
        self.assertEqual("R_axis", meta.figure_axis)
        self.assertEqual("ohm", meta.unit)


class TestCollectPlaceholders(DialogTestCase):
    def build_widgets(self, dialog, spec):
        """spec: {이름: (sweep 가능 여부, 입력값, sweep 선택 여부)}"""
        widgets = {}
        for name, (sweepable, text, is_sweep) in spec.items():
            combo = None
            if sweepable:
                combo = QComboBox()
                combo.addItem("[SWEEP]")
                combo.addItem("fixed")
                combo.setCurrentIndex(0 if is_sweep else 1)
            edit = QLineEdit(text)
            widgets[name] = (combo, edit)
        dialog._ph_widgets = widgets

    def test_single_sweep_placeholder(self):
        dialog = self.make("sweep")
        self.build_widgets(dialog, {
            "volt": (True, "", True),
            "ch": (False, "1", False),
        })
        filled, sweep = dialog._collect_placeholder_values()
        self.assertEqual("volt", sweep)
        self.assertEqual({"volt": _SWEEP_MARKER, "ch": "1"}, filled)

    def test_two_sweep_placeholders_rejected(self):
        dialog = self.make("sweep")
        self.build_widgets(dialog, {
            "a": (True, "", True),
            "b": (True, "", True),
        })
        with self.assertRaises(_ValidationError) as ctx:
            dialog._collect_placeholder_values()
        self.assertIn("Exactly one", str(ctx.exception))

    def test_empty_fixed_value_rejected(self):
        dialog = self.make("measurement")
        self.build_widgets(dialog, {"m": (False, "  ", False)})
        with self.assertRaises(_ValidationError) as ctx:
            dialog._collect_placeholder_values()
        self.assertIn("{m}", str(ctx.exception))

    def test_no_placeholders_is_fine(self):
        dialog = self.make("measurement")
        dialog._ph_widgets = {}
        self.assertEqual(({}, None), dialog._collect_placeholder_values())


if __name__ == "__main__":
    unittest.main()
