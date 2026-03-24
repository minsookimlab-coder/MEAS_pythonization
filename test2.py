"""
test2.py: self._session의 write 측정시스템 완전 재현
sweep_worker.run_step()의 모든 과정을 비복 측정과 함께 재현합니다.

프로세스 흐름:
  1. 기기 연결 (필요시 auto-open)
  2. 현재값 읽기 (첫 스텝) 또는 마지막 쓴 값 사용
  3. 다음 스텝값 계산
  4. Safety ramp를 통한 Write (5개 sub-step, 5ms 간격)
  5. Measurement 쿼리 (전압/전류)

사용 VISA 명령어 (2636A, main_ui_profile.yaml 참고):
  - write_smua_voltage: smua.source.levelv={v}
  - read_smua_voltage: print(smua.measure.v())
  - measurement: print(smua.measure.i())
"""
import sys
import time
import pyvisa
from dataclasses import dataclass
from typing import List, Tuple
import csv
from datetime import datetime

sys.path.insert(0, r"c:\Users\Main\Desktop\pythonization")

# ════════════════════════════════════════════════════════════════════════════
# 설정 값
# ════════════════════════════════════════════════════════════════════════════
ADDRESS = "192.168.0.10"
N_ITERATIONS = 10

# VISA 명령어 (main_ui_profile.yaml 참고)
CMD_SET_SMUA = "smua.source.levelv={v}"
QUERY_CURRENT_SMUA = "print(smua.source.levelv)"  # 현재값 readback
QUERY_MEASURE_V = "print(smua.measure.v())"      # 전압 측정
QUERY_MEASURE_I = "print(smua.measure.i())"      # 전류 측정

# Safety ramp 설정 (main_ui_profile.yaml 참고)
SAFETY_STEPS = 10
SAFETY_INTERVAL_MS = 5.0
SAFETY_INTERVAL_S = SAFETY_INTERVAL_MS / 1000.0

# Sweep 설정
START_VOLTAGE = 0.0
END_VOLTAGE = 1.0
SWEEP_STEPS = 5


# ════════════════════════════════════════════════════════════════════════════
# 타이밍 데이터 클래스
# ════════════════════════════════════════════════════════════════════════════
@dataclass
class StepTiming:
    """sweep_worker.py의 StepTiming과 대응"""
    step_index: int
    # 각 단계별 절대 시간 (t0 기준)
    t_worker_start: float = 0.0      # run_step 진입
    t_connect: float = 0.0            # 기기 연결 완료
    t_source_read_done: float = 0.0   # 현재값 읽기 완료
    t_write_start: float = 0.0        # write 시작
    t_write_done: float = 0.0         # 모든 write 완료
    t_meas_done: float = 0.0          # 모든 measurement 완료

    # 각 sub-step별 세부 타이밍
    ramp_steps: List[Tuple[float, float]] = None  # [(sub_value, elapsed_ms), ...]
    
    def __post_init__(self):
        self.ramp_steps = []


@dataclass
class ProcessMetrics:
    """프로세스 메트릭 수집용"""
    step_index: int
    current_voltage: float
    next_voltage: float
    connection_ms: float
    source_read_ms: float
    write_total_ms: float
    write_per_step_ms: float
    measure_total_ms: float
    total_step_ms: float


# ════════════════════════════════════════════════════════════════════════════
# 헬퍼 함수
# ════════════════════════════════════════════════════════════════════════════

def initialize_2636a(inst):
    """2636A 초기화 (keithley_2636a.py의 _post_connect 참고)"""
    print("[Init] Disabling TSP prompts...")
    inst.write("localnode.prompts = 0")
    
    print("[Init] Clearing buffers...")
    inst.clear()
    
    print("[Init] Clearing error queue...")
    inst.write("errorqueue.clear()")
    
    print("[Init] Setting to source voltage mode...")
    inst.write("smua.source.func = smua.OUTPUT_DCVOLTS")
    inst.write("smua.source.output = smua.OUTPUT_ON")


def read_current_voltage(inst, t0):
    """현재 소스 전압 읽기 (첫 스텝만 필요)"""
    t_start = time.perf_counter()
    try:
        result = inst.query(QUERY_CURRENT_SMUA).strip()
        current = float(result)
        elapsed = (time.perf_counter() - t_start) * 1000
        print(f"  [SOURCE READ] Current voltage: {current:.6f} V ({elapsed:.2f} ms)")
        return current, elapsed
    except Exception as e:
        print(f"  [ERROR] Source read failed: {e}")
        raise


