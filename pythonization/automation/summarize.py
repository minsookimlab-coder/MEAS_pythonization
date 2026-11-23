"""측정 결과를 LLM 이 읽을 크기로 줄인다.

수천 점을 그대로 보내면 대화가 금방 커지고 비용이 늘어난다. 대신 채널마다
최소·최대·평균·처음·끝값, 극값의 위치, 가장 가파른 변화 위치를 주고, 곡선 모양은
균등 간격으로 뽑은 수십 점으로 보여 준다. LLM 은 이것으로 '전이가 어디쯤 있다',
'값이 포화됐다', '노이즈가 크다' 같은 판단을 하고 다음 측정을 정한다.

numpy 없이 순수 파이썬으로 쓴다 (점 수가 수만 개 수준이라 충분히 빠르다).
"""
import math
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


def _sig(v: Optional[float], digits: int = 6):
    """유효숫자 digits 자리로 반올림. nan/None 은 None (JSON null)."""
    if v is None or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    if v == 0:
        return 0.0
    return float(f"{v:.{digits}g}")


def _stats(x: Sequence[float], y: Sequence[Optional[float]]) -> dict:
    pairs = [(xi, yi) for xi, yi in zip(x, y)
             if yi is not None and math.isfinite(yi) and math.isfinite(xi)]
    n_bad = len(y) - len(pairs)
    if not pairs:
        return {"n": 0, "n_invalid": n_bad}
    ys = [p[1] for p in pairs]
    i_min = min(range(len(ys)), key=ys.__getitem__)
    i_max = max(range(len(ys)), key=ys.__getitem__)
    mean = sum(ys) / len(ys)
    std = math.sqrt(sum((v - mean) ** 2 for v in ys) / len(ys))
    out = {
        "n": len(pairs),
        "n_invalid": n_bad,
        "first": _sig(ys[0]), "last": _sig(ys[-1]),
        "min": _sig(ys[i_min]), "x_at_min": _sig(pairs[i_min][0]),
        "max": _sig(ys[i_max]), "x_at_max": _sig(pairs[i_max][0]),
        "mean": _sig(mean), "std": _sig(std),
    }
    # 가장 가파른 변화 — 전이·급변 위치 후보
    best = None
    for (x0, y0), (x1, y1) in zip(pairs, pairs[1:]):
        dx = x1 - x0
        if abs(dx) < 1e-300:
            continue
        slope = (y1 - y0) / dx
        if best is None or abs(slope) > abs(best[0]):
            best = (slope, 0.5 * (x0 + x1))
    if best is not None:
        out["max_abs_slope"] = _sig(best[0])
        out["x_at_max_abs_slope"] = _sig(best[1])
    return out


def _downsample_indices(n: int, k: int) -> List[int]:
    if n <= k:
        return list(range(n))
    return sorted({round(i * (n - 1) / (k - 1)) for i in range(k)})


def summarize_series(x: Sequence[float], columns: Dict[str, Sequence[Optional[float]]],
                     x_name: str = "x", max_curve_points: int = 40) -> dict:
    """x 와 여러 y 열을 요약한다. columns: {열 이름: 값 목록 (x 와 같은 길이)}."""
    out = {
        "points": len(x),
        x_name: {"first": _sig(x[0]) if x else None, "last": _sig(x[-1]) if x else None},
        "columns": {name: _stats(x, ys) for name, ys in columns.items()},
    }
    if x:
        idx = _downsample_indices(len(x), max_curve_points)
        curve = {x_name: [_sig(x[i], 5) for i in idx]}
        for name, ys in columns.items():
            curve[name] = [_sig(ys[i], 5) if i < len(ys) else None for i in idx]
        out["curve"] = curve
    return out


def _cell(text: str) -> Optional[float]:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None     # 'ERR', '—', 빈칸


def read_dat(path) -> Tuple[List[str], List[str], List[List[Optional[float]]]]:
    """DataSaver 가 쓴 .dat 를 읽는다. 반환: (컬럼 이름, 단위, 행 목록)."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    if len(lines) < 2:
        return [], [], []
    names = lines[0].split("\t")
    units = lines[1].split("\t")
    rows = []
    for line in lines[2:]:
        if not line.strip():
            continue
        rows.append([_cell(c) for c in line.split("\t")])
    return names, units, rows


def summarize_dat(path, max_curve_points: int = 40) -> dict:
    """저장된 .dat 파일 전체를 요약 — LLM 이 실제로 저장된 데이터를 보고 판단하게."""
    names, units, rows = read_dat(path)
    if not names:
        return {"points": 0}
    x = [r[0] if r and r[0] is not None else float("nan") for r in rows]
    cols = {}
    for j, name in enumerate(names[1:], start=1):
        label = f"{name} ({units[j]})" if j < len(units) and units[j] else name
        cols[label] = [r[j] if j < len(r) else None for r in rows]
    x_label = f"{names[0]} ({units[0]})" if units and units[0] else names[0]
    return summarize_series(x, cols, x_name=x_label, max_curve_points=max_curve_points)
