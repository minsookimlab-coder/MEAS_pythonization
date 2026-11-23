"""LLM 에게 보여 줄 '실험실 현황' — 하네스의 핵심.

"게이트를 -20 V 에서 20 V 까지 스윕해" 같은 요청을 받았을 때 LLM 이 어느 채널을
움직이고 무엇을 측정할지 고르려면, 채널마다 장비·명령 설명·단위·사용자가 적은 역할
메모·허용 범위가 한눈에 보여야 한다. 그 목록을 여기서 만든다.

인덱스는 메인 창 프로파일의 순서 그대로다. 도구 호출은 이 인덱스로 채널을 가리킨다.
"""
from typing import Dict, Optional

from pythonization.automation.settings import AutomationSettings
from pythonization.config.models import MeasType


def measurement_column_label(m) -> str:
    """메인 창 .dat 의 컬럼 이름과 같은 규칙 (MainWindow._meas_label_for)."""
    base = m.figure_axis or m.description
    suffix = (m.axis_suffix or "").strip()
    if m.meas_type == MeasType.CONTACT:
        return suffix if suffix else base
    return f"{base}_{suffix}" if suffix else base


def _limits(pol) -> Optional[dict]:
    if pol is None:
        return None
    out = {"min": pol.min_value, "max": pol.max_value}
    if hasattr(pol, "max_rate"):
        out["max_rate_per_min"] = pol.max_rate
    return out


def build_lab_context(profile, settings: AutomationSettings,
                      instruments: Optional[Dict[str, dict]] = None,
                      main_defaults: Optional[dict] = None) -> dict:
    """프로파일 + 사용자 정책 → LLM 용 JSON 객체.

    instruments: {alias: {"driver": 클래스명, "interface": "LAN"...}}
    main_defaults: 메인 창의 현재 rate/time_per_point/저장 설정 (사용자가 평소 쓰는 값)
    """
    sweep_channels = []
    for i, sv in enumerate(profile.sweep_values):
        pol = settings.sweep_policy(sv.alias, sv.description)
        allowed = bool(pol and pol.allowed and pol.min_value is not None
                       and pol.max_value is not None)
        item = {
            "index": i,
            "instrument": sv.alias,
            "description": sv.description,
            "unit": sv.unit,
            "axis": sv.figure_axis,
            "role": (pol.role if pol else "") or "",
            "ai_allowed": allowed,
            "limits": _limits(pol) if allowed else None,
        }
        if sv.safety_steps:
            item["safety_ramp"] = {"sub_steps": sv.safety_steps,
                                   "interval_ms": sv.safety_interval_ms}
        if getattr(sv, "needs_fix", ""):
            item["unusable"] = sv.needs_fix
        sweep_channels.append(item)

    measurements = []
    for i, m in enumerate(profile.measurements):
        pol = settings.measurement_policy(m.alias, m.description)
        item = {
            "index": i,
            "instrument": m.alias,
            "description": m.description,
            "unit": m.unit,
            "column": measurement_column_label(m),
            "type": m.meas_type.value if hasattr(m.meas_type, "value") else str(m.meas_type),
            "role": (pol.role if pol else "") or "",
            "checked_in_main_window": bool(m.checked),
        }
        if pol is not None and pol.abort_abs is not None:
            item["abort_if_abs_above"] = pol.abort_abs
        if getattr(m, "needs_fix", ""):
            item["unusable"] = m.needs_fix
        measurements.append(item)

    second_channels = []
    for i, ch in enumerate(profile.second_sweep_channels):
        pol = settings.second_policy(ch.alias, ch.description)
        allowed = bool(pol and pol.allowed and pol.min_value is not None
                       and pol.max_value is not None)
        second_channels.append({
            "index": i,
            "instrument": ch.alias,
            "description": ch.description,
            "unit": ch.unit,
            "advance": ch.advance_type.value if hasattr(ch.advance_type, "value")
            else str(ch.advance_type),
            "has_readback": bool(ch.feedback_read_cmd or ch.paired_read_cmd),
            "role": (pol.role if pol else "") or "",
            "ai_allowed": allowed,
            "limits": _limits(pol) if allowed else None,
        })

    write_commands = []
    for i, wc in enumerate(profile.write_cmds):
        pol = settings.write_policy(wc.alias, wc.description)
        item = {
            "index": i,
            "instrument": wc.alias,
            "description": wc.description,
            "takes_value": "{v}" in wc.cmd_set,
            "unit": wc.unit,
            "role": (pol.role if pol else "") or "",
            "ai_allowed": bool(pol and pol.allowed),
        }
        if pol and pol.allowed and "{v}" in wc.cmd_set:
            item["limits"] = {"min": pol.min_value, "max": pol.max_value}
        write_commands.append(item)

    ctx = {
        "sweep_channels": sweep_channels,
        "measurements": measurements,
        "second_axis_channels": second_channels,
        "write_commands": write_commands,
        "instruments": [
            {"alias": alias, **info} for alias, info in sorted((instruments or {}).items())
        ],
        "budget": {
            "max_run_hours": settings.max_run_hours,
            "max_points_per_sweep": settings.max_points_per_sweep,
            "min_time_per_point_s": settings.min_time_per_point,
        },
        "approval_mode": settings.approval_mode,
    }
    if main_defaults:
        ctx["main_window_defaults"] = main_defaults
    if settings.lab_notes.strip():
        ctx["lab_notes"] = settings.lab_notes.strip()
    return ctx
