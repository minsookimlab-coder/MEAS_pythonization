"""
ProfileRegistry: 사용자 프로파일 관리.
profiles/ 디렉토리에 이름별 YAML 파일로 저장.
ParameterManagerRegistry의 drop-in 대체품.
"""
import re as _re
import shutil
from pathlib import Path
from typing import Dict, List, Optional, TYPE_CHECKING

import yaml

from pythonization.config.models import (
    ParameterManagerProfile, MainUIProfile, FullProfile, DoubleSweepConfig,
    CycleSweepConfig, CycleDoubleSweepConfig,
    InstantiatedMeasurement, InstantiatedSweepValue, InstantiatedWriteCmd,
    InstantiatedSecondSweepChannel, MetaDataConfig,
)

if TYPE_CHECKING:
    from pythonization.instruments.command_library import VisaLibraryRegistry

from pythonization.app.paths import SETTINGS_DIR as _SETTINGS_DIR


_PLACEHOLDER_RE = _re.compile(r"\{(\w+)\}")


def _find_entry(entries, description: str):
    """라이브러리 목록에서 같은 description 을 가진 항목. 없으면 None."""
    return next((e for e in entries if e.description == description), None)


def _resolve_template(template: str, fill_params: dict) -> str:
    """사용자가 채워 둔 값을 새 템플릿에 다시 적용한다.

    라이브러리 쪽에 새 placeholder 가 생겨 채울 수 없으면 예외를 내지 않고
    템플릿 그대로 둔다 — 여기서 죽으면 라이브러리 저장 자체가 실패한다.
    """
    if not fill_params:
        return template
    try:
        return template.format(**fill_params)
    except (KeyError, ValueError):
        return template


def _sweep_cmd_set(entry_cmd: str, current_cmd: str) -> str:
    """sweep 명령의 cmd_set 갱신 — sweep 축 placeholder 를 {v} 로 정규화.

    placeholder 가 정확히 하나면 그것이 sweep 축이다. 둘 이상이면 어느 것이 축인지
    알 수 없으므로 사용자가 Parameter Manager 에서 정한 기존 명령을 유지한다.
    """
    placeholders = _PLACEHOLDER_RE.findall(entry_cmd)
    if len(placeholders) == 1:
        return entry_cmd.replace(f"{{{placeholders[0]}}}", "{v}")
    return current_cmd