def write_with_safety_ramp(inst, current_v, next_v, t0, timing):
    """Safety ramp을 통한 write (sweep_worker.run_step 참고)"""
    print(f"  [WRITE] Starting safety ramp: {current_v:.6f} V → {next_v:.6f} V ({SAFETY_STEPS} steps)")
    
    # sub_v 계산
    sub_vs = [
        current_v + (next_v - current_v) * i / SAFETY_STEPS
        for i in range(1, SAFETY_STEPS + 1)
    ]
    
    t_start = time.perf_counter()
    
    for idx, sub_v in enumerate(sub_vs):
        t_sub_start = time.perf_counter()
        
        # Write sub-step value
        cmd = CMD_SET_SMUA.format(v=sub_v)
        inst.write(cmd)
        
        # Sleep (if interval > 0)
        if SAFETY_INTERVAL_S > 0:
            time.sleep(SAFETY_INTERVAL_S)
        
        t_sub_elapsed = (time.perf_counter() - t_sub_start) * 1000
        timing.ramp_steps.append((sub_v, t_sub_elapsed))
        
        if idx == 0 or idx == len(sub_vs) - 1:
            print(f"    └─ Step {idx+1}/{SAFETY_STEPS}: {sub_v:.6f} V ({t_sub_elapsed:.2f} ms)")
    
    total_write_ms = (time.perf_counter() - t_start) * 1000
    avg_per_step = total_write_ms / SAFETY_STEPS
    print(f"  [WRITE] Ramp complete: {total_write_ms:.2f} ms total ({avg_per_step:.2f} ms per step)")
    
    return total_write_ms, avg_per_step


def perform_measurements(inst, t0, timing):
    """Measurement 쿼리 수행"""
    measurements = {
        "voltage": QUERY_MEASURE_V,
        "current": QUERY_MEASURE_I,
    }
    
    results = {}
    t_start = time.perf_counter()
    
    print(f"  [MEASUREMENT] Reading responses...")
    for name, cmd in measurements.items():
        try:
            t_m_start = time.perf_counter()
            result = inst.query(cmd).strip()
            value = float(result)
            elapsed = (time.perf_counter() - t_m_start) * 1000
            results[name] = (value, elapsed)
            print(f"    └─ {name}: {value:.6e} ({elapsed:.2f} ms)")
        except Exception as e:
            print(f"    └─ {name}: ERROR - {e}")
            results[name] = (None, 0)
    
    total_meas_ms = (time.perf_counter() - t_start) * 1000
    print(f"  [MEASUREMENT] Total: {total_meas_ms:.2f} ms")
    
    return results, total_meas_ms


def calculate_next_step(current, start, end, steps):
    """다음 스텝값 계산 (선형 sweep)"""
    step_size = (end - start) / steps
    next_v = current + step_size
    return min(next_v, end), next_v >= end


# ════════════════════════════════════════════════════════════════════════════
# 메인 프로세스
# ════════════════════════════════════════════════════════════════════════════

def run_write_step_iteration(inst, step_idx, current_v, t0_session):
    """
    Single write sweep step 재현 (sweep_worker.run_step과 동일 프로세스)
    
    Returns:
        (current_voltage, next_voltage, metrics)
    """
    print(f"\n{'='*70}")
    print(f"STEP {step_idx + 1}: Write + Measure Cycle")
    print(f"{'='*70}")
    
    timing = StepTiming(step_index=step_idx)
    t_step_start = time.perf_counter()
    timing.t_worker_start = (t_step_start - t0_session) * 1000
    
    # [1] 기기 연결 (재사용 가능)
    t_conn_start = time.perf_counter()
    # inst는 이미 연결되어 있음 (auto-open 시뮬레이션)
    timing.t_connect = (time.perf_counter() - t_conn_start) * 1000
    conn_ms = timing.t_connect
    
    # [2] 현재값 읽기 (첫 스텝만 실제 읽음)
    if step_idx == 0:
        current_v, source_read_ms = read_current_voltage(inst, t0_session)
    else:
        print(f"  [SOURCE READ] Using last write value: {current_v:.6f} V (skipped)")
        source_read_ms = 0
    
    timing.t_source_read_done = (time.perf_counter() - t0_session) * 1000
    
    # [3] 다음 스텝값 계산
    next_v, is_done = calculate_next_step(
        current_v, START_VOLTAGE, END_VOLTAGE, SWEEP_STEPS
    )
    print(f"  [CALC] Next value: {next_v:.6f} V (done: {is_done})")
    
    # [4] Safety ramp을 통한 write
    timing.t_write_start = (time.perf_counter() - t0_session) * 1000
    write_total_ms, write_per_step_ms = write_with_safety_ramp(
        inst, current_v, next_v, t0_session, timing
    )
    timing.t_write_done = (time.perf_counter() - t0_session) * 1000
    
    # [5] Measurement 수행
    meas_results, meas_total_ms = perform_measurements(inst, t0_session, timing)
    timing.t_meas_done = (time.perf_counter() - t0_session) * 1000
    
    # 메트릭 계산
    total_step_ms = (time.perf_counter() - t_step_start) * 1000
    
    metrics = ProcessMetrics(
        step_index=step_idx,
        current_voltage=current_v,
        next_voltage=next_v,
        connection_ms=conn_ms,
        source_read_ms=source_read_ms,
        write_total_ms=write_total_ms,
        write_per_step_ms=write_per_step_ms,
        measure_total_ms=meas_total_ms,
        total_step_ms=total_step_ms,
    )
    
    # 요약 출력
    print(f"\n  [SUMMARY]")
    print(f"    Connection:  {metrics.connection_ms:>8.2f} ms")
    print(f"    Source Read: {metrics.source_read_ms:>8.2f} ms")
    print(f"    Write Ramp:  {metrics.write_total_ms:>8.2f} ms")
    print(f"    Measurement: {metrics.measure_total_ms:>8.2f} ms")
    print(f"    ─────────────────────────")
    print(f"    Total Step:  {metrics.total_step_ms:>8.2f} ms")
    
    return next_v, metrics


