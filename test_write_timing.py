"""
write/query 소요 시간 분석 — 실제 sweep 시퀀스와 비교

[가설별 체크 포인트]
  A. pyvisa write 자체         → ~0.87ms (이미 확인)
  B. pyvisa query 자체         → 네트워크 RTT + 기기 처리
  C. time.sleep(0.001) 실제    → Windows 타이머 해상도 문제 (최소 ~15ms)
  D. safety ramp 5회 write     → write×5 + sleep×5 합산
  E. InstrumentSession 경유    → lock + is_open + format 오버헤드
"""
import sys
import threading
import time
import pyvisa

sys.path.insert(0, r"c:\Users\Main\Desktop\pythonization")

ADDRESS  = "192.168.0.10"
CMD_SET  = "smua.source.levelv={v}"
QUERY_CMD = "print(smua.source.levelv)"
N        = 10
VALUE    = 0.0

SEP = "─" * 52

rm   = pyvisa.ResourceManager()
inst = rm.open_resource(f"TCPIP0::{ADDRESS}::inst0::INSTR")
inst.timeout           = 5000
inst.read_termination  = "\n"
inst.write_termination = "\n"
# TSP 프롬프트 비활성화 (실제 앱과 동일 조건)
inst.write("localnode.prompts = 0")
inst.clear()
inst.write("errorqueue.clear()")
print(f"Connected: {ADDRESS}\n")

lock = threading.Lock()

def avg(lst): return sum(lst) / len(lst) if lst else 0.0
def print_samples(label, samples):
    print(f"\n{SEP}")
    print(f"[{label}]")
    for i, v in enumerate(samples):
        print(f"  {i:>2}: {v:.3f} ms")
    print(f"  avg: {avg(samples):.3f} ms   min: {min(samples):.3f}   max: {max(samples):.3f}")

# ══ A. pyvisa write 단독 ═══════════════════════════════════════════════════
samples_a = []
for _ in range(N):
    t0 = time.perf_counter()
    inst.write(CMD_SET.format(v=VALUE))
    samples_a.append((time.perf_counter() - t0) * 1000)
print_samples("A. pyvisa write 단독", samples_a)

# ══ B. pyvisa query 단독 ══════════════════════════════════════════════════
samples_b = []
for _ in range(N):
    t0 = time.perf_counter()
    inst.query(QUERY_CMD)
    samples_b.append((time.perf_counter() - t0) * 1000)
print_samples("B. pyvisa query 단독", samples_b)

# ══ C. time.sleep(1ms) 실제 대기 시간 ════════════════════════════════════
# Windows 기본 타이머 해상도는 ~15ms → sleep(0.001)이 15ms 이상 걸릴 수 있음
samples_c = []
for _ in range(N):
    t0 = time.perf_counter()
    time.sleep(0.0001)
    samples_c.append((time.perf_counter() - t0) * 1000)
print_samples("C. time.sleep(0.001) 실제 대기", samples_c)

# ══ D. safety ramp 5회 write + sleep(1ms) 시뮬레이션 ══════════════════════
# 실제 safety_steps=5, safety_interval_ms=1.0 조건 재현
SAFETY_STEPS = 5
INTERVAL_S   = 0.001
current = 0.0
next_v  = 0.1
sub_vs  = [current + (next_v - current) * i / SAFETY_STEPS
           for i in range(1, SAFETY_STEPS + 1)]

samples_d = []
for _ in range(N):
    t0 = time.perf_counter()
    for sub_v in sub_vs:
        inst.write(CMD_SET.format(v=sub_v))
        time.sleep(INTERVAL_S)
    samples_d.append((time.perf_counter() - t0) * 1000)
    # 원위치
    inst.write(CMD_SET.format(v=VALUE))
print_samples(f"D. safety ramp ×{SAFETY_STEPS} + sleep({INTERVAL_S*1000:.0f}ms) 합산", samples_d)

# ══ E. lock 경유 write (InstrumentSession 모방) ════════════════════════════
samples_e = []
for _ in range(N):
    t0 = time.perf_counter()
    with lock:
        inst.write(CMD_SET.format(v=VALUE))
    samples_e.append((time.perf_counter() - t0) * 1000)
print_samples("E. lock 경유 write", samples_e)

# ══ 요약 ══════════════════════════════════════════════════════════════════
print(f"\n{SEP}")
print("[ 요약 ]")
print(f"  A. write 단독          : {avg(samples_a):.2f} ms")
print(f"  B. query 단독          : {avg(samples_b):.2f} ms")
print(f"  C. sleep(1ms) 실제     : {avg(samples_c):.2f} ms  ← Windows 해상도 문제?")
print(f"  D. ramp×{SAFETY_STEPS}+sleep합산   : {avg(samples_d):.2f} ms  ← 실제 safety ramp 1스텝")
print(f"  E. lock+write          : {avg(samples_e):.2f} ms")
print(f"  D에서 sleep 기여분     : {avg(samples_d) - SAFETY_STEPS*avg(samples_a):.2f} ms")

inst.close()
rm.close()
