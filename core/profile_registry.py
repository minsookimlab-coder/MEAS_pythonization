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

from config.config_models import (
    ParameterManagerProfile, MainUIProfile, FullProfile, DoubleSweepConfig,
    InstantiatedMeasurement, InstantiatedSweepValue, InstantiatedWriteCmd,
    InstantiatedSecondSweepChannel, MetaDataConfig,
)

if TYPE_CHECKING:
    from core.visa_library_registry import VisaLibraryRegistry

from core.app_dirs import SETTINGS_DIR as _SETTINGS_DIR


# ---------------------------------------------------------------------------
# 라이브러리 명령 변경 추적
# ---------------------------------------------------------------------------

def _placeholders(template: str) -> list:
    """템플릿에 남아 있는 {이름} 목록 (중복 제거, 등장 순서 유지)."""
    seen, out = set(), []
    for name in _re.findall(r"\{(\w+)\}", template or ""):
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


#: sweep 축 placeholder 는 fill_params 에 키는 있지만 값이 이 표식이다.
#: Parameter Manager 가 '측정할 때 값이 들어올 자리' 라는 뜻으로 넣는다.
SWEEP_SENTINEL = "[SWEEP]"


def filled_keys(fill_params: dict) -> set:
    """실제 값이 채워진 placeholder 이름만.

    sweep 축은 키가 있어도 '채워진 것' 이 아니다. 이걸 채워진 값으로 세면
    '파라미터가 줄었다' 는 잘못된 판정이 나고, 값 치환 때 명령에
    '[SWEEP]' 이라는 문자열이 그대로 박힌다.
    """
    return {k for k, v in (fill_params or {}).items()
            if str(v).strip() != SWEEP_SENTINEL}