def _write_cmd_set(entry_cmd: str, current_cmd: str) -> str:
    """write 명령의 cmd_set 갱신.

    write 는 'OUTP ON' 처럼 placeholder 없는 고정 명령이 많다. 그런 경우 라이브러리
    명령을 그대로 쓰고, 기존 항목이 {v} 를 쓰고 있을 때만 sweep 축으로 보아
    정규화한다.
    """
    placeholders = _PLACEHOLDER_RE.findall(entry_cmd)
    if not placeholders:
        return entry_cmd
    if len(placeholders) == 1 and "{v}" in current_cmd:
        return entry_cmd.replace(f"{{{placeholders[0]}}}", "{v}")
    return current_cmd


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
        self._migrate_vna_configs()

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

    def _vna_config_path(self, name: str) -> Path:
        """VNA 전용 설정 파일 경로 (profiles/vna/{name}.yaml)."""
        return self._profiles_dir / "vna" / f"{self._sanitize(name)}.yaml"

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

    def _migrate_vna_configs(self):
        """profiles/vna_*.yaml → profiles/vna/*.yaml 이동 및 오염 정리.

        이전 버전에서 VNA config가 profiles/ 안에 vna_{name}.yaml로 저장되어
        list_profiles()에 오염됐던 문제를 수정한다.
        """
        vna_dir = self._profiles_dir / "vna"
        vna_dir.mkdir(exist_ok=True)
        for p in list(self._profiles_dir.glob("vna_*.yaml")):
            name = p.stem[4:]  # "vna_SangIl" → "SangIl"
            dest = vna_dir / f"{name}.yaml"
            try:
                if not dest.exists():
                    p.rename(dest)
                else:
                    p.unlink()
            except Exception:
                pass
        # active_name이 vna_* 오염 상태이면 원래 프로파일로 복구
        if self._active_name.startswith("vna_"):
            real_name = self._active_name[4:]
            candidates = [p.stem for p in sorted(self._profiles_dir.glob("*.yaml"))
                          if not p.stem.startswith("vna_")]
            if real_name in candidates:
                self.set_active(real_name)
            elif candidates:
                self.set_active(candidates[0])

    # ------------------------------------------------------------------
    # Profile CRUD
    # ------------------------------------------------------------------

    def list_profiles(self) -> List[str]:
        """프로파일 이름 목록 반환 (default 항상 첫 번째).
        vna_* 파일은 VNA 전용이므로 제외."""
        names = [p.stem for p in sorted(self._profiles_dir.glob("*.yaml"))
                 if not p.stem.startswith("vna_")]
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
        vna_path = self._vna_config_path(name)
        if vna_path.exists():
            vna_path.unlink()
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
            # VNA config 파일도 이름 변경
            old_vna = self._vna_config_path(old_name)
            new_vna = self._vna_config_path(actual)
            if old_vna.exists() and not new_vna.exists():
                old_vna.rename(new_vna)

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
        # VNA config도 복사
        src_vna = self._vna_config_path(name)
        dst_vna = self._vna_config_path(new_name)
        if src_vna.exists() and not dst_vna.exists():
            dst_vna.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_vna, dst_vna)
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

    @property
    def cycle_sweep_config(self) -> CycleSweepConfig:
        return self.get_active_profile().cycle_sweep

    def save_cycle_sweep_config(self, cfg: CycleSweepConfig):
        profile = self.get_active_profile()
        profile.cycle_sweep = cfg
        self.save_active_profile(profile)

    @property
    def cycle_double_sweep_config(self) -> CycleDoubleSweepConfig:
        return self.get_active_profile().cycle_double_sweep

    def save_cycle_double_sweep_config(self, cfg: CycleDoubleSweepConfig):
        profile = self.get_active_profile()
        profile.cycle_double_sweep = cfg
        self.save_active_profile(profile)

    @property
    def meta_data_config(self) -> MetaDataConfig:
        return self.get_active_profile().meta_data

    def save_meta_data_config(self, cfg: MetaDataConfig):
        profile = self.get_active_profile()
        profile.meta_data = cfg
        self.save_active_profile(profile)

    # ------------------------------------------------------------------
    # Library rebuild (ParameterManagerRegistry compatibility)
    # ------------------------------------------------------------------

    def rebuild_main_ui_from_library(
        self,
        lib_registry: "VisaLibraryRegistry",
    ) -> MainUIProfile:
        """라이브러리가 저장될 때 MainUIProfile 을 라이브러리 기준으로 갱신한다.

        갱신하는 것 — 명령 문자열, figure_axis, unit (라이브러리에서 온 필드)
        보존하는 것 — fill_params, safety, checked, advance 설정 (사용자가 정한 값)

        **라이브러리에서 사라진 항목도 지우지 않는다.** 여기서 지우면 명령 하나를
        고치려고 라이브러리를 저장했을 뿐인데 공들여 구성한 측정 항목이 통째로
        날아간다. 정리는 Parameter Manager 에서 사용자가 직접 한다.
        """
        active = self.get_active_profile()
        old = active.main_ui

        new_mui = MainUIProfile(
            measurements=self._refresh_measurements(old.measurements, lib_registry),
            sweep_values=self._refresh_sweep_values(old.sweep_values, lib_registry),
            write_cmds=self._refresh_write_cmds(old.write_cmds, lib_registry),
            second_sweep_channels=self._refresh_second_channels(
                old.second_sweep_channels, lib_registry),
            alarm_measurements=old.alarm_measurements,
            meta_data_measurements=old.meta_data_measurements,
        )
        active.main_ui = new_mui
        self.save_active_profile(active)
        return new_mui

    # ── 항목 종류별 갱신 ─────────────────────────────────────────────────
    # 넷 다 같은 모양이다: 라이브러리에서 같은 description 을 찾아 라이브러리 유래
    # 필드만 덮어쓰고, 못 찾으면 원래 항목을 그대로 통과시킨다.

    def _refresh_measurements(
        self, items, lib_registry: "VisaLibraryRegistry",
    ) -> List[InstantiatedMeasurement]:
        out = []
        for item in items:
            entry = _find_entry(lib_registry.get_library(item.alias).measurements,
                                item.description)
            if entry is None:
                out.append(item)
                continue
            out.append(item.model_copy(update={
                "resolved_cmd": _resolve_template(entry.cmd_query, item.fill_params),
                "figure_axis":  entry.figure_axis,
                "unit":         entry.unit,
            }))
        return out

    def _refresh_sweep_values(
        self, items, lib_registry: "VisaLibraryRegistry",
    ) -> List[InstantiatedSweepValue]:
        out = []
        for item in items:
            entry = _find_entry(lib_registry.get_library(item.alias).sweep_values,
                                item.description)
            if entry is None:
                out.append(item)
                continue
            out.append(item.model_copy(update={
                "cmd_set":         _sweep_cmd_set(entry.cmd_set, item.cmd_set),
                "paired_read_cmd": entry.paired_read_cmd,
                "figure_axis":     entry.figure_axis,
                "unit":            entry.unit,
            }))
        return out

    def _refresh_write_cmds(
        self, items, lib_registry: "VisaLibraryRegistry",
    ) -> List[InstantiatedWriteCmd]:
        out = []
        for item in items:
            entry = _find_entry(lib_registry.get_library(item.alias).write_cmds,
                                item.description)
            if entry is None:
                out.append(item)
                continue
            out.append(item.model_copy(update={
                "cmd_set":     _write_cmd_set(entry.cmd_set, item.cmd_set),
                "figure_axis": entry.figure_axis,
                "unit":        entry.unit,
            }))
        return out

    def _refresh_second_channels(
        self, items, lib_registry: "VisaLibraryRegistry",
    ) -> List[InstantiatedSecondSweepChannel]:
        """second 채널은 sweep value 로도, write command 로도 만들 수 있다.

        source_type 이 어느 쪽인지에 따라 찾아볼 라이브러리 목록과 명령 갱신 규칙이
        달라진다. 알 수 없는 source_type 은 손대지 않는다.
        """
        out = []
        for item in items:
            library = lib_registry.get_library(item.alias)

            if item.source_type == "sweep_value":
                entry = _find_entry(library.sweep_values, item.description)
                if entry is None:
                    out.append(item)
                    continue
                out.append(item.model_copy(update={
                    "cmd_set":         _sweep_cmd_set(entry.cmd_set, item.cmd_set),
                    "paired_read_cmd": entry.paired_read_cmd,
                    "figure_axis":     entry.figure_axis,
                    "unit":            entry.unit,
                }))

            elif item.source_type == "write_cmd":
                entry = _find_entry(library.write_cmds, item.description)
                if entry is None:
                    out.append(item)
                    continue
                out.append(item.model_copy(update={
                    "cmd_set":     _write_cmd_set(entry.cmd_set, item.cmd_set),
                    "figure_axis": entry.figure_axis,
                    "unit":        entry.unit,
                }))

            else:
                out.append(item)
        return out
