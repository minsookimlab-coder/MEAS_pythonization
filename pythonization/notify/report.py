"""측정 리포트 — 휴대폰으로 보낼 '문서' 한 장을 만든다.

담는 것: 마지막으로 저장된 측정 **그래프(PNG)**, 장비가 마지막으로 읽은 값
(온도·자기장 등), **측정 조건**(축·범위·프로파일), 진행 상황.

GUI 와 네트워크에 의존하지 않는다 — 저장된 `.dat` 와 호출자가 넘긴 값만 쓴다.
그래서 측정 워커 스레드에서 불러도 되고, 테스트는 임시 폴더의 파일만으로 된다.
그래프는 matplotlib 의 Agg 백엔드로 그린다(창이 필요 없다). matplotlib 가 없으면
PNG 없이 **글만 있는 리포트**가 나온다 — 알림 자체가 실패하지는 않는다.

왜 이렇게 나눴나: ntfy 전송(notify/ntfy.py)과 리포트 조립을 섞으면, 그래프를
그리려고 네트워크 코드를 import 하거나 그 반대가 된다. 여기는 '문서를 만든다'
까지만 하고, 보내는 일은 AlarmManager.send_report 가 맡는다.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from pythonization.automation.summarize import read_dat

#: 그래프에 그릴 최대 점 수 — 넘으면 균등 간격으로 솎는다 (1200점짜리도 가볍게).
MAX_PLOT_POINTS = 600
#: 자동 선택 시 y 축 최대 개수
MAX_AUTO_Y = 3
#: x 축 후보 이름 (앞에 있을수록 우선) — 없으면 첫 열을 쓴다
_X_PREFERENCE = ("freq", "Bfield", "time", "Temperature")


@dataclass
class Report:
    """보낼 준비가 끝난 리포트 한 장."""
    title: str
    text: str                                   # 알림 본문 (마크다운)
    png: Optional[bytes] = None                 # 그래프 이미지 (없을 수 있다)
    png_name: str = "graph.png"
    data_file: Optional[str] = None
    warnings: List[str] = field(default_factory=list)

    def has_graph(self) -> bool:
        return bool(self.png)


def _fmt(v) -> str:
    """숫자는 유효숫자 6자리까지, 나머지는 그대로."""
    if isinstance(v, bool) or v is None:
        return str(v)
    if isinstance(v, (int,)):
        return str(v)
    if isinstance(v, float):
        if v != v:                               # NaN
            return "nan"
        return f"{v:.6g}"
    return str(v)


def _pick_axes(headers: Sequence[str], rows: Sequence[Sequence[Optional[float]]],
               x_col: Optional[str], y_cols: Optional[Sequence[str]]):
    """x/y 열을 정한다. 지정이 없으면 이름 우선순위 → 변화가 있는 열 순으로 고른다."""
    names = list(headers)
    if x_col and x_col in names:
        x = x_col
    else:
        x = next((n for n in _X_PREFERENCE if n in names), names[0] if names else "")
    if y_cols:
        ys = [c for c in y_cols if c in names and c != x]
    else:
        # 값이 실제로 변하는 열을 우선 — 상수 열(설정값)은 그래프로 쓸모가 없다
        ys = []
        for i, n in enumerate(names):
            if n == x:
                continue
            vals = [r[i] for r in rows if i < len(r) and r[i] is not None]
            if len(vals) >= 2 and max(vals) != min(vals):
                ys.append(n)
        ys = ys[:MAX_AUTO_Y]
    return x, ys


def _downsample(n: int, k: int) -> List[int]:
    if n <= k:
        return list(range(n))
    step = n / float(k)
    return [min(n - 1, int(i * step)) for i in range(k)]


def render_graph_png(headers: Sequence[str], rows: Sequence[Sequence[Optional[float]]],
                     x_col: str, y_cols: Sequence[str], *, title: str = "",
                     max_points: int = MAX_PLOT_POINTS) -> Optional[bytes]:
    """x 축 하나에 y 열 여러 개를 그려 PNG 바이트로 돌려준다.

    matplotlib 가 없으면 None (선택 의존성 — 없어도 글 리포트는 나간다).
    Agg 백엔드를 명시해 창·DISPLAY 없이 그린다.
    """
    if not rows or not x_col or not y_cols:
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")                    # 창 없는 백엔드 (반드시 pyplot 전에)
        import matplotlib.pyplot as plt
    except Exception:
        return None

    names = list(headers)
    xi = names.index(x_col)
    idxs = _downsample(len(rows), max_points)
    xs = [rows[i][xi] if xi < len(rows[i]) else None for i in idxs]

    fig, ax = plt.subplots(figsize=(7.0, 4.0), dpi=110)
    drawn = 0
    for name in y_cols:
        yi = names.index(name)
        pts = [(x, rows[i][yi] if yi < len(rows[i]) else None)
               for x, i in zip(xs, idxs)]
        pts = [(x, y) for x, y in pts if x is not None and y is not None]
        if len(pts) < 2:
            continue
        ax.plot([p[0] for p in pts], [p[1] for p in pts], linewidth=1.0, label=name)
        drawn += 1
    if not drawn:
        plt.close(fig)
        return None
    ax.set_xlabel(x_col)
    if len(y_cols) == 1:
        ax.set_ylabel(y_cols[0])
    else:
        ax.legend(fontsize=8, loc="best")
    if title:
        ax.set_title(title, fontsize=10)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    buf = io.BytesIO()
    try:
        fig.savefig(buf, format="png")
    finally:
        plt.close(fig)
    return buf.getvalue()


def last_values(headers: Sequence[str], rows: Sequence[Sequence[Optional[float]]],
                skip: Sequence[str] = ()) -> Dict[str, float]:
    """마지막 행의 값들 — 장비 온도·자기장처럼 '지금 상태'를 보여주는 열."""
    if not rows:
        return {}
    last = rows[-1]
    out: Dict[str, float] = {}
    for i, name in enumerate(headers):
        if name in skip or i >= len(last) or last[i] is None:
            continue
        out[name] = last[i]
    return out


def build_report(*, title: str,
                 conditions: Optional[Dict[str, object]] = None,
                 data_file: Optional[Path] = None,
                 x_col: Optional[str] = None,
                 y_cols: Optional[Sequence[str]] = None,
                 instrument_values: Optional[Dict[str, object]] = None,
                 progress: str = "",
                 note: str = "",
                 max_points: int = MAX_PLOT_POINTS) -> Report:
    """리포트 한 장을 조립한다.

    `data_file` 이 있으면 거기서 그래프와 '마지막 값'을 뽑는다. 파일이 없거나
    읽히지 않아도 **리포트는 만들어진다** — 조건·진행만 담고 사유를 warnings 에 남긴다.
    장시간 측정에서 알림이 끊기는 것보다, 불완전해도 가는 편이 낫다.
    """
    warnings: List[str] = []
    headers: List[str] = []
    rows: List[List[Optional[float]]] = []
    png: Optional[bytes] = None
    png_name = "graph.png"

    path = Path(data_file) if data_file else None
    if path is not None:
        try:
            headers, _units, rows = read_dat(path)
        except Exception as e:
            warnings.append(f"데이터 파일을 읽지 못했습니다 ({path.name}): {e}")
    if rows:
        x, ys = _pick_axes(headers, rows, x_col, y_cols)
        try:
            png = render_graph_png(headers, rows, x, ys, title=path.name if path else "",
                                   max_points=max_points)
        except Exception as e:                   # 그림 실패가 알림을 막으면 안 된다
            warnings.append(f"그래프를 그리지 못했습니다: {e}")
        if png is None and not warnings:
            warnings.append("그래프 없음 (matplotlib 미설치 또는 그릴 값이 없음)")
        if path is not None:
            png_name = path.with_suffix(".png").name

    lines: List[str] = []
    if progress:
        lines.append(f"**진행**: {progress}")
    if note:
        lines.append(note)

    if conditions:
        lines.append("")
        lines.append("**측정 조건**")
        for k, v in conditions.items():
            lines.append(f"- {k}: {_fmt(v)}")

    # 장비 값: 호출자가 준 것이 우선, 없으면 데이터 마지막 행에서 뽑는다
    values: Dict[str, object] = dict(instrument_values or {})
    if not values and rows:
        values = dict(last_values(headers, rows))
    if values:
        lines.append("")
        lines.append("**장비 상태 (마지막 값)**")
        for k, v in values.items():
            lines.append(f"- {k}: {_fmt(v)}")

    if path is not None:
        lines.append("")
        lines.append(f"**데이터**: {path.name}" + (f"  ({len(rows)}점)" if rows else ""))
    for w in warnings:
        lines.append(f"- ⚠ {w}")

    return Report(title=title, text="\n".join(lines).strip(), png=png,
                  png_name=png_name,
                  data_file=str(path) if path else None, warnings=warnings)