# ════════════════════════════════════════════════════════════════════════════
# 메인 진입점
# ════════════════════════════════════════════════════════════════════════════

def main():
    print("\n")
    print("╔" + "═"*68 + "╗")
    print("║" + " "*15 + "WRITE MEASUREMENT SYSTEM REPRODUCTION" + " "*16 + "║")
    print("║" + " "*68 + "║")
    print("║" + f" Device: Keithley 2636A ({ADDRESS})" + " "*33 + "║")
    print("║" + f" Safety Ramp: {SAFETY_STEPS} steps × {SAFETY_INTERVAL_MS} ms" + " "*34 + "║")
    print("║" + f" Iterations: {N_ITERATIONS}" + " "*51 + "║")
    print("╚" + "═"*68 + "╝")
    print()
    
    try:
        # PyVISA 초기화
        print(f"[INIT] Connecting to {ADDRESS}...")
        rm = pyvisa.ResourceManager()
        inst = rm.open_resource(f"TCPIP0::{ADDRESS}::inst0::INSTR")
        inst.timeout = 5000
        inst.read_termination = "\n"
        inst.write_termination = "\n"
        
        print(f"[INIT] Connected!")
        print(f"[INIT] IDN: {inst.query('*IDN?')}")
        
        # 2636A 초기화
        initialize_2636a(inst)
        
        # CSV 파일 준비
        csv_filename = f"test2_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        csv_file = open(csv_filename, 'w', newline='')
        csv_writer = csv.writer(csv_file)
        
        # CSV 헤더
        csv_writer.writerow([
            "Iteration", "Step", "Current_V", "Next_V",
            "Conn_ms", "SourceRead_ms", "WriteTotal_ms", "WritePerStep_ms",
            "Measure_ms", "TotalStep_ms"
        ])
        
        # 메인 루프 (N_ITERATIONS 반복)
        all_metrics = []
        current_voltage = START_VOLTAGE
        
        for iteration in range(N_ITERATIONS):
            print(f"\n{'#'*70}")
            print(f"# ITERATION {iteration + 1}/{N_ITERATIONS}")
            print(f"{'#'*70}")
            
            t0_iter = time.perf_counter()
            
            # 각 스텝별 write + measure (SWEEP_STEPS개)
            for step_idx in range(SWEEP_STEPS):
                current_voltage, metrics = run_write_step_iteration(
                    inst, step_idx, current_voltage, t0_iter
                )
                all_metrics.append(metrics)
                
                # CSV 기록
                csv_writer.writerow([
                    iteration + 1, step_idx + 1,
                    f"{metrics.current_voltage:.6f}",
                    f"{metrics.next_voltage:.6f}",
                    f"{metrics.connection_ms:.2f}",
                    f"{metrics.source_read_ms:.2f}",
                    f"{metrics.write_total_ms:.2f}",
                    f"{metrics.write_per_step_ms:.2f}",
                    f"{metrics.measure_total_ms:.2f}",
                    f"{metrics.total_step_ms:.2f}",
                ])
                csv_file.flush()
        
        csv_file.close()
        
        # 종합 통계
        print(f"\n{'='*70}")
        print("COMPREHENSIVE STATISTICS")
        print(f"{'='*70}\n")
        
        if all_metrics:
            connection_times = [m.connection_ms for m in all_metrics]
            source_read_times = [m.source_read_ms for m in all_metrics if m.source_read_ms > 0]
            write_times = [m.write_total_ms for m in all_metrics]
            measure_times = [m.measure_total_ms for m in all_metrics]
            total_times = [m.total_step_ms for m in all_metrics]
            
            def stats(lst, name):
                if not lst:
                    return
                avg_time = sum(lst) / len(lst)
                min_time = min(lst)
                max_time = max(lst)
                print(f"{name:.<40} {avg_time:>8.2f} ms (min: {min_time:>6.2f}, max: {max_time:>6.2f})")
            
            print("Average Times:")
            stats(connection_times, "  Connection")
            if source_read_times:
                stats(source_read_times, "  Source Read (first step only)")
            stats(write_times, "  Write (with ramp)")
            stats(measure_times, "  Measurement")
            stats(total_times, "  Total per Step")
            
            print(f"\nTotal measurements: {len(all_metrics)}")
            print(f"CSV Results saved: {csv_filename}")
        
        inst.close()
        rm.close()
        print(f"\n[DONE] Session closed.\n")

    except Exception as e:
        print(f"\n[ERROR] {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()