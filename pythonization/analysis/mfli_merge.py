"""
core/mfli_merge.py — LabOne sweeper CSV + MFLI Noise Sweep 모듈 .dat 를 sweep 단위로
분리·대응·병합하고 sweep별 파일(sweep_N_merged_data.dat)로 저장한다.

동작
  1) LabOne CSV(여러 sweep=chunk 누적)와 우리 측정 데이터를 읽는다. 우리 데이터는 단일 .dat(주파수
     wrap으로 분리) 또는 per-sweep 저장 폴더(*.dat 각 1 sweep) 둘 다 지원.
  2) 두 소스를 각각 sweep으로 분리한 뒤 1↔1, 2↔2 … 처음부터 순차 병합한다. 둘 중 한쪽의 sweep이
     떨어지면 종료하고, 이후 개수 불일치를 경고로 반환한다. 병합 결과는 **LabOne grid 전체(예: 200점)를
     기준 행**으로 하고, 각 grid 점에 같은 주파수의 우리 측정을 붙인다. 매칭은 rank가 아니라 '로그-주파수
     최근접'이다(우리 주파수는 grid의 부분집합 — rank로 짝지으면 poll로 놓친 점 때문에 통째로 밀림).
     우리가 poll로 읽지 못한 grid 점은 Module_frequency·aux(온도/M81 등)를 **NaN**으로 채운다
     (노이즈 x/y/r/X_noise/R_noise/NEPBW는 LabOne 값이라 grid 전 점에 존재). 하나의 주파수로 합치지 않고
     LabOne_frequency·Module_frequency 두 열을 모두 내보낸다(겹치는 행은 둘이 거의 같아야 정상).
  3) 각 결과에 Noise level 컬럼을 계산해 추가한다:
        X_noise, R_noise (V/√Hz) = xstddev|rstddev / √(bandwidth)   [bandwidth 필드 = NEPBW]

Qt·numpy 의존 없음(표준 라이브러리만) → GUI 버튼과 CLI(python -m pythonization.analysis.mfli_merge …) 양쪽에서 사용.
"""
import bisect
import csv
import math
import re
from pathlib import Path
from typing import Optional


def _num_key(name: str):
    """파일명/라벨의 '마지막 숫자'로 정렬하는 natural-sort 키.
    `test_x001.dat`·`test_x1000.dat`을 001<…<999<1000 순으로(문자열 정렬은 1000을 101 앞에 두어 깨짐)."""
    nums = re.findall(r"\d+", str(name))
    return (int(nums[-1]) if nums else -1, str(name))


# ---------------------------------------------------------------- LabOne
def find_labone_csv(path: Path):
    """CSV 파일이면 [그 파일], 폴더면 그 안의 데이터 CSV(*_sample_*.csv, header 제외) 정렬 목록."""
    path = Path(path)
    if path.is_file():
        return [path]
    files = sorted(p for p in path.glob("*sample*.csv") if "header" not in p.name.lower())
    if not files:
        raise FileNotFoundError(f"LabOne 데이터 CSV를 찾지 못함: {path}")
    return files


def parse_labone(paths):
    """LabOne CSV(들)을 파싱해 sweep 목록 반환. 각 sweep = (label, {field:[float]}).

    형식: 세미콜론 구분, 행 = 'chunk;timestamp;size;fieldname;v0;v1;…'(뒤는 nan 패딩). chunk=sweep 번호."""
    sweeps = []
    for path in paths:
        with open(path, "r", encoding="utf-8", newline="") as f:
            rows = csv.reader(f, delimiter=";")
            next(rows, None)
            cur_chunk, cur = None, None
            for r in rows:
                if len(r) < 5:
                    continue
                chunk, fld = r[0], r[3]
                vals = []
                for x in r[4:]:
                    x = x.strip()
                    if x == "" or x.lower() == "nan":
                        break
                    try:
                        vals.append(float(x))
                    except ValueError:
                        break
                if chunk != cur_chunk:
                    cur = {}
                    sweeps.append((f"{path.name}#chunk{chunk}", cur))
                    cur_chunk = chunk
                cur[fld] = vals
    return sweeps


