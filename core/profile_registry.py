"""
ProfileRegistry: 사용자 프로파일 관리.
profiles/ 디렉토리에 이름별 YAML 파일로 저장.
ParameterManagerRegistry의 drop-in 대체품.
"""
import re as _re
from pathlib import Path
from typing import Dict, List, Optional, TYPE_CHECKING

import yaml

from config.config_models import (
    ParameterManagerProfile, MainUIProfile, FullProfile, DoubleSweepConfig,
    InstantiatedMeasurement, InstantiatedSweepValue, InstantiatedWriteCmd,
    InstantiatedSecondSweepChannel,
)

if TYPE_CHECKING:
    from core.visa_library_registry import VisaLibraryRegistry

_SETTINGS_DIR = Path(__file__).parent.parent / "settings"


class ProfileRegistry:
    """
    Named profiles management.

    profiles/ ─ 각 프로파일을 별도 YAML 파일로 저장.
    active_profile.txt ─ 현재 활성 프로파일 이름 저장.

    ParameterManagerRegistry와 동일한 API를 제공하므로 drop-in 대체 가능.
    """

    def __init__(self, settings_dir: Optional[Path] = None):
        self._base = settings_dir or _SETTINGS_DIR
        self._profiles_dir = self._base / "profiles"
        self._profiles_dir.mkdir(parents=True, exist_ok=True)
        self._active_path = self._base / "active_profile.txt"
        self._active_name: str = "default"
        self._cache: Dict[str, FullProfile] = {}

        self._load_active_name()
        self._migrate_if_needed()

        # Ensure active profile file exists
        if not self._profile_path(self._active_name).exists():
            self.save_profile(self._active_name, FullProfile())

    # ------------------------------------------------------------------
    # File paths
    # ------------------------------------------------------------------

    @staticmethod
    def _sanitize(name: str) -> str:
        """파일명으로 사용 가능하도록 정리."""
        safe = _re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip().strip('.')
        return safe or "profile"

    def _profile_path(self, name: str) -> Path:
        return self._profiles_dir / f"{self._sanitize(name)}.yaml"

    # ------------------------------------------------------------------
    # Active profile name
    # ------------------------------------------------------------------

    def _load_active_name(self):
        if self._active_path.exists():
            try:
                self._active_name = self._active_path.read_text(encoding="utf-8").strip() or "default"
            except Exception:
                self._active_name = "default"

    def _save_active_name(self):
        self._active_path.write_text(self._active_name, encoding="utf-8")

    # ------------------------------------------------------------------
    # Migration from old settings files
    # ------------------------------------------------------------------

    def _migrate_if_needed(self):
        """기존 YAML 설정 파일을 profiles/default.yaml로 마이그레이션."""
        if any(self._profiles_dir.glob("*.yaml")):
            return  # 이미 프로파일 있음

        profile = FullProfile()
        for fname, field in [
            ("parameter_manager.yaml", "parameter_manager"),
            ("main_ui_profile.yaml", "main_ui"),
            ("double_sweep.yaml", "double_sweep"),
        ]:
            path = self._base / fname
            if path.exists():
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        data = yaml.safe_load(f) or {}
                    if field == "parameter_manager":
                        profile.parameter_manager = ParameterManagerProfile.model_validate(data)
                    elif field == "main_ui":
                        profile.main_ui = MainUIProfile.model_validate(data)
                    elif field == "double_sweep":
                        profile.double_sweep = DoubleSweepConfig.model_validate(data)
                except Exception:
                    pass

        self.save_profile("default", profile)

    # ------------------------------------------------------------------
    # Profile CRUD
    # ------------------------------------------------------------------

    def list_profiles(self) -> List[str]:
        """프로파일 이름 목록 반환 (default 항상 첫 번째)."""
        names = [p.stem for p in sorted(self._profiles_dir.glob("*.yaml"))]
        if "default" in names:
            names.remove("default")
            names.insert(0, "default")
        return names

    def get_profile(self, name: str) -> FullProfile:
        if name in self._cache:
            return self._cache[name]
        path = self._profile_path(name)
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    p = FullProfile.model_validate(yaml.safe_load(f) or {})
                    self._cache[name] = p
                    return p
            except Exception:
                pass
        return FullProfile()

    def save_profile(self, name: str, profile: FullProfile):
        self._cache[name] = profile
        path = self._profile_path(name)
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(profile.model_dump(mode='json'), f, allow_unicode=True, sort_keys=False)

    @property
    def active_name(self) -> str:
        return self._active_name

    def get_active_profile(self) -> FullProfile:
        return self.get_profile(self._active_name)

    def set_active(self, name: str):
        self._active_name = name
        self._save_active_name()

    def save_active_profile(self, profile: FullProfile):
        self.save_profile(self._active_name, profile)

    def add_profile(self, name: str) -> str:
        """새 빈 프로파일 추가. 이름 충돌 시 _N 붙임."""
        base = self._sanitize(name)
        actual = base
        counter = 2
        while self._profile_path(actual).exists():
            actual = f"{base}_{counter}"
            counter += 1
        self.save_profile(actual, FullProfile())
        return actual

    def delete_profile(self, name: str):
        """프로파일 삭제. 마지막 프로파일은 삭제 불가."""
        if len(self.list_profiles()) <= 1:
            raise ValueError("마지막 프로파일은 삭제할 수 없습니다.")
        path = self._profile_path(name)
        if path.exists():
            path.unlink()
        self._cache.pop(name, None)
        if self._active_name == name:
            remaining = self.list_profiles()
            if remaining:
                self.set_active(remaining[0])

    def rename_profile(self, old_name: str, new_name: str) -> str:
        """프로파일 이름 변경. 실제 파일명 변경 후 캐시 갱신.

        충돌 시 _2, _3, ... 를 붙여 고유 이름을 반환합니다.
        """
        base = self._sanitize(new_name)
        actual = base
        counter = 2
        while self._profile_path(actual).exists() and actual != old_name:
            actual = f"{base}_{counter}"
            counter += 1

        profile = self.get_profile(old_name)
        self.save_profile(actual, profile)

        # 기존 파일 삭제 (이름이 달라진 경우)
        if actual != old_name:
            old_path = self._profile_path(old_name)
            if old_path.exists():
                old_path.unlink()
            self._cache.pop(old_name, None)

        if self._active_name == old_name:
            self.set_active(actual)

        return actual

    def duplicate_profile(self, name: str) -> str:
        """프로파일 복제. 새 이름은 name_2, name_3, ... 순."""
        source = self.get_profile(name)
        base = self._sanitize(name)
        counter = 2
        new_name = f"{base}_{counter}"
        while self._profile_path(new_name).exists():
            counter += 1
            new_name = f"{base}_{counter}"
        self.save_profile(new_name, source.model_copy(deep=True))
        return new_name

    # ------------------------------------------------------------------
    # ParameterManagerRegistry compatibility API
    # ------------------------------------------------------------------

    @property
    def selection(self) -> ParameterManagerProfile:
        return self.get_active_profile().parameter_manager

    def save_selection(self, sel: ParameterManagerProfile):
        profile = self.get_active_profile()
        profile.parameter_manager = sel
        self.save_active_profile(profile)

    @property
    def main_ui_profile(self) -> MainUIProfile:
        return self.get_active_profile().main_ui

    def save_main_ui(self, mui: MainUIProfile):
        profile = self.get_active_profile()
        profile.main_ui = mui
        self.save_active_profile(profile)

    @property
    def double_sweep_config(self) -> DoubleSweepConfig:
        return self.get_active_profile().double_sweep

    def save_double_sweep_config(self, cfg: DoubleSweepConfig):
        profile = self.get_active_profile()
        profile.double_sweep = cfg
        self.save_active_profile(profile)

    # ------------------------------------------------------------------
    # Library rebuild (ParameterManagerRegistry compatibility)
    # ------------------------------------------------------------------

    def rebuild_main_ui_from_library(self, lib_registry: "VisaLibraryRegistry") -> MainUIProfile:
        """
        selection의 (alias, description) 포인터로 라이브러리를 참조해
        MainUIProfile을 재인스턴스화합니다.
        description이 라이브러리에 존재하면 최신 명령어로 갱신하고,
        없으면 기존 항목을 그대로 사용합니다.
        fill_params는 항상 보존됩니다.
        """
        active = self.get_active_profile()
        sel = active.parameter_manager
        old_mui = active.main_ui

        old_meas  = {(m.alias, m.description): m for m in old_mui.measurements}
        old_sweep = {(s.alias, s.description): s for s in old_mui.sweep_values}
        old_write = {(w.alias, w.description): w for w in old_mui.write_cmds}
        old_second = {(s.alias, s.description): s for s in old_mui.second_sweep_channels}

        # ── Measurements ──────────────────────────────────────────────
        new_measurements: list[InstantiatedMeasurement] = []
        for s in sel.measurements:
            lib = lib_registry.get_library(s.alias)
            entry = next((e for e in lib.measurements if e.description == s.description), None)
            old = old_meas.get((s.alias, s.description))
            if entry:
                new_measurements.append(InstantiatedMeasurement(
                    alias=s.alias,
                    description=entry.description,
                    resolved_cmd=entry.cmd_query,
                    figure_axis=entry.figure_axis,
                    unit=entry.unit,
                    fill_params=old.fill_params if old else {},
                ))
            elif old:
                new_measurements.append(old)

        # ── Sweep Values ───────────────────────────────────────────────
        new_sweep_values: list[InstantiatedSweepValue] = []
        for s in sel.sweep_values:
            lib = lib_registry.get_library(s.alias)
            entry = next((e for e in lib.sweep_values if e.description == s.description), None)
            old = old_sweep.get((s.alias, s.description))
            if entry:
                paired_cmd = entry.paired_read_cmd
                lib_phs = _re.findall(r"\{(\w+)\}", entry.cmd_set)
                if len(lib_phs) == 1:
                    new_cmd_set = entry.cmd_set.replace(f"{{{lib_phs[0]}}}", "{v}")
                elif old:
                    new_cmd_set = old.cmd_set
                else:
                    new_cmd_set = entry.cmd_set
                new_sweep_values.append(InstantiatedSweepValue(
                    alias=s.alias,
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
        for s in sel.write_cmds:
            lib = lib_registry.get_library(s.alias)
            entry = next((e for e in lib.write_cmds if e.description == s.description), None)
            old = old_write.get((s.alias, s.description))
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
                    alias=s.alias,
                    description=entry.description,
                    cmd_set=new_cmd_set,
                    figure_axis=entry.figure_axis,
                    unit=entry.unit,
                    fill_params=old.fill_params if old else {},
                ))
            elif old:
                new_write_cmds.append(old)

        # ── Second Sweep Channels ──────────────────────────────────────
        new_second: list[InstantiatedSecondSweepChannel] = []
        for s in sel.second_sweep_channels:
            lib = lib_registry.get_library(s.alias)
            old = old_second.get((s.alias, s.description))
            sv_entry = next((e for e in lib.sweep_values if e.description == s.description), None)
            wc_entry = next((e for e in lib.write_cmds  if e.description == s.description), None)
            if sv_entry:
                lib_phs = _re.findall(r"\{(\w+)\}", sv_entry.cmd_set)
                new_cmd_set = (
                    sv_entry.cmd_set.replace(f"{{{lib_phs[0]}}}", "{v}") if len(lib_phs) == 1
                    else (old.cmd_set if old else sv_entry.cmd_set)
                )
                paired_cmd = sv_entry.paired_read_cmd
                if old and old.source_type == "sweep_value":
                    new_second.append(old.model_copy(update={
                        "cmd_set": new_cmd_set,
                        "paired_read_cmd": paired_cmd,
                        "figure_axis": sv_entry.figure_axis,
                        "unit": sv_entry.unit,
                    }))
                else:
                    new_second.append(InstantiatedSecondSweepChannel(
                        alias=s.alias, description=sv_entry.description,
                        source_type="sweep_value", cmd_set=new_cmd_set,
                        paired_read_cmd=paired_cmd,
                        figure_axis=sv_entry.figure_axis, unit=sv_entry.unit,
                    ))
            elif wc_entry:
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
                    new_second.append(old.model_copy(update={
                        "cmd_set": new_cmd_set,
                        "figure_axis": wc_entry.figure_axis,
                        "unit": wc_entry.unit,
                    }))
                else:
                    new_second.append(InstantiatedSecondSweepChannel(
                        alias=s.alias, description=wc_entry.description,
                        source_type="write_cmd", cmd_set=new_cmd_set,
                        figure_axis=wc_entry.figure_axis, unit=wc_entry.unit,
                    ))
            elif old:
                new_second.append(old)

        return MainUIProfile(
            measurements=new_measurements,
            sweep_values=new_sweep_values,
            write_cmds=new_write_cmds,
            second_sweep_channels=new_second,
        )
