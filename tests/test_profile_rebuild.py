"""ProfileRegistry.rebuild_main_ui_from_library — 라이브러리 변경을 프로파일에 반영.

VISA 라이브러리에서 명령을 고치면 이미 구성해 둔 측정 항목도 따라 갱신돼야 한다.
그런데 **사용자가 채운 값(fill_params)과 safety 설정, 체크 상태는 지켜야 한다.**
이 균형이 깨지면 라이브러리를 저장하는 순간 측정 구성이 조용히 망가진다.

라이브러리를 저장할 때만 불린다. 기존 항목을 기준으로 돌면서 라이브러리에서
찾은 것만 갱신하고, 못 찾은 항목도 그대로 남긴다 — 여기서 지워버리면 사용자가
공들여 구성한 측정 항목이 사라진다.
"""
import tempfile
import unittest
from pathlib import Path

from pythonization.config.models import (
    FullProfile,
    InstantiatedMeasurement,
    InstantiatedSecondSweepChannel,
    InstantiatedSweepValue,
    InstantiatedWriteCmd,
    InstrumentCmdLibrary,
    MainUIProfile,
    MeasurementParamDef,
    ParameterManagerProfile,
    SweepValueDef,
    WriteCmdDef,
)
from pythonization.instruments.command_library import VisaLibraryRegistry
from pythonization.profiles.registry import ProfileRegistry


class RebuildTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.reg = ProfileRegistry(settings_dir=self.tmp)
        self.lib = VisaLibraryRegistry(path=self.tmp / "visa_libraries.yaml")

    def set_library(self, **kw):
        self.lib.set_library("dev", InstrumentCmdLibrary(**kw))

    def set_profile(self, main_ui: MainUIProfile,
                    selection: ParameterManagerProfile = None):
        profile = FullProfile(
            main_ui=main_ui,
            parameter_manager=selection or ParameterManagerProfile(),
        )
        self.reg.save_active_profile(profile)

    def rebuild(self) -> MainUIProfile:
        return self.reg.rebuild_main_ui_from_library(self.lib)