# ---------------------------------------------------------------- ours .dat
def parse_ours(path: Path):
    """우리 .dat 파싱 → (labels, units, {label:[float]}). 헤더 2줄(라벨/단위) + 데이터."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    labels = lines[0].split("\t")
    units = lines[1].split("\t") if len(lines) > 1 else [""] * len(labels)
    cols = {lab: [] for lab in labels}
    for ln in lines[2:]:
        if not ln.strip():
            continue
        parts = ln.split("\t")
        for i, lab in enumerate(labels):
            try:
                cols[lab].append(float(parts[i]))
            except (ValueError, IndexError):
                cols[lab].append(float("nan"))
    return labels, units, cols


def split_by_wrap(freq):
    """주파수 배열을 sweep 구간으로 분리. 인접 값이 전체 범위의 50% 넘게 점프하면 새 sweep."""
    n = len(freq)
    if n == 0:
        return []
    span = (max(freq) - min(freq)) or 1.0
    bounds = [0]
    for i in range(1, n):
        if abs(freq[i] - freq[i - 1]) > 0.5 * span:
            bounds.append(i)
    bounds.append(n)
    return [(bounds[k], bounds[k + 1]) for k in range(len(bounds) - 1)]


# ---------------------------------------------------------------- helpers
def interp_clamped(x, xp, fp):
    """xp(오름차순)에 대해 fp를 선형보간. 범위 밖은 끝값으로 clamp. 길이 1이면 그 값."""
    n = len(xp)
    if n == 0:
        return float("nan")
    if n == 1 or x <= xp[0]:
        return fp[0]
    if x >= xp[-1]:
        return fp[-1]
    lo, hi = 0, n - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if xp[mid] <= x:
            lo = mid
        else:
            hi = mid
    x0, x1, y0, y1 = xp[lo], xp[hi], fp[lo], fp[hi]
    return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def _fmt(v):
    return "nan" if (v is None or (isinstance(v, float) and math.isnan(v))) else f"{v:.10E}"


def merge_pair(lab_sweep, our_seg, our_labels, our_units, cols):
    """LabOne grid 전체(예: 200점)를 '기준 행'으로 삼고, 각 grid 점에 같은 주파수의 우리 측정을 붙인다.

    출력은 grid 점 개수만큼(= 200행). 우리가 poll로 읽지 못한 grid 점은 Module_frequency와
    aux(probe_T/M81_V)를 NaN으로 채운다(노이즈 x/y/r/X_noise/R_noise/NEPBW는 LabOne 값이라 항상 있음).
    매칭은 rank가 아니라 '로그-주파수 최근접'이다 — 우리 주파수는 grid의 부분집합이라 각 우리 점이
    자기 grid 점에 정확히 배정된다(둘이 겹치는 행은 LabOne_frequency≈Module_frequency)."""
    L = lab_sweep
    lab_freq = L.get("grid") or L.get("frequency") or []   # grid = 스윕 축(권위값)
    m = len(lab_freq)
    if m == 0:
        return None

    def lget(name, k):
        v = L.get(name, [])
        return v[k] if (0 <= k < len(v)) else float("nan")

    # grid를 주파수 오름차순으로 (출력 행 순서). 매칭용 로그축은 양수 grid만.
    order = sorted(range(m), key=lambda k: lab_freq[k])
    pos_order = [k for k in order if lab_freq[k] > 0]
    slog = [math.log(lab_freq[k]) for k in pos_order]

    s0, s1 = our_seg
    of = cols["frequency"][s0:s1]
    aux_names = [lab for lab in our_labels if lab not in ("frequency", "time")]

    # 우리 각 점을 '가장 가까운 grid 점'에 배정(원래 grid 인덱스 → 우리 local 인덱스).
    # 한 grid 점에 여러 우리 점이 몰리면 주파수가 더 가까운 것만 남긴다.
    assign = {}
    for li, f in enumerate(of):
        if f <= 0 or not slog:
            continue
        lf = math.log(f)
        p = bisect.bisect_left(slog, lf)
        cands = []
        if p < len(slog):
            cands.append(p)
        if p > 0:
            cands.append(p - 1)
        best = min(cands, key=lambda q: abs(slog[q] - lf))
        gidx = pos_order[best]
        dist = abs(slog[best] - lf)
        if gidx not in assign or dist < assign[gidx][0]:
            assign[gidx] = (dist, li)

    unit_map = dict(zip(our_labels, our_units))
    labels = (["LabOne_frequency", "Module_frequency", "x", "y", "r",
               "X_noise", "R_noise", "NEPBW"] + aux_names)
    units = (["Hz", "Hz", "V", "V", "V", "V/sqrtHz", "V/sqrtHz", "Hz"]
             + [unit_map.get(a, "") for a in aux_names])

    nan = float("nan")
    rows = []                              # grid 점 1개당 1행 (기준 = LabOne 200점)
    for k in order:
        bw = lget("bandwidth", k)
        xs, rs = lget("xstddev", k), lget("rstddev", k)
        xn = xs / math.sqrt(bw) if (bw and bw > 0 and not math.isnan(xs)) else nan
        rn = rs / math.sqrt(bw) if (bw and bw > 0 and not math.isnan(rs)) else nan
        row = [lab_freq[k], nan, lget("x", k), lget("y", k), lget("r", k), xn, rn, bw]
        if k in assign:                    # 이 grid 점에 우리 측정이 있으면 채움
            li = assign[k][1]
            row[1] = of[li]                                    # Module_frequency
            row += [cols[a][s0:s1][li] for a in aux_names]     # 우리 aux
        else:                              # 우리가 안 읽은 grid 점 → NaN
            row += [nan] * len(aux_names)
        rows.append(row)
    return labels, units, rows


def write_dat(path: Path, labels, units, rows):
    lines = ["\t".join(labels), "\t".join(units)]
    for row in rows:
        lines.append("\t".join(_fmt(v) for v in row))
    Path(path).write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------- ours: 파일/폴더 → sweep 목록
def iter_our_sweeps(ours):
    """우리 측정 데이터를 sweep 목록으로 반환: [(labels, units, cols, (s0,s1), srcname), ...].

    - ours가 '파일'이면: 옛 단일 파일 형식 — 주파수 wrap으로 여러 sweep을 분리.
    - ours가 '폴더'이면: 새 per-sweep 저장 형식 — *.dat(정렬)마다 파싱(보통 파일 1개=sweep 1개,
      혹시 내부에 wrap이 있으면 더 분리). 병합 결과(*merged_data.dat)는 입력에서 제외한다.
    """
    ours = Path(ours)
    if ours.is_dir():
        # 숫자 정렬 필수: 1000개+에서 문자열 정렬은 x1000을 x101 앞에 두어 순서를 망친다.
        files = sorted((f for f in ours.glob("*.dat")
                        if not f.name.endswith("merged_data.dat")),
                       key=lambda p: _num_key(p.stem))
        if not files:
            raise FileNotFoundError(f"우리 측정 .dat를 폴더에서 찾지 못함: {ours}")
    else:
        files = [ours]
    sweeps = []
    for fp in files:
        labels, units, cols = parse_ours(fp)
        if "frequency" not in cols:
            continue
        for (a, b) in split_by_wrap(cols["frequency"]):
            sweeps.append((labels, units, cols, (a, b), fp.name))
    return sweeps


# ---------------------------------------------------------------- public API
def merge_sweeps(labone, ours, outdir) -> dict:
    """LabOne CSV(파일/폴더) + 우리 .dat(파일 또는 per-sweep 폴더)를 sweep별로 병합해
    outdir에 sweep_N_merged_data.dat 저장.

    반환: {written:[파일명], outdir, n_labone, n_ours, n_paired, warning:str|None}
    """
    lab_paths = find_labone_csv(Path(labone))
    # parse_labone은 파일순(find_labone_csv가 이름순 정렬) + 파일 내 append순으로 chunk을 낸다
    # = 측정 시간순. LabOne이 큰 CSV를 _00001.csv로 롤오버하며 chunk 번호를 재시작해도 파일순이
    # 이를 흡수하므로, chunk 번호로 재정렬하지 않는다(재정렬하면 롤오버 시 순서가 섞임).
    lab_sweeps = parse_labone(lab_paths)
    our_sweeps = iter_our_sweeps(ours)
    if not our_sweeps:
        raise ValueError("우리 측정 .dat에서 유효한 sweep을 찾지 못했습니다.")

    # LabOne autosave에는 (a) 범위가 다른 sweep(중간에 start/stop 변경), (b) 부분/미완성 sweep
    # (첫·마지막 chunk가 잘림), (c) 노이즈가 아직 계산 안 된 sweep(예: 첫 sweep) 이 섞여 있을 수
    # 있다. 그냥 index로 1↔1 짝지으면 이런 것들이 sweep_1/2로 들어가 결과가 이상해진다.
    # → 우리 .dat 범위와 겹치고 + 완전(full grid)하고 + 노이즈가 유효한 LabOne sweep만 쓴다.
    our_all = [f for (_lab, _u, cols, (a, b), _n) in our_sweeps
               for f in cols["frequency"][a:b] if not math.isnan(f)]
    if not our_all:
        raise ValueError("측정 .dat에 유효한 frequency가 없습니다.")
    o0, o1 = min(our_all), max(our_all)

    def _grid(sw):
        return sw.get("grid") or sw.get("frequency") or []

    def _has_noise(sw):
        xs = sw.get("xstddev") or sw.get("rstddev") or []
        return any(not math.isnan(v) for v in xs)

    glen = max((len(_grid(sw)) for _l, sw in lab_sweeps), default=0)   # full sweep 포인트 수
    good_lab = []
    drop_range = drop_partial = drop_nonoise = 0
    for lbl, sw in lab_sweeps:
        g = _grid(sw)
        if not g:
            drop_partial += 1
            continue
        a, b = min(g), max(g)
        if min(b, o1) - max(a, o0) <= 0:            # (a) 범위 안 겹침
            drop_range += 1
        elif glen and len(g) < 0.9 * glen:          # (b) 부분(미완성) sweep
            drop_partial += 1
        elif not _has_noise(sw):                    # (c) 노이즈 미계산 sweep
            drop_nonoise += 1
        else:
            good_lab.append((lbl, sw))

    if not good_lab:
        lab_all = [f for _l, sw in lab_sweeps for f in _grid(sw)]
        lr = f"{min(lab_all):.0f}~{max(lab_all):.0f}" if lab_all else "?"
        raise ValueError(
            f"측정 범위({o0:.0f}~{o1:.0f} Hz)와 맞는 '완전한' LabOne sweep이 없습니다 "
            f"(LabOne 전체 {lr} Hz; 범위·부분·노이즈없음으로 전부 제외됨). "
            f"측정 .dat와 같은 범위를 온전히 스윕한 LabOne CSV를 선택하세요.")

    # 우리 측정도 부분(짧은) sweep은 제외 (중단된 마지막 패스 등)
    def _seg_len(s):
        (_l, _u, _c, (a, b), _n) = s
        return b - a
    slen = max((_seg_len(s) for s in our_sweeps), default=0)
    good_our = [s for s in our_sweeps if slen and _seg_len(s) >= 0.5 * slen]
    drop_our = len(our_sweeps) - len(good_our)

    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    # 이전 병합의 잔여 파일 제거 (이번보다 많이 만들었으면 stale 파일이 남아 혼란)
    for old in outdir.glob("sweep_*_merged_data.dat"):
        try:
            old.unlink()
        except Exception:
            pass

    n_pair = min(len(good_lab), len(good_our))
    written = []
    for i in range(n_pair):
        _label, lab_sweep = good_lab[i]
        o_labels, o_units, o_cols, o_seg, _src = good_our[i]
        res = merge_pair(lab_sweep, o_seg, o_labels, o_units, o_cols)
        if res is None:
            continue
        labels, units, rows = res
        out = outdir / f"sweep_{i + 1}_merged_data.dat"
        write_dat(out, labels, units, rows)
        written.append(out.name)

    warns = []
    if drop_range:
        warns.append(f"범위 다른 LabOne sweep {drop_range}개 제외.")
    if drop_partial:
        warns.append(f"부분(미완성) LabOne sweep {drop_partial}개 제외.")
    if drop_nonoise:
        warns.append(f"노이즈 미계산 LabOne sweep {drop_nonoise}개 제외(예: 첫 sweep).")
    if drop_our:
        warns.append(f"부분 측정 segment {drop_our}개 제외.")
    if len(good_lab) != len(good_our):
        more = "LabOne(유효)" if len(good_lab) > len(good_our) else "Ours(유효)"
        extra = abs(len(good_lab) - len(good_our))
        warns.append(f"유효 sweep 개수 불일치 LabOne {len(good_lab)} vs Ours {len(good_our)} — "
                     f"{more}에 {extra}개 더 있어 처음 {n_pair}개만 병합.")
    warning = " ".join(warns) if warns else None
    return {"written": written, "outdir": str(outdir),
            "n_labone": len(lab_sweeps), "n_labone_good": len(good_lab),
            "n_ours": len(our_sweeps), "n_ours_good": len(good_our),
            "n_paired": len(written), "warning": warning}


# ---------------------------------------------------------------- CLI
def _main():
    import argparse
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="LabOne CSV + MFLI .dat sweep별 병합")
    ap.add_argument("--labone", required=True, help="LabOne CSV 파일 또는 autosave 폴더")
    ap.add_argument("--ours",   required=True, help="MFLI Noise Sweep 모듈 .dat")
    ap.add_argument("--outdir", required=True, help="결과 저장 폴더")
    a = ap.parse_args()
    res = merge_sweeps(a.labone, a.ours, a.outdir)
    for name in res["written"]:
        print(f"  [OK] {name}")
    print(f"완료: {res['n_paired']}개 -> {res['outdir']} "
          f"(LabOne {res['n_labone']} / Ours {res['n_ours']} sweeps)")
    if res["warning"]:
        print(f"[WARN] {res['warning']}")


if __name__ == "__main__":
    _main()