def check_placeholders(description: str, template: str, fill_params: dict,
                       axis_slots: int = 0) -> str:
    """라이브러리 템플릿을 이 항목의 fill_params 로 다시 채울 수 있는지 판정한다.

    axis_slots — 채우지 않고 비워 두는 자리 수.
        측정(query)      0 — 모든 placeholder 가 fill_params 에 있어야 한다
        sweep value      1 — sweep 축 하나가 {v} 로 남는다
        write cmd      0/1 — 기존 명령에 {v} 가 있었는지에 따라

    반환: 문제가 없으면 빈 문자열, 있으면 사용자에게 보일 사유.
    """
    need = _placeholders(template)
    have = filled_keys(fill_params)
    unfilled = [n for n in need if n not in have]
    if len(unfilled) == axis_slots:
        return ""
    lib_txt = ", ".join(f"{{{n}}}" for n in need) or "(없음)"
    have_txt = ", ".join(sorted(have)) or "(없음)"
    if len(unfilled) > axis_slots:
        what = "채워야 할 파라미터가 늘었습니다"
    else:
        what = "파라미터가 줄었습니다"
    return (
        f"VISA Library 의 '{description}' 명령이 바뀌어 이 항목을 그대로 쓸 수 없습니다 "
        f"— {what}.\n"
        f"  라이브러리 명령의 파라미터: {lib_txt}\n"
        f"  이 항목에 저장된 값:        {have_txt}\n\n"
        f"Parameter Manager 에서 이 항목을 지우고 다시 등록하면 해결됩니다."
    )


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
    def meta_data_config(self) -> MetaDataConfig:
        return self.get_active_profile().meta_data

    def save_meta_data_config(self, cfg: MetaDataConfig):
        profile = self.get_active_profile()
        profile.meta_data = cfg
        self.save_active_profile(profile)

    # ------------------------------------------------------------------
    # Library rebuild (ParameterManagerRegistry compatibility)
    # ------------------------------------------------------------------

    def refresh_needs_fix(self, mui: MainUIProfile,
                          lib_registry: "VisaLibraryRegistry") -> None:
        """needs_fix 를 라이브러리 기준으로 다시 계산한다 (명령은 건드리지 않는다).

        needs_fix 는 파생 상태다. 프로파일 파일에 저장돼 있지만 그 사이 라이브러리가
        바뀌었을 수도, 판정 로직이 고쳐졌을 수도 있다. 저장된 값을 그대로 믿으면
        예전 판정이 계속 남아 멀쩡한 항목이 잠긴 채로 보인다. 창을 열 때마다 다시
        계산한다.

        라이브러리에 해당 항목이 아예 없으면(orphan) placeholder 문제가 아니므로
        needs_fix 를 비운다.
        """
        for group, kind, slots in (
            (mui.measurements, "measurements", 0),
            (mui.sweep_values, "sweep_values", 1),
            (mui.write_cmds, "write_cmds", None),
        ):
            for item in group:
                try:
                    lib = lib_registry.get_library(item.alias)
                    entries = getattr(lib, kind, [])
                    entry = next((e for e in entries
                                  if e.description == item.description), None)
                except Exception:
                    entry = None
                if entry is None:
                    item.needs_fix = ""
                    continue
                template = getattr(entry, "cmd_query", None) or entry.cmd_set
                n = slots if slots is not None else (1 if "{v}" in item.cmd_set else 0)
                item.needs_fix = check_placeholders(
                    item.description, template, item.fill_params, n)

    def propagate_library_change(self, lib_registry: "VisaLibraryRegistry") -> dict:
        """라이브러리 변경을 **모든 프로파일**에 다시 적용한다.

        활성 프로파일만 고치면 다른 프로파일은 옛 명령을 든 채 남아, 나중에 그
        프로파일로 전환한 사용자가 조용히 틀린 명령으로 측정하게 된다.

        각 프로파일의 main_ui 를 라이브러리 기준으로 재인스턴스화해 저장하고,
        파라미터가 맞지 않아 쓸 수 없게 된 항목은 needs_fix 에 사유를 남긴다.

        반환: {프로파일 이름: [문제 항목 설명, …]} — 문제가 있는 프로파일만 담는다.
        """
        original = self._active_name
        problems: Dict[str, List[str]] = {}
        try:
            for name in self.list_profiles():
                try:
                    profile = self.get_profile(name)
                except Exception:
                    continue
                self._active_name = name          # rebuild 가 활성 프로파일을 본다
                try:
                    new_mui = self.rebuild_main_ui_from_library(
                        lib_registry, drop_orphans=False)
                except Exception:
                    continue
                broken = [
                    f"{item.alias} / {item.description}"
                    for group in (new_mui.measurements, new_mui.sweep_values,
                                  new_mui.write_cmds)
                    for item in group
                    if getattr(item, "needs_fix", "")
                ]
                if broken:
                    problems[name] = broken
                profile.main_ui = new_mui
                self.save_profile(name, profile)
        finally:
            self._active_name = original
        return problems

    def rebuild_main_ui_from_library(
        self,
        lib_registry: "VisaLibraryRegistry",
        drop_orphans: bool = True,
    ) -> MainUIProfile:
        """
        라이브러리 변경 시 MainUIProfile을 재인스턴스화합니다.

        drop_orphans=False (라이브러리 저장 트리거):
            기존 main_ui 항목을 기준으로 이터레이션합니다.
            라이브러리에서 찾으면 cmd/figure_axis/unit 등 라이브러리 유래 필드만 갱신하고,
            사용자 설정(fill_params, safety, checked 등)은 보존합니다.
            라이브러리에서 찾지 못한 항목도 그대로 유지합니다 (절대 삭제 없음).

        drop_orphans=True (명시적 정리 — 현재 미사용):
            selection 포인터를 기준으로 이터레이션하며 라이브러리에 없는 항목을
            결과와 selection에서 모두 제거합니다.
        """
        active = self.get_active_profile()
        old_mui = active.main_ui

        # ── drop_orphans=False: 기존 main_ui 기준, 라이브러리 필드만 갱신 ──────
        if not drop_orphans:
            # Measurements
            new_measurements: list[InstantiatedMeasurement] = []
            for m in old_mui.measurements:
                lib = lib_registry.get_library(m.alias)
                entry = next((e for e in lib.measurements if e.description == m.description), None)
                if entry:
                    nf = check_placeholders(m.description, entry.cmd_query, m.fill_params, 0)
                    if nf:
                        # 파라미터가 안 맞으면 옛 명령을 그대로 두고 표시만 남긴다.
                        # 반쯤 치환된 템플릿({ph} 가 남은 문자열)을 장비로 보내면 안 된다.
                        new_measurements.append(m.model_copy(update={"needs_fix": nf}))
                    else:
                        resolved = (entry.cmd_query.format(**m.fill_params)
                                    if m.fill_params else entry.cmd_query)
                        new_measurements.append(m.model_copy(update={
                            "resolved_cmd": resolved,
                            "figure_axis":  entry.figure_axis,
                            "unit":         entry.unit,
                            "needs_fix":    "",
                        }))
                else:
                    new_measurements.append(m)

            # Sweep Values
            new_sweep_values: list[InstantiatedSweepValue] = []
            for sv in old_mui.sweep_values:
                lib = lib_registry.get_library(sv.alias)
                entry = next((e for e in lib.sweep_values if e.description == sv.description), None)
                if entry:
                    nf = check_placeholders(sv.description, entry.cmd_set, sv.fill_params, 1)
                    if nf:
                        new_sweep_values.append(sv.model_copy(update={"needs_fix": nf}))
                    else:
                        # 채우지 않고 남는 자리 하나 = sweep 축 → {v} 로 정규화
                        done = filled_keys(sv.fill_params)
                        axis = next(n for n in _placeholders(entry.cmd_set)
                                    if n not in done)
                        new_cmd_set = entry.cmd_set.replace(f"{{{axis}}}", "{v}")
                        for k in done:
                            new_cmd_set = new_cmd_set.replace(
                                f"{{{k}}}", str(sv.fill_params[k]))
                        new_sweep_values.append(sv.model_copy(update={
                            "cmd_set":         new_cmd_set,
                            "paired_read_cmd": entry.paired_read_cmd,
                            "figure_axis":     entry.figure_axis,
                            "unit":            entry.unit,
                            "needs_fix":       "",
                        }))
                else:
                    new_sweep_values.append(sv)

            # Write Commands
            new_write_cmds: list[InstantiatedWriteCmd] = []
            for wc in old_mui.write_cmds:
                lib = lib_registry.get_library(wc.alias)
                entry = next((e for e in lib.write_cmds if e.description == wc.description), None)
                if entry:
                    slots = 1 if "{v}" in wc.cmd_set else 0
                    nf = check_placeholders(wc.description, entry.cmd_set, wc.fill_params, slots)
                    if nf:
                        new_write_cmds.append(wc.model_copy(update={"needs_fix": nf}))
                    else:
                        new_cmd_set = entry.cmd_set
                        done = filled_keys(wc.fill_params)
                        if slots:
                            axis = next(n for n in _placeholders(entry.cmd_set)
                                        if n not in done)
                            new_cmd_set = new_cmd_set.replace(f"{{{axis}}}", "{v}")
                        for k in done:
                            new_cmd_set = new_cmd_set.replace(
                                f"{{{k}}}", str(wc.fill_params[k]))
                        new_write_cmds.append(wc.model_copy(update={
                            "cmd_set":     new_cmd_set,
                            "figure_axis": entry.figure_axis,
                            "unit":        entry.unit,
                            "needs_fix":   "",
                        }))
                else:
                    new_write_cmds.append(wc)

            # Second Sweep Channels
            new_second: list[InstantiatedSecondSweepChannel] = []
            for sc in old_mui.second_sweep_channels:
                lib = lib_registry.get_library(sc.alias)
                if sc.source_type == "sweep_value":
                    entry = next((e for e in lib.sweep_values if e.description == sc.description), None)
                    if entry:
                        lib_phs = _re.findall(r"\{(\w+)\}", entry.cmd_set)
                        new_cmd_set = (
                            entry.cmd_set.replace(f"{{{lib_phs[0]}}}", "{v}") if len(lib_phs) == 1
                            else sc.cmd_set
                        )
                        new_second.append(sc.model_copy(update={
                            "cmd_set":         new_cmd_set,
                            "paired_read_cmd": entry.paired_read_cmd,
                            "figure_axis":     entry.figure_axis,
                            "unit":            entry.unit,
                        }))
                    else:
                        new_second.append(sc)
                elif sc.source_type == "write_cmd":
                    entry = next((e for e in lib.write_cmds if e.description == sc.description), None)
                    if entry:
                        lib_phs = _re.findall(r"\{(\w+)\}", entry.cmd_set)
                        new_cmd_set = (
                            entry.cmd_set.replace(f"{{{lib_phs[0]}}}", "{v}")
                            if lib_phs and "{v}" in sc.cmd_set
                            else (entry.cmd_set if not lib_phs else sc.cmd_set)
                        )
                        new_second.append(sc.model_copy(update={
                            "cmd_set":     new_cmd_set,
                            "figure_axis": entry.figure_axis,
                            "unit":        entry.unit,
                        }))
                    else:
                        new_second.append(sc)
                else:
                    new_second.append(sc)

            new_mui = MainUIProfile(
                measurements=new_measurements,
                sweep_values=new_sweep_values,
                write_cmds=new_write_cmds,
                second_sweep_channels=new_second,
                alarm_measurements=old_mui.alarm_measurements,
                meta_data_measurements=old_mui.meta_data_measurements,
            )
            active.main_ui = new_mui
            self.save_active_profile(active)
            return new_mui

        # ── drop_orphans=True: selection 기준, 유령 항목 정리 ─────────────────
        sel = active.parameter_manager

        old_meas   = {(m.alias, m.description): m for m in old_mui.measurements}
        old_sweep  = {(s.alias, s.description): s for s in old_mui.sweep_values}
        old_write  = {(w.alias, w.description): w for w in old_mui.write_cmds}
        old_second = {(s.alias, s.description): s for s in old_mui.second_sweep_channels}

        found_meas:   set = set()
        found_sweep:  set = set()
        found_write:  set = set()
        found_second: set = set()

        # Measurements
        new_measurements = []
        for s in sel.measurements:
            lib = lib_registry.get_library(s.alias)
            entry = next((e for e in lib.measurements if e.description == s.description), None)
            old = old_meas.get((s.alias, s.description))
            if entry:
                found_meas.add((s.alias, s.description))
                fill = old.fill_params if old else {}
                try:
                    resolved = entry.cmd_query.format(**fill) if fill else entry.cmd_query
                except (KeyError, ValueError):
                    resolved = entry.cmd_query
                new_measurements.append(InstantiatedMeasurement(
                    alias=s.alias,
                    description=entry.description,
                    resolved_cmd=resolved,
                    figure_axis=entry.figure_axis,
                    unit=entry.unit,
                    fill_params=fill,
                ))

        # Sweep Values
        new_sweep_values = []
        for s in sel.sweep_values:
            lib = lib_registry.get_library(s.alias)
            entry = next((e for e in lib.sweep_values if e.description == s.description), None)
            old = old_sweep.get((s.alias, s.description))
            if entry:
                found_sweep.add((s.alias, s.description))
                lib_phs = _re.findall(r"\{(\w+)\}", entry.cmd_set)
                new_cmd_set = (
                    entry.cmd_set.replace(f"{{{lib_phs[0]}}}", "{v}") if len(lib_phs) == 1
                    else (old.cmd_set if old else entry.cmd_set)
                )
                new_sweep_values.append(InstantiatedSweepValue(
                    alias=s.alias,
                    description=entry.description,
                    cmd_set=new_cmd_set,
                    paired_read_cmd=entry.paired_read_cmd,
                    figure_axis=entry.figure_axis,
                    unit=entry.unit,
                    safety_steps=old.safety_steps if old else 0,
                    safety_interval_ms=old.safety_interval_ms if old else 0.0,
                    fill_params=old.fill_params if old else {},
                ))

        # Write Commands
        new_write_cmds = []
        for s in sel.write_cmds:
            lib = lib_registry.get_library(s.alias)
            entry = next((e for e in lib.write_cmds if e.description == s.description), None)
            old = old_write.get((s.alias, s.description))
            if entry:
                found_write.add((s.alias, s.description))
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

        # Second Sweep Channels
        new_second = []
        for s in sel.second_sweep_channels:
            lib = lib_registry.get_library(s.alias)
            old = old_second.get((s.alias, s.description))
            sv_entry = next((e for e in lib.sweep_values if e.description == s.description), None)
            wc_entry = next((e for e in lib.write_cmds  if e.description == s.description), None)
            if sv_entry:
                found_second.add((s.alias, s.description))
                lib_phs = _re.findall(r"\{(\w+)\}", sv_entry.cmd_set)
                new_cmd_set = (
                    sv_entry.cmd_set.replace(f"{{{lib_phs[0]}}}", "{v}") if len(lib_phs) == 1
                    else (old.cmd_set if old else sv_entry.cmd_set)
                )
                if old and old.source_type == "sweep_value":
                    new_second.append(old.model_copy(update={
                        "cmd_set": new_cmd_set, "paired_read_cmd": sv_entry.paired_read_cmd,
                        "figure_axis": sv_entry.figure_axis, "unit": sv_entry.unit,
                    }))
                else:
                    new_second.append(InstantiatedSecondSweepChannel(
                        alias=s.alias, description=sv_entry.description,
                        source_type="sweep_value", cmd_set=new_cmd_set,
                        paired_read_cmd=sv_entry.paired_read_cmd,
                        figure_axis=sv_entry.figure_axis, unit=sv_entry.unit,
                    ))
            elif wc_entry:
                found_second.add((s.alias, s.description))
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
                        "figure_axis": wc_entry.figure_axis, "unit": wc_entry.unit,
                    }))
                else:
                    new_second.append(InstantiatedSecondSweepChannel(
                        alias=s.alias, description=wc_entry.description,
                        source_type="write_cmd", cmd_set=new_cmd_set,
                        figure_axis=wc_entry.figure_axis, unit=wc_entry.unit,
                    ))

        new_mui = MainUIProfile(
            measurements=new_measurements,
            sweep_values=new_sweep_values,
            write_cmds=new_write_cmds,
            second_sweep_channels=new_second,
            alarm_measurements=old_mui.alarm_measurements,
            meta_data_measurements=old_mui.meta_data_measurements,
        )

        # 유령 selection 포인터 제거
        cleaned_sel = sel.model_copy(update={
            "measurements":          [s for s in sel.measurements
                                      if (s.alias, s.description) in found_meas],
            "sweep_values":          [s for s in sel.sweep_values
                                      if (s.alias, s.description) in found_sweep],
            "write_cmds":            [s for s in sel.write_cmds
                                      if (s.alias, s.description) in found_write],
            "second_sweep_channels": [s for s in sel.second_sweep_channels
                                      if (s.alias, s.description) in found_second],
        })
        active.parameter_manager = cleaned_sel
        active.main_ui = new_mui
        self.save_active_profile(active)
        return new_mui