class TestPreserveUserSettings(RebuildTestCase):
    """라이브러리 필드만 갱신하고 사용자 설정은 보존한다."""

    def test_measurement_picks_up_new_library_fields(self):
        self.set_library(measurements=[MeasurementParamDef(
            description="R", cmd_query="MEAS:RES?", figure_axis="R_new", unit="ohm")])
        self.set_profile(MainUIProfile(measurements=[InstantiatedMeasurement(
            alias="dev", description="R", resolved_cmd="OLD?",
            figure_axis="R_old", unit="", checked=False)]))

        result = self.rebuild()
        meas = result.measurements[0]
        self.assertEqual("MEAS:RES?", meas.resolved_cmd)
        self.assertEqual("R_new", meas.figure_axis)
        self.assertEqual("ohm", meas.unit)
        self.assertFalse(meas.checked, "체크 상태는 사용자 설정이라 보존해야 한다")

    def test_fill_params_are_reapplied_to_new_command(self):
        self.set_library(measurements=[MeasurementParamDef(
            description="R", cmd_query="FETCh:SENSe{m}:NEW?")])
        self.set_profile(MainUIProfile(measurements=[InstantiatedMeasurement(
            alias="dev", description="R", resolved_cmd="FETCh:SENSe2:OLD?",
            fill_params={"m": "2"})]))

        result = self.rebuild()
        self.assertEqual("FETCh:SENSe2:NEW?", result.measurements[0].resolved_cmd,
                         "사용자가 넣은 m=2 가 새 명령에도 적용돼야 한다")

    def test_unfillable_placeholder_keeps_raw_template(self):
        # 라이브러리에 새 placeholder 가 생겼는데 사용자 값이 없는 경우 —
        # 예외로 죽지 않고 템플릿 그대로 둔다
        self.set_library(measurements=[MeasurementParamDef(
            description="R", cmd_query="MEAS:{ch}:{unknown}?")])
        self.set_profile(MainUIProfile(measurements=[InstantiatedMeasurement(
            alias="dev", description="R", resolved_cmd="x", fill_params={"ch": "1"})]))

        result = self.rebuild()
        self.assertEqual("MEAS:{ch}:{unknown}?", result.measurements[0].resolved_cmd)

    def test_missing_library_entry_is_kept_untouched(self):
        """라이브러리에서 사라진 항목도 절대 지우지 않는다."""
        self.set_library(measurements=[])
        self.set_profile(MainUIProfile(measurements=[InstantiatedMeasurement(
            alias="dev", description="사라진 항목", resolved_cmd="KEEP?",
            figure_axis="keep", unit="V")]))

        result = self.rebuild()
        self.assertEqual(1, len(result.measurements))
        self.assertEqual("KEEP?", result.measurements[0].resolved_cmd)

    def test_sweep_value_single_placeholder_becomes_v(self):
        self.set_library(sweep_values=[SweepValueDef(
            description="V", cmd_set="smua.source.levelv = {volt}",
            paired_read_cmd="print(smua.measure.v())", unit="V")])
        self.set_profile(MainUIProfile(sweep_values=[InstantiatedSweepValue(
            alias="dev", description="V", cmd_set="old = {v}",
            paired_read_cmd="old_read", safety_steps=5, safety_interval_ms=10.0)]))

        sv = self.rebuild().sweep_values[0]
        self.assertEqual("smua.source.levelv = {v}", sv.cmd_set,
                         "sweep 대상 placeholder 는 {v} 로 정규화된다")
        self.assertEqual("print(smua.measure.v())", sv.paired_read_cmd)
        self.assertEqual(5, sv.safety_steps, "safety 는 사용자 설정이라 보존")
        self.assertAlmostEqual(10.0, sv.safety_interval_ms)

    def test_sweep_value_multi_placeholder_keeps_existing_cmd(self):
        # placeholder 가 둘 이상이면 어느 것이 sweep 축인지 알 수 없어 기존 것을 유지
        self.set_library(sweep_values=[SweepValueDef(
            description="V", cmd_set="SET {a} {b}")])
        self.set_profile(MainUIProfile(sweep_values=[InstantiatedSweepValue(
            alias="dev", description="V", cmd_set="KEEP {v}", paired_read_cmd="")]))

        self.assertEqual("KEEP {v}", self.rebuild().sweep_values[0].cmd_set)

    def test_write_cmd_without_placeholder_takes_library_command(self):
        self.set_library(write_cmds=[WriteCmdDef(
            description="ON", cmd_set="OUTP ON", unit="")])
        self.set_profile(MainUIProfile(write_cmds=[InstantiatedWriteCmd(
            alias="dev", description="ON", cmd_set="OLD")]))

        self.assertEqual("OUTP ON", self.rebuild().write_cmds[0].cmd_set)

    def test_write_cmd_with_v_placeholder_is_normalized(self):
        self.set_library(write_cmds=[WriteCmdDef(
            description="SET", cmd_set="LEVEL {lvl}")])
        self.set_profile(MainUIProfile(write_cmds=[InstantiatedWriteCmd(
            alias="dev", description="SET", cmd_set="OLD {v}")]))

        self.assertEqual("LEVEL {v}",
                         self.rebuild().write_cmds[0].cmd_set)

    def test_second_channel_sweep_value_preserves_advance_settings(self):
        self.set_library(sweep_values=[SweepValueDef(
            description="B", cmd_set="FIELD {t}", paired_read_cmd="READ:FLD", unit="T")])
        self.set_profile(MainUIProfile(second_sweep_channels=[
            InstantiatedSecondSweepChannel(
                alias="dev", description="B", source_type="sweep_value",
                cmd_set="OLD {v}", feedback_poll_interval=3.0,
                feedback_tolerance_pct=98.0)]))

        sc = self.rebuild().second_sweep_channels[0]
        self.assertEqual("FIELD {v}", sc.cmd_set)
        self.assertEqual("READ:FLD", sc.paired_read_cmd)
        self.assertAlmostEqual(3.0, sc.feedback_poll_interval, msg="advance 설정 보존")
        self.assertAlmostEqual(98.0, sc.feedback_tolerance_pct)

    def test_alarm_and_metadata_lists_survive(self):
        alarm = InstantiatedMeasurement(alias="dev", description="A", resolved_cmd="A?")
        meta = InstantiatedMeasurement(alias="dev", description="B", resolved_cmd="B?")
        self.set_library(measurements=[])
        self.set_profile(MainUIProfile(
            alarm_measurements=[alarm], meta_data_measurements=[meta]))
        result = self.rebuild()
        self.assertEqual(["A"], [m.description for m in result.alarm_measurements])
        self.assertEqual(["B"], [m.description for m in result.meta_data_measurements])

    def test_result_is_persisted(self):
        self.set_library(measurements=[MeasurementParamDef(
            description="R", cmd_query="NEW?")])
        self.set_profile(MainUIProfile(measurements=[InstantiatedMeasurement(
            alias="dev", description="R", resolved_cmd="OLD?")]))
        self.rebuild()

        fresh = ProfileRegistry(settings_dir=self.tmp)
        self.assertEqual("NEW?",
                         fresh.get_active_profile().main_ui.measurements[0].resolved_cmd)


if __name__ == "__main__":
    unittest.main()
