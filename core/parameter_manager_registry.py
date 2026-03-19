"""
ParameterManagerRegistry: Parameter Manager 선택 상태 및 Main UI 프로파일 로드/저장.
"""
import re as _re
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import yaml

from config.config_models import (
    ParameterManagerProfile, MainUIProfile,
    InstantiatedMeasurement, InstantiatedSweepValue, InstantiatedWriteCmd,
    InstantiatedSecondSweepChannel, DoubleSweepConfig,
)

if TYPE_CHECKING:
    from core.visa_library_registry import VisaLibraryRegistry

_SETTINGS_DIR = Path(__file__).parent.parent / "settings"


class ParameterManagerRegistry:

    def __init__(self, settings_dir: Optional[Path] = None):
        base = settings_dir or _SETTINGS_DIR
        self._sel_path = base / "parameter_manager.yaml"
        self._mui_path = base / "main_ui_profile.yaml"
        self._ds_path  = base / "double_sweep.yaml"
        self._selection = ParameterManagerProfile()
        self._main_ui   = MainUIProfile()
        self._double_sweep = DoubleSweepConfig()
        self._load()

    def _load(self):
        if self._sel_path.exists():
            with open(self._sel_path, "r", encoding="utf-8") as f:
                try:
                    self._selection = ParameterManagerProfile.model_validate(
                        yaml.safe_load(f) or {}
                    )
                except Exception:
                    self._selection = ParameterManagerProfile()

        if self._mui_path.exists():
            with open(self._mui_path, "r", encoding="utf-8") as f:
                try:
                    self._main_ui = MainUIProfile.model_validate(yaml.safe_load(f) or {})
                except Exception:
                    self._main_ui = MainUIProfile()

        if self._ds_path.exists():
            with open(self._ds_path, "r", encoding="utf-8") as f:
                try:
                    self._double_sweep = DoubleSweepConfig.model_validate(yaml.safe_load(f) or {})
                except Exception:
                    self._double_sweep = DoubleSweepConfig()

    # ------------------------------------------------------------------
    # Selection (Parameter Manager window state)
    # ------------------------------------------------------------------

    @property
    def selection(self) -> ParameterManagerProfile:
        return self._selection

    def save_selection(self, sel: ParameterManagerProfile):
        self._selection = sel
        with open(self._sel_path, "w", encoding="utf-8") as f:
            yaml.dump(sel.model_dump(), f, allow_unicode=True, sort_keys=False)

    # ------------------------------------------------------------------
    # Main UI Profile (instantiated entries with filled params)
    # ------------------------------------------------------------------

    @property
    def main_ui_profile(self) -> MainUIProfile:
        return self._main_ui

    def save_main_ui(self, profile: MainUIProfile):
        self._main_ui = profile
        with open(self._mui_path, "w", encoding="utf-8") as f:
            yaml.dump(profile.model_dump(), f, allow_unicode=True, sort_keys=False)

    # ------------------------------------------------------------------
    # Double Sweep Config
    # ------------------------------------------------------------------

    @property
    def double_sweep_config(self) -> DoubleSweepConfig:
        return self._double_sweep

    def save_double_sweep_config(self, cfg: DoubleSweepConfig):
        self._double_sweep = cfg
        with open(self._ds_path, "w", encoding="utf-8") as f:
            yaml.dump(cfg.model_dump(), f, allow_unicode=True, sort_keys=False)

    # ------------------------------------------------------------------
    # Auto re-instantiation from library
    # ------------------------------------------------------------------

    def rebuild_main_ui_from_library(self, lib_registry: "VisaLibraryRegistry") -> MainUIProfile:
        """
        selection의 (alias, description) 포인터로 라이브러리를 참조해
        MainUIProfile을 재인스턴스화합니다.
        description이 라이브러리에 존재하면 최신 명령어로 갱신하고,
        없으면 기존 main_ui_profile 항목을 그대로 사용합니다.
        """
        old_meas  = {(m.alias, m.description): m for m in self._main_ui.measurements}
        old_sweep = {(s.alias, s.description): s for s in self._main_ui.sweep_values}
        old_write = {(w.alias, w.description): w for w in self._main_ui.write_cmds}

        # ── Measurements ──────────────────────────────────────────────
        new_measurements: list[InstantiatedMeasurement] = []
        for sel in self._selection.measurements:
            lib = lib_registry.get_library(sel.alias)
            entry = next((e for e in lib.measurements if e.description == sel.description), None)
            if entry:
                new_measurements.append(InstantiatedMeasurement(
                    alias=sel.alias,
                    description=entry.description,
                    resolved_cmd=entry.resolved_cmd(),
                    figure_axis=entry.figure_axis,
                    unit=entry.unit,
                    fill_params=old_meas[(sel.alias, sel.description)].fill_params
                    if (sel.alias, sel.description) in old_meas else {},
                ))
            elif (sel.alias, sel.description) in old_meas:
                new_measurements.append(old_meas[(sel.alias, sel.description)])

        # ── Sweep Values ───────────────────────────────────────────────
        new_sweep_values: list[InstantiatedSweepValue] = []
        for sel in self._selection.sweep_values:
            lib = lib_registry.get_library(sel.alias)
            entry = next((e for e in lib.sweep_values if e.description == sel.description), None)
            old = old_sweep.get((sel.alias, sel.description))
            if entry:
                paired_cmd = entry.paired_read.resolved_cmd()
                lib_phs = _re.findall(r"\{(\w+)\}", entry.cmd_set)
                if len(lib_phs) == 1:
                    # 단일 플레이스홀더 → sweep 파라미터로 간주, {v}로 치환
                    new_cmd_set = entry.cmd_set.replace(f"{{{lib_phs[0]}}}", "{v}")
                elif old:
                    # 복수 플레이스홀더 → 기존 cmd_set 유지 (고정값 파라미터 복원 불가)
                    new_cmd_set = old.cmd_set
                else:
                    new_cmd_set = entry.cmd_set
                new_sweep_values.append(InstantiatedSweepValue(
                    alias=sel.alias,
                    description=entry.description,
                    cmd_set=new_cmd_set,
                    paired_read_cmd=paired_cmd,
                    figure_axis=entry.figure_axis,
                    unit=entry.unit,
                    safety_steps=old.safety_steps if old else 0,
                    safety_interval_ms=old.safety_interval_ms if old else 0.0,
                    fill_params=old.fill_params if old else {},
                ))
            elif old:
                new_sweep_values.append(old)

        # ── Write Commands ─────────────────────────────────────────────
        new_write_cmds: list[InstantiatedWriteCmd] = []
        for sel in self._selection.write_cmds:
            lib = lib_registry.get_library(sel.alias)
            entry = next((e for e in lib.write_cmds if e.description == sel.description), None)
            old = old_write.get((sel.alias, sel.description))
            if entry:
                lib_phs = _re.findall(r"\{(\w+)\}", entry.cmd_set)
                if not lib_phs:
                    new_cmd_set = entry.cmd_set
                elif len(lib_phs) == 1 and old and "{v}" in old.cmd_set:
                    new_cmd_set = entry.cmd_set.replace(f"{{{lib_phs[0]}}}", "{v}")
                elif old:
                    new_cmd_set = old.cmd_set
                else:
                    new_cmd_set = entry.cmd_set
                new_write_cmds.append(InstantiatedWriteCmd(
                    alias=sel.alias,
                    description=entry.description,
                    cmd_set=new_cmd_set,
                    figure_axis=entry.figure_axis,
                    unit=entry.unit,
                    fill_params=old.fill_params if old else {},
                ))
            elif old:
                new_write_cmds.append(old)

        # ── Second Sweep Channels ──────────────────────────────────────
        old_second = {(s.alias, s.description): s for s in self._main_ui.second_sweep_channels}
        new_second_channels: list[InstantiatedSecondSweepChannel] = []
        for sel in self._selection.second_sweep_channels:
            lib = lib_registry.get_library(sel.alias)
            old = old_second.get((sel.alias, sel.description))
            # Try sweep_values first, then write_cmds
            sv_entry = next((e for e in lib.sweep_values if e.description == sel.description), None)
            wc_entry = next((e for e in lib.write_cmds if e.description == sel.description), None)
            if sv_entry:
                source_type = "sweep_value"
                lib_phs = _re.findall(r"\{(\w+)\}", sv_entry.cmd_set)
                if len(lib_phs) == 1:
                    new_cmd_set = sv_entry.cmd_set.replace(f"{{{lib_phs[0]}}}", "{v}")
                elif old:
                    new_cmd_set = old.cmd_set
                else:
                    new_cmd_set = sv_entry.cmd_set
                paired_cmd = sv_entry.paired_read.resolved_cmd()
                if old and old.source_type == "sweep_value":
                    new_second_channels.append(InstantiatedSecondSweepChannel(
                        alias=sel.alias,
                        description=sv_entry.description,
                        source_type=source_type,
                        advance_type=old.advance_type,
                        cmd_set=new_cmd_set,
                        paired_read_cmd=paired_cmd,
                        sweep_rate=old.sweep_rate,
                        safety_steps=old.safety_steps,
                        safety_interval_ms=old.safety_interval_ms,
                        feedback_read_cmd=old.feedback_read_cmd,
                        feedback_poll_interval=old.feedback_poll_interval,
                        feedback_tolerance_pct=old.feedback_tolerance_pct,
                        wait_time=old.wait_time,
                        figure_axis=sv_entry.figure_axis,
                        unit=sv_entry.unit,
                    ))
                else:
                    new_second_channels.append(InstantiatedSecondSweepChannel(
                        alias=sel.alias,
                        description=sv_entry.description,
                        source_type=source_type,
                        cmd_set=new_cmd_set,
                        paired_read_cmd=paired_cmd,
                        figure_axis=sv_entry.figure_axis,
                        unit=sv_entry.unit,
                    ))
            elif wc_entry:
                source_type = "write_cmd"
                lib_phs = _re.findall(r"\{(\w+)\}", wc_entry.cmd_set)
                if not lib_phs:
                    new_cmd_set = wc_entry.cmd_set
                elif len(lib_phs) == 1 and old and "{v}" in old.cmd_set:
                    new_cmd_set = wc_entry.cmd_set.replace(f"{{{lib_phs[0]}}}", "{v}")
                elif old:
                    new_cmd_set = old.cmd_set
                else:
                    new_cmd_set = wc_entry.cmd_set
                if old and old.source_type == "write_cmd":
                    new_second_channels.append(InstantiatedSecondSweepChannel(
                        alias=sel.alias,
                        description=wc_entry.description,
                        source_type=source_type,
                        advance_type=old.advance_type,
                        cmd_set=new_cmd_set,
                        wait_time=old.wait_time,
                        figure_axis=wc_entry.figure_axis,
                        unit=wc_entry.unit,
                    ))
                else:
                    new_second_channels.append(InstantiatedSecondSweepChannel(
                        alias=sel.alias,
                        description=wc_entry.description,
                        source_type=source_type,
                        cmd_set=new_cmd_set,
                        figure_axis=wc_entry.figure_axis,
                        unit=wc_entry.unit,
                    ))
            elif old:
                new_second_channels.append(old)

        return MainUIProfile(
            measurements=new_measurements,
            sweep_values=new_sweep_values,
            write_cmds=new_write_cmds,
            second_sweep_channels=new_second_channels,
        )
