# Measurement System — 사용자 매뉴얼

---

## 목차

1. [시작하기](#1-시작하기)
2. [장비 등록 — Instrument Settings](#2-장비-등록--instrument-settings)
3. [VISA 라이브러리 관리](#3-visa-라이브러리-관리)
4. [Parameter Manager — 측정 프로파일 구성](#4-parameter-manager--측정-프로파일-구성)
5. [메인 창 — 단일 Sweep 측정](#5-메인-창--단일-sweep-측정)
6. [Double Sweep](#6-double-sweep)
7. [Command Window — 즉석 명령 실행](#7-command-window--즉석-명령-실행)
8. [Graph — 실시간 그래프](#8-graph--실시간-그래프)
9. [Data / Timing / Sweep Status 보조 창](#9-data--timing--sweep-status-보조-창)
10. [Meta Data Config — 측정 전 메타 기록](#10-meta-data-config--측정-전-메타-기록)
11. [Debug 창 — 로그 및 콘솔](#11-debug-창--로그-및-콘솔)
12. [데이터 파일 형식](#12-데이터-파일-형식)
13. [알람 및 Telegram 알림](#13-알람-및-telegram-알림)
14. [Cycle Sweep](#14-cycle-sweep)
15. [Double Sweep+ (Cycle) — 온도·자기장마다 cycle 반복](#15-double-sweep-cycle--온도자기장마다-cycle-반복)
16. [VNA Control — Power Sweep 모드](#16-vna-control--power-sweep-모드)

---

## 1. 시작하기

### 실행 방법

프로젝트 루트에서 `pythonization.bat`을 실행하거나, 가상환경을 활성화한 뒤 직접 실행합니다.

```
python main.py
```

### 초기 설정 순서

처음 사용 시 아래 순서로 설정합니다.

```
장비 등록 (Instrument Settings)
  → VISA 라이브러리 작성 (VISA Library)
    → 측정 구성 (Parameter Manager)
      → 메인 창에서 Sweep 실행
```

---

## 2. 장비 등록 — Instrument Settings

**메뉴 → Settings → Instrument Settings**

### 2.1 장비 목록 (왼쪽 패널)

- 등록된 장비가 목록으로 표시됩니다.
- **+ Add** : 새 장비 추가
- **- Remove** : 선택된 장비 삭제
- 목록은 드래그로 순서를 변경할 수 있습니다.

### 2.2 장비 설정 (오른쪽 패널)

| 항목 | 설명 |
|------|------|
| **Alias** | 장비를 식별하는 고유 이름 (예: `2636A`, `ITC`) |
| **Device Type (Driver)** | 장비 드라이버 클래스 (`instruments/drivers/` 폴더에서 자동 검색됨) |
| **Interface Type** | 연결 방식: `LAN` / `GPIB` / `RS232` / `USB` |
| **Address** | IP 주소, GPIB 번호, COM 포트, USB 리소스 문자열 |
| **MAC Address** | LAN 장비에서 DHCP 고정용 MAC 주소 (선택). **Fetch MAC** 버튼으로 자동 입력 |
| **Port** | LAN Raw Socket 포트 (기본 INSTR 방식이면 0) |
| **VISA Resource Preview** | 설정 값으로 생성되는 실제 VISA 리소스 문자열 미리보기 |

### 2.3 동적 변수 (Extra Parameters)

장비 드라이버에서 사용할 추가 파라미터를 Key/Value 형태로 등록합니다.

| 예시 Key | 예시 Value | 설명 |
|----------|-----------|------|
| `timeout` | `5000` | 응답 대기 시간 (ms) |
| `baud_rate` | `9600` | RS232 보드레이트 |
| `isobus` | `1` | Oxford ITC ISOBUS 장비 번호 |
| `delay` | `0.1` | Oxford ITC query 응답 대기 (초) |

### 2.4 연결 테스트

**Test Connection** 버튼을 클릭하면 `*IDN?` 쿼리(또는 드라이버 고유 테스트 명령)를 전송하고 결과를 표시합니다.

### 2.5 저장

**Save & Apply** 버튼을 누르면 설정 폴더의 `instruments.yaml`에 저장되고 즉시 시스템에 반영됩니다.
(정확한 경로는 [부록 B](#부록-b--설정-파일-위치) 참고.)

---

## 3. VISA 라이브러리 관리

**메뉴 → Settings → VISA Library**

각 장비에 사용할 명령어(측정, Sweep, Write)를 등록하는 라이브러리입니다.

### 3.1 항목 종류

| 표시 | 종류 | 설명 |
|------|------|------|
| **[Q] Measurement** | 측정 명령 | 장비에 쿼리해서 값을 읽음 (`cmd_query`) |
| **[S] Sweep Value** | Sweep 명령 | 출력값 설정 (`cmd_set`) + 읽기 명령 (`paired_read_cmd`) |
| **[W] Write Command** | 단방향 명령 | 응답 없이 쓰기만 하는 명령 (`cmd_set`) |

### 3.2 명령어 템플릿

명령어에 `{변수명}` 형태의 플레이스홀더를 사용하면 Parameter Manager에서 실제 값을 채웁니다.

```
예) smua.source.levelv = {v}     ← {v}는 Sweep 값으로 자동 대입
    print(smua.measure.r())      ← 플레이스홀더 없음, 고정 명령
    SENS:FREQ {freq};:READ?      ← {freq}를 사용자가 직접 입력
```

### 3.3 등록 정보

| 필드 | 설명 |
|------|------|
| **Description** | 사람이 읽을 수 있는 이름 |
| **VISA Command** | 실제 명령어 문자열 |
| **Figure Axis** | 그래프/데이터 파일의 열 이름 |
| **Unit** | 데이터 단위 (예: `V`, `A`, `Ω`, `T`) |
| **Paired Read Cmd** | Sweep Value용 — 현재 출력값을 읽는 쿼리 명령 |

---

## 4. Parameter Manager — 측정 프로파일 구성

**메뉴 → Settings → Parameter Manager**

VISA 라이브러리에서 실제 측정에 사용할 항목들을 골라 프로파일로 구성합니다.

### 4.1 섹션 구성

Parameter Manager는 4개 섹션으로 구성됩니다.

- **Sweep Values** : 출력 소스로 사용할 항목 (전압/전류/자기장 등)
- **Measurements** : 측정할 항목 (저항/온도/전류 등)
- **Write Commands** : 매 스텝마다 실행할 단방향 명령
- **Second Sweep Channels** : Double Sweep의 두 번째 채널

### 4.2 항목 추가 방법

1. 상단 **Instrument** 드롭다운에서 장비 선택
2. **VISA** 드롭다운에서 명령어 선택
3. 플레이스홀더가 있으면 **Parameters** 영역에서 값 입력 또는 `[SWEEP]` 지정
4. **Description / Figure Axis / Unit** 수정 (필요 시)
5. **Add** 버튼으로 목록에 추가

### 4.3 플레이스홀더 설정

- `[SWEEP]` : 해당 파라미터를 Sweep 변수로 지정 (값이 각 스텝마다 자동 변경됨)
- 고정값 : 측정 전반에 걸쳐 고정된 값 사용

### 4.4 Sweep Value — Safety Ramp

큰 전압/전류를 급격히 인가할 때 장비 보호를 위해 Safety Ramp를 설정합니다.

| 항목 | 설명 |
|------|------|
| **Use Safety Ramp** | 체크 시 활성화 |
| **Steps** | 목표값까지 나누는 중간 단계 수 |
| **Interval** | 각 중간 단계 사이 대기 시간 (ms) |

Interval이 0이면 N개의 명령을 한 번의 VISA write로 묶어서 전송합니다(빠름).
Interval이 > 0이면 각 단계 사이에 실제로 대기합니다(장비 안정화).

### 4.5 Measurement — 타입 설정

| MeasType | 역할 |
|----------|------|
| `none` | 일반 측정값 |
| `contact` | 접촉저항 등 특수 접미사 처리 |
| `temperature` | Meta Data에 평균/표준편차 자동 기록 |
| `bfield` | Meta Data에 평균/표준편차 자동 기록 |

### 4.6 Second Sweep Channel

Double Sweep에서 사용할 두 번째 채널입니다. Advance Type을 선택합니다.

| Advance Type | 동작 |
|--------------|------|
| **Simple Hop** | 값을 즉시 설정하고 다음 phase로 진행 |
| **Sweep** | 지정 rate로 천천히 목표값에 접근 |
| **Feedback** | 값을 설정한 뒤 실제 측정값이 목표의 일정 % 이상 도달할 때까지 대기 |
| **Wait for Time** | 값 설정 후 지정 시간(초) 대기 |

### 4.7 Apply

**Apply** 버튼을 누르면 프로파일이 저장되고 메인 창의 Sweep Channel, 측정 목록이 즉시 갱신됩니다.

---

## 5. 메인 창 — 단일 Sweep 측정

### 5.1 프로파일 선택

창 상단의 드롭다운에서 사용할 프로파일을 선택합니다.

| 버튼 | 기능 |
|------|------|
| **Add** | 새 프로파일 생성 |
| **Copy** | 현재 프로파일 복제 |
| **Rename** | 이름 변경 |
| **Delete** | 삭제 (확인 대화상자) |

### 5.2 Sweep Parameters 패널

| 항목 | 설명 |
|------|------|
| **Source Value** | 현재 소스 출력값 (읽기 전용, 측정 시작 시 실제 readback) |
| **Sweep To** | 목표값 |
| **Sweep Rate** | 변화 속도 (단위/min) |
| **Time / Point** | 한 스텝당 소요 시간 (초) |
| **Step Size** | 계산된 스텝 크기 = Rate × Time/Point / 60 |
| **Idle** | 마지막 스텝 처리 후 다음 스텝까지 남은 대기 시간 (빨간색 = 과부하) |
| **Remaining** | 남은 예상 시간 |

### 5.3 Sweep Channel 선택

Parameter Manager에서 등록한 Sweep Value들이 라디오 버튼으로 표시됩니다.

- **Time** : 시간 축 sweep (경과 시간 vs 측정값 기록)
- **[등록된 항목들]** : 선택 시 해당 장비의 출력을 소스로 사용

### 5.4 Active Measurements

체크박스로 이번 sweep에서 측정할 항목을 선택합니다.

- **Suffix** : 데이터 열 이름에 추가할 접미사 (같은 항목을 여러 번 쓸 때 구분)
- **Type 콤보** : 측정 분류 (none / contact / temperature / bfield)

### 5.5 Save Settings

| 항목 | 설명 |
|------|------|
| **Main Folder** | 데이터 저장 루트 경로 |
| **Custom Folder** | 추가 하위 폴더 |
| **Custom Word** | 파일명 앞에 붙을 단어 |
| **Include Date** | 체크 시 `YYYYMMDD` 날짜 폴더 및 파일명 포함 |
| **Enable Save** | 체크 해제 시 파일 저장 없이 측정만 진행 |
| **미리보기 레이블** | 실제 저장 경로 실시간 표시 |

파일명 규칙: `{custom_word}{YYYYMMDD}X{순번3자리}.dat`
예) `experiment20250101X001.dat`

**Open Folder** 버튼 : 저장 경로를 파일 탐색기로 열기

### 5.6 Sweep 실행

1. Sweep Channel 선택
2. 측정 항목 체크
3. Sweep To / Rate / Time per Point 입력
4. 저장 경로 설정
5. **Start** 버튼 클릭

실행 중에는:
- 창 테두리가 초록색으로 글로우 애니메이션
- Start 버튼 비활성화, Stop 버튼 활성화
- 모든 설정 UI 잠금 (변경 불가)

**측정 시퀀스 (단일 Sweep)**

```
초기 상태 측정 (현재 위치에서 이동 없이 측정)
  → 스텝 반복:
      현재 위치 확인 → 다음값 계산 → 쓰기 → 측정 → 데이터 저장
        → 타이머 대기 → 다음 스텝
  → 목표 도달 → 자동 종료
```

### 5.7 Derivative 채널

Sweep 중 선택한 측정값의 1차/2차/3차 미분을 실시간으로 계산합니다.

| 설정 | 설명 |
|------|------|
| **Enable** | 미분 채널 활성화 |
| **Numerator** | 분자에 해당하는 측정 열 선택 |
| **Denominator** | 분모에 해당하는 열 선택 (보통 sweep 변수) |
| **Window** | 미분 계산에 사용할 포인트 수 (슬라이딩 윈도우) |
| **Min ΔX** | 이보다 작은 분모 변화는 무시 (노이즈 방지) |
| **Method** | 미분 계산 방식 선택 |
| **Label / Unit** | 결과 열의 이름과 단위 |

---

## 6. Double Sweep

**메뉴 → View → Double Sweep** 또는 메인 창의 **Double Sweep** 버튼

두 번째 채널(자기장, 게이트 전압 등)의 값을 배열로 순회하면서 각 값마다 DUMMY → TRACE → RETRACE를 반복 실행합니다.

### 6.1 실행 흐름

```
array[0] 설정
  → PRE_INIT   : First Channel → Start Point (데이터 없음)
  → DUMMY      : Start Point → Start Point (초기 안정화, 저장됨)
  → TRACE      : Start Point → Stop Point  (저장됨)
  → RETRACE    : Stop Point  → Start Point (또는 0, 저장됨)
array[1] 설정 → 반복...
array 소진 → 완료
```

### 6.2 Sweep Parameters (First Channel)

| 항목 | 설명 |
|------|------|
| **Start Point** | First channel의 시작값 |
| **Stop Point** | TRACE의 목표값 |
| **Rate (trace)** | TRACE 속도 (units/min) |
| **Rate (retrace)** | RETRACE 속도 |
| **Rate (dummy)** | DUMMY 속도 |
| **Time / Point** | 스텝당 시간 (초) |
| **Retrace to 0** | 체크 시 RETRACE가 Start Point 대신 0으로 이동 |

### 6.3 Second Channel Array

| 항목 | 설명 |
|------|------|
| **From** | 배열 시작값 |
| **To** | 배열 끝값 |
| **Step** | 배열 간격 |
| **→ N pts** | 계산된 배열 포인트 수 |

### 6.4 Second Channel 선택

Parameter Manager의 Second Sweep Channel 항목들이 라디오 버튼으로 표시됩니다.
**SWEEP** 타입 채널 선택 시 Sweep Rate, Safety Ramp 옵션이 추가로 표시됩니다.

### 6.5 상태 표시줄 (하단)

| 레이블 | 표시 내용 |
|--------|-----------|
| **Phase** | 현재 단계 (IDLE / PRE_INIT / ADVANCING / DUMMY / TRACE / RETRACE) |
| **Second** | 현재 second channel 값 + 단위 |
| **Array** | 진행 상황 (예: `3/120`) |
| **Alarm** | 마지막 알람 발생 내용 |

### 6.6 데이터 저장 경로

```
{Main Folder}/{Custom Folder}/{phase}/{date}/{custom_word}{date}_{fig_axis}_{값}.dat

예) /data/experiment/trace/20250101/exp20250101_B_1.5000.dat
    /data/experiment/dummy/20250101/exp20250101_B_1.5000.dat
    /data/experiment/retrace/20250101/exp20250101_B_1.5000.dat
```

### 6.7 Estimated Time

Array 크기, Sweep 거리, Rate, Time/Point를 기반으로 전체 소요 시간을 자동 계산하여 표시합니다.

### 6.8 알람 설정

우측 알람 패널에서 측정 중 특정 조건이 충족되면 알람을 발생시킬 수 있습니다.
→ 자세한 내용은 [13. 알람 및 Telegram 알림](#13-알람-및-telegram-알림) 참조

---

## 7. Command Window — 즉석 명령 실행

**메뉴 → View → Command Window**

측정 중이 아닐 때 장비에 명령을 직접 보내 테스트하거나 상태를 확인합니다.

### 7.1 사용법

1. **Instrument** : 명령을 보낼 장비 선택
2. **VISA** : 실행할 명령 선택
   - `[Q]` : 측정 명령 (query)
   - `[S]` : Sweep 값 설정 명령
   - `[W]` : Write 명령
3. 플레이스홀더가 있으면 **Parameters** 칸에 값 입력
4. **Read cmd** : Sweep Value 선택 시 자동 입력됨 (수동 입력도 가능)
5. **Send** 버튼 클릭

결과가 **Output** 창에 표시됩니다.

---

## 8. Graph — 실시간 그래프

**메뉴 → View → Graph** 또는 메인 창의 **Graph** 버튼

### 8.1 패널 추가/제거

- **Add Panel** : 새 그래프 패널 추가
- 각 패널 우측 **×** 버튼으로 삭제

### 8.2 축 설정 (패널별)

| 항목 | 설명 |
|------|------|
| **X Axis** | 가로축으로 사용할 데이터 열 |
| **Y Axis** | 세로축으로 사용할 데이터 열 |
| **Phase** | Double Sweep 전용 — `trace` / `retrace` / `dummy` 중 표시할 단계 선택 |

### 8.3 그래프 조작

| 동작 | 기능 |
|------|------|
| 마우스 우클릭 | 컨텍스트 메뉴 |
| **Hold** | 화면 고정 (측정은 계속 진행) |
| **Linear Regression** | 드래그 영역에 선형 회귀 (기울기/절편/R²) 표시 |
| **Export** | PNG로 저장 |
| 스크롤 | 확대/축소 |
| 드래그 | 이동 (자동으로 Hold 상태로 전환) |

### 8.4 2D Map

Double Sweep 완료 후 폴더의 .dat 파일들을 불러와 2D 컬러맵으로 시각화합니다.

- 컬러맵 선택: Warming / Jet / Viridis / Inferno / Gray / RdBu
- X/Y 축 선택 가능

---

## 9. Data / Timing / Sweep Status 보조 창

### 9.1 Data Window (**메뉴 → View → Data**)

가장 최근 스텝의 측정값을 실시간으로 표시합니다. 데이터가 누적되지 않으며 매 스텝마다 갱신됩니다.

| 열 | 내용 |
|----|------|
| Figure Axis | 측정 항목 이름 |
| Unit | 단위 |
| Value | 최신 측정값 |

### 9.2 Timing Window (**메뉴 → View → Timing**)

각 스텝의 처리 시간을 항목별로 측정하여 성능을 분석합니다.

| 항목 | 설명 |
|------|------|
| Period | 직전 스텝 시작부터 현재 스텝 시작까지 |
| Dispatch → Worker | 신호 전달 지연 |
| Source Read | 첫 스텝의 소스 readback 시간 |
| Write | 장비에 값 쓰기 시간 |
| Measurements | 모든 측정 쿼리 시간 |
| Worker → Main | 결과 신호 전달 지연 |
| UI Update | UI 갱신 시간 |
| **Idle** | 다음 스텝까지 남은 시간 (빨간색 = 스텝이 time_per_point 초과) |

Last / Min / Max / Avg 열이 함께 표시됩니다.

### 9.3 Sweep Status Window (**메뉴 → View → Sweep Status**)

현재 스텝 번호, 현재값, 다음 목표값을 간략하게 표시합니다.

---

## 10. Meta Data Config — 측정 전 메타 기록

**메뉴 → View → Meta Data Config**

측정 시작 직전 장비 상태(온도, 자기장 등)를 JSON 파일로 기록하는 기능입니다.

### 10.1 활성화

**Enable Meta Data** 체크박스로 전체 기능을 켜고 끕니다.

### 10.2 항목 선택

Parameter Manager에서 등록된 측정 항목들이 나열됩니다.
각 항목의 체크박스를 켜면 해당 항목이 메타데이터에 포함됩니다.

### 10.3 자동 통계 (T/B 타입)

Active Measurements에서 `temperature` 또는 `bfield` 타입으로 분류된 항목이 메타데이터에 포함되면 sweep 동안 측정값을 수집하여 **평균(mean)과 표준편차(std)** 를 자동 계산합니다.

### 10.4 저장 위치

데이터 .dat 파일과 같은 이름으로 `.json` 확장자로 저장됩니다.

```json
{
  "timestamp": "2025-01-01T12:00:00",
  "T_mean": { "value": 4.21, "unit": "K" },
  "T_std":  { "value": 0.03, "unit": "K" },
  "B":      { "value": 1.500, "unit": "T" }
}
```

---

## 11. Debug 창 — 로그 및 콘솔

**메뉴 → View → Debug**

### 11.1 VISA Log

장비와 주고받는 모든 VISA 통신을 기록합니다.

- 형식: `[alias @ 주소] ← 명령어` / `[alias @ 주소] → 결과`
- **Verbose** 체크 해제 시 에러만 표시
- **Clear** 버튼으로 로그 삭제

### 11.2 Sweep Log

각 Sweep 스텝의 진행 상황을 기록합니다.

- 측정 성공/실패, 측정값 요약, 에러 메시지 포함
- **Verbose** 체크 시 스텝별 상세 로그 표시

### 11.3 Console

장비에 임의 명령을 직접 실행합니다.

```
사용법: {alias} {명령어}
예시:   2636A print(smua.measure.v())
        ITC V
        help
```

---

## 12. 데이터 파일 형식

### 12.1 파일 구조 (.dat)

```
열이름1    열이름2    열이름3    ...
단위1      단위2      단위3      ...
값1        값2        값3        ...
값1        값2        값3        ...
```

- 탭(`\t`) 구분
- 첫 번째 열: Sweep 채널 (sweep_to 값 또는 elapsed time)
- 이후 열: 활성 측정값들
- Derivative 채널이 켜져 있으면 마지막 열에 추가

### 12.2 저장 경로 규칙

| 시나리오 | 경로 예시 |
|----------|-----------|
| 기본 | `C:/data/X001.dat` |
| Custom Folder | `C:/data/experiment/X001.dat` |
| Include Date | `C:/data/experiment/20250101/X001.dat` |
| Custom Word | `C:/data/experiment/20250101/myexp20250101X001.dat` |
| Double Sweep | `C:/data/exp/trace/20250101/exp20250101_B_1.5.dat` |

---

## 13. 알람 및 Telegram 알림

Double Sweep 창 우측 패널에서 설정합니다.

### 13.1 알람 활성화

**Alarm Enabled** 체크박스로 전체 알람 기능을 켭니다.

### 13.2 고정 트리거

| 항목 | 발동 조건 |
|------|-----------|
| **Communication Error** | VISA 통신 오류 / 타임아웃 발생 시 |
| **Measurement Error** | 측정 채널이 ERR 값을 반환했을 때 |
| **All Complete** | 전체 array 측정이 정상 완료되었을 때 |

### 13.3 측정값 트리거

RETRACE 완료 후 측정값이 조건을 충족하면 알람을 발생시킵니다.

1. 측정 항목 선택 (Parameter Manager에 등록된 항목)
2. 비교 연산자 선택: `>` / `<` / `>=` / `<=` / `==` / `!=`
3. 기준값 입력
4. **+ Add** 버튼으로 추가

각 트리거는 체크박스로 개별 활성/비활성 가능합니다.

### 13.4 알림 수단

| 수단 | 설정 |
|------|------|
| **Sound** | 기본 경보음 |
| **Email** | SMTP 설정 필요 (수신자, 서버, 포트, 계정, 비밀번호) |
| **Telegram** | Bot Token + Chat ID 필요 |

### 13.5 Telegram 설정

1. **Bot Token** 입력 (BotFather에서 발급)
2. **Name** 과 **Chat ID** 입력 후 **Save** 버튼
3. **Test** 버튼으로 메시지 전송 확인
4. Telegram 탭의 **Enable** 체크박스 활성화

---

## 14. Cycle Sweep

**메뉴 → View → Cycle Sweep…** (`Ctrl+Shift+C`)

메인 창의 단일 sweep 은 목표값이 하나뿐이라 `0 V → 30 V` 로 끝납니다. Cycle Sweep 은
목표값을 여러 개 두고 그 묶음(cycle)을 정해진 횟수만큼 반복합니다.

**Sweep channel 은 Keithley 2636A 로 등록된 항목만** 선택할 수 있습니다.
목록이 비어 있으면 Instrument Settings 에서 그 장비의 드라이버가 `Keithley2636A`
인지, Parameter Manager 에 Paired Command 가 등록돼 있는지 확인하세요.

### 14.1 한 cycle 이란

측정을 시작하면 2636A 의 **현재값에서 Initial Value 까지 먼저 이동**한 뒤(이 구간은
기록하지 않습니다), 거기서부터 목표값을 순서대로 훑습니다. 이 한 바퀴가 cycle 입니다.

초기값 `0`, 목표값 `30 / -30 / 0` 이면

```
(현재값) → 0 V   ← 초기값 이동, 데이터 없음
① 0 V → 30 V      ② 30 V → -30 V      ③ -30 V → 0 V      = cycle 1회
```

2회차부터는 직전 cycle 의 마지막 target(위 예에서 0 V)에서 이어지므로 값이 끊기지
않습니다.

### 14.2 목표값 입력 — Cycle Targets

**한 칸에 값 하나씩** 넣고, 위에서 아래 순서로 sweep 합니다.

```
Cycle Targets
  1.  [   30   ] V   [−]
  2.  [  -30   ] V   [−]
  3.  [    0   ] V   [−]
  [ +  target 추가 ]
```

- **`+ target 추가`** — 맨 아래에 빈 칸을 하나 더 만듭니다.
- **`−`** — 그 줄을 지웁니다. 번호는 자동으로 다시 매겨집니다.
- **빈 칸은 무시**합니다. 칸만 만들어 두고 값을 안 넣으면 그 줄은 없는 것으로 칩니다.
- 목록 아래 미리보기 줄에 `(현재값) → 0V(초기값) → 30V → -30V → 0V  [3 구간 / cycle]`
  처럼 실제로 돌아갈 순서가 표시됩니다. 숫자로 읽을 수 없는 값이 있으면 빨간 글씨로
  알려 줍니다.

### 14.3 그 밖의 입력 항목

| 항목 | 설명 |
|------|------|
| **Initial Value** | cycle 을 시작하기 전에 먼저 이동할 값. 이 구간은 저장하지 않습니다 |
| **Sweep Rate** | 이동 속도 (units/min). **초기값 이동·cycle·0 복귀 전부 이 속도** |
| **Time / Point** | 스텝당 시간 (초). 한 스텝 이동량 = rate/60 × Time/Point |
| **Cycles** | 같은 cycle 을 반복할 횟수 |
| **Return to zero** | 체크: 모든 cycle 종료 후 0 까지 복귀 (이 구간도 **저장하지 않음**). 해제: 마지막 target 값에 그대로 정지 |

**Estimated / 종료 예상** — cycle 1 은 초기값에서, 2회차부터는 직전 cycle 의 마지막
target 에서 출발한다고 보고 거리를 더해 계산합니다. 맨 처음 '현재값 → 초기값' 구간은
현재값을 미리 알 수 없어 빠져 있습니다.

### 14.4 측정 항목

측정 항목(Active Measurements)과 미분 채널은 **메인 창 설정을 시작 시점에 그대로
가져다 씁니다.** 그래서 시작 전에 메인 창에서 원하는 measurement 를 체크해
두어야 합니다. (저장 위치와 파일명은 아래 14.5 처럼 이 창에서 직접 정합니다.)

### 14.5 Save Settings — 저장 위치와 파일명

저장 폴더와 파일명은 **이 창에서 직접** 정합니다. 메인 창 설정과 별개이므로,
메인 창 sweep 과 다른 폴더에 따로 모아 둘 수 있습니다.

| 항목 | 설명 |
|------|------|
| **Main Folder** | 저장 루트. `찾아보기…` 버튼으로 고를 수 있습니다 |
| **Sub Folder** | 그 아래 하위 폴더. 비우면 Main Folder 바로 아래에 저장 |
| **File Name** | 파일명 앞부분. 뒤에 cycle 번호가 자동으로 붙습니다 |
| **날짜 포함** | 체크 시 `YYYY-MM-DD` 폴더가 하나 더 생기고 파일명에도 `YYYYMMDD` 가 들어갑니다 |
| **파일로 저장** | 끄면 측정은 하되 파일을 만들지 않습니다 (그래프·Data 창에만 표시) |

**`Main 창 설정 가져오기`** 버튼을 누르면 메인 창의 저장 설정을 한 번에 복사해
옵니다 (Sub Folder 에는 `/cycle` 이 붙습니다). 처음 이 창을 여는 프로파일에서는
이 값이 자동으로 채워집니다.

결과 경로 (아래 미리보기 줄에 실시간으로 표시됩니다):

```
{Main Folder}/{Sub Folder}/[YYYY-MM-DD]/{File Name}{YYYYMMDD}_cycleNNN.dat

예) D:/data/sampleA/cycle/2026-08-10/run1_20260810_cycle001.dat
    D:/data/sampleA/cycle/2026-08-10/run1_20260810_cycle002.dat
```

- **cycle 하나당 파일 하나**입니다. 번호는 뺄 수 없습니다.
- 각 파일의 첫 행은 그 cycle 의 **시작점**(이동 없이 현재 위치에서 한 번 측정)입니다.
- 메타데이터 JSON(`…_cycleNNN.json`)도 cycle 단위로 저장되며, 온도·자기장의
  평균·표준편차도 그 cycle 구간에 대해서만 계산됩니다.
- 같은 File Name 으로 같은 날 다시 돌리면 **같은 이름의 파일을 덮어씁니다.**
  이전 결과를 남기려면 File Name 을 바꾸세요.

그래프는 cycle 을 지우지 않고 겹쳐 그립니다 — hysteresis 를 바로 볼 수 있습니다
(값이 올라가는 구간은 trace 색, 내려가는 구간은 retrace 색).

### 14.6 상태 표시줄

| 레이블 | 표시 내용 |
|--------|-----------|
| **Phase** | `IDLE` / `PRE_INIT`(초기값 이동) / `SWEEPING` / `RETURNING 0` |
| **Cycle** | 진행 상황 (예: `3/10`) |
| **Segment** | 현재 cycle 안의 구간 (예: `2/3`) |
| **Target** | 현재 구간의 목표값 |

### 14.7 중단과 재개

- 통신 오류가 나면 10초 뒤 자동으로 1회 재시도합니다.
- 다시 실패하면 측정을 멈추고 그 시점의 cycle 을 재개 로그에 저장합니다.
- **Resume** 버튼으로 저장된 cycle 부터 다시 시작할 수 있습니다.
  재개 단위는 **cycle** 이며, 그 cycle 의 `.dat` 파일은 새로 씁니다.
  재개할 때도 그 cycle 이 원래 출발했을 위치(cycle 1 은 초기값, 2회차부터는 직전
  cycle 의 마지막 target)로 먼저 이동한 뒤 이어갑니다.
- Stop 으로 직접 멈춰도 같은 방식으로 재개 지점이 남습니다.

메인 창에서 단일 sweep 이 돌고 있으면 Cycle Sweep 은 시작할 수 없고, 반대도
마찬가지입니다 (같은 장비를 두 곳에서 구동하는 사고 방지).

---

## 15. Double Sweep+ (Cycle) — 온도·자기장마다 cycle 반복

**메뉴 → View → Double Sweep+ (Cycle)…** (`Ctrl+Shift+B`)

Cycle Sweep 은 한 온도에서만 돌지만, 실제 실험은 보통 **온도(또는 자기장)를 한 칸씩
바꿔 가며 같은 cycle 을 다시 도는 것**입니다. 이 창이 그 바깥 루프를 대신 돌려 줍니다.

예를 들어 300 K 부터 20 K 씩 내리면서 매 온도에서 `0 → 30 → -30 → 30 → 0 V` cycle 을
돌리고 싶다면, 온도가 실제로 따라올 시간(예: 1시간)을 주도록 설정하면 이렇게 진행됩니다.

```
300 K 설정 → 1시간 대기 → 300 K 에서 cycle
280 K 설정 → 1시간 대기 → 280 K 에서 cycle
260 K 설정 → 1시간 대기 → 260 K 에서 cycle   …
```

### 15.1 진행 순서 (모든 온도/자기장 값에서 동일)

| 단계 | Phase | 하는 일 |
|------|-------|---------|
| ① | `PRE_INIT` | First 채널을 **Initial Value** 로 이동 (기록 안 함) |
| ② | `ADVANCING_SECOND` | Second 채널(온도/자기장)을 다음 값으로 설정 |
| ③ | `SETTLING` | **Settle Wait** 만큼 대기 (남은 시간이 실시간 표시) |
| ④ | `CYCLING` | cycle 1 … N 진행 (cycle 마다 파일 하나) |

①이 ②보다 **먼저**인 이유: 온도·자기장이 변하는 동안 시료에 직전 cycle 의 마지막
전압이 계속 걸려 있으면 안 되기 때문입니다. Initial Value 를 0 V 로 두면 온도가
움직이는 내내 0 V 에 머뭅니다.

### 15.2 ① Second Channel — ITC / IPS 고르기

맨 위 **장비 라디오**로 `ITC (온도)` / `IPS (자기장)` / `전체` 를 고르면 그 장비로
등록된 Second Sweep Channel 만 목록에 남습니다.

목록이 비어 있으면
- Instrument Settings 에서 그 장비의 드라이버가 `OxfordITC` / `OxfordIPS` 인지,
- Parameter Manager 에 **Second Sweep Channel** 로 등록돼 있는지

확인하세요. (`전체` 를 고르면 드라이버와 무관하게 전부 나옵니다.)

**Advance Type**(값을 어떻게 옮길지)은 Parameter Manager 에서 그 채널을 등록할 때
정합니다. 고른 타입에 따라 아래 설정 구획이 나타납니다.

| Advance Type | 동작 | 이 창에서 고치는 값 |
|---|---|---|
| `simple_hop` | 값만 보내고 바로 다음 | — |
| `sweep` | 정해진 속도로 천천히 이동 | Sweep Rate, Safety Ramp |
| `feedback` | 실제 값이 도달·안정될 때까지 폴링 | Read Cmd, Poll, Tolerance, Std Window/Threshold, Noise Floor |
| `wait_for_time` | 값을 보내고 정해진 시간 대기 | Wait Time |
| `threshold_time` | 도달 판정 후 고정 시간 대기 | 위 둘 다 |

### 15.3 ② Array 와 Settle Wait

**From / To / Step** 으로 훑을 값 목록을 만듭니다.
**내려가는 방향이면 Step 을 음수로** 넣으세요 (예: `300 → 100`, Step `-20`).
부호가 반대면 값이 하나만 만들어집니다 — 오른쪽 `→ N pts` 표시로 바로 확인됩니다.

`Array 값 테이블…` 을 누르면 값 목록을 표로 직접 편집할 수 있습니다. **측정 중에도
아직 측정하지 않은(대기) 행은 고치거나 추가·삭제할 수 있습니다.** 직접 만든 목록을
그대로 쓰려면 `테이블 초기화 안 함` 을 켜세요.

| 항목 | 설명 |
|------|------|
| **Settle Wait** | second 를 설정한 뒤 cycle 을 시작하기까지 기다리는 시간 (시/분 입력) |
| **첫 값은 대기 건너뛰기** | 첫 값에서만 대기를 생략합니다. **이미 그 온도/자기장에 도달해 있을 때만** 켜세요 |
| **마지막에 second 를 0 으로** | 모든 값이 끝난 뒤 second 채널을 0 으로 보냅니다 |

> **Settle Wait 는 Advance Type 의 대기와 별개입니다.** `feedback` 으로 도달을 확인한
> 뒤에도 Settle Wait 만큼 **추가로** 기다립니다. 온도계가 가리키는 값과 시료 온도가
> 같아지는 데 걸리는 시간을 따로 주려는 것이므로, 둘 다 쓰는 것이 정상입니다.

측정 중에는 상태 표시줄에 `대기 1h 23m 45s 남음` 처럼 남은 시간이 초 단위로 갱신됩니다.

### 15.4 ③④⑤ First Channel / Targets / Cycle

Cycle Sweep 창과 같은 방식입니다 ([14장](#14-cycle-sweep) 참고).
다만 여기서는 **Sweep Value 로 등록된 항목이면 무엇이든** 고를 수 있습니다.

| 항목 | 설명 |
|------|------|
| **Initial Value** | 매 second 값마다 cycle 시작 전에 먼저 이동할 값 (기록 안 함) |
| **Cycle Targets** | 한 칸에 하나씩, 위에서 아래 순서로. 빈 칸은 무시 |
| **Sweep Rate / Time per Point** | cycle 전체 공통 |
| **Cycles / point** | second 값 **하나당** cycle 반복 횟수 |
| **마지막에 first 를 0 으로** | 모든 측정이 끝난 뒤 first 채널을 0 으로 복귀 (기록 안 함) |

**Estimated / 종료 예상** 은 이동 거리와 Settle Wait 를 더해 계산합니다. second 채널이
실제로 온도·자기장에 도달하는 데 걸리는 시간은 장비에 달려 있어 **빠져 있습니다** —
실제 소요는 이보다 깁니다.

### 15.5 ⑥ 저장

저장 폴더·파일명은 **이 창에서 직접** 정합니다 (메인 창 설정과 별개).
`Main 창 설정 가져오기` 로 한 번에 복사할 수 있습니다.

**second 값 하나 × cycle 하나마다 `.dat` 파일이 하나**씩 생깁니다.

```
{Main Folder}/{Sub Folder}/[YYYY-MM-DD]/{File Name}{YYYYMMDD}_{axis}_{2nd값}_cycleNNN.dat
예: D:\data\sampleA\cycle2d\2026-08-27\sampleA20260827_T_280_cycle002.dat
```

창 아래 미리보기 줄이 실제로 만들어질 경로입니다. 같은 이름·같은 날짜로 다시 돌리면
**기존 파일을 덮어씁니다** — File Name 을 바꾸세요.

메타데이터 JSON 에는 second 채널 정보(값·advance type·Settle Wait)와 cycle 정보가
함께 들어갑니다. 그래프는 second 값이 바뀔 때마다 새로 시작하고, 같은 값의 cycle 들은
겹쳐 그려 hysteresis 를 바로 볼 수 있습니다.

### 15.6 상태 표시줄

| 레이블 | 표시 내용 |
|--------|-----------|
| **Phase** | `IDLE` / `PRE_INIT` / `ADVANCING_SECOND` / `SETTLING` / `CYCLING` / `RETURNING_…` |
| **Second** | 현재 온도/자기장 목표값 |
| **Array** | 몇 번째 값인지 (예: `3/11`) |
| **Cycle** | 그 값에서의 cycle 진행 (예: `2/5`) |
| **Seg** | 현재 cycle 안의 구간 (예: `2/4`) |
| (맨 오른쪽) | 대기 중이면 남은 시간 |

### 15.7 중단과 재개

- 통신 오류가 나면 10초 뒤 자동으로 1회 재시도합니다. 다시 실패하면 멈추고 그 시점의
  **array 값**을 재개 로그에 저장합니다.
- second 채널이 목표에 도달하지 못하는 워치독 타임아웃은 **재시도 없이 즉시 중단**합니다
  (장비가 명령을 놓쳤을 가능성이 커서, 계속 기다리면 시간만 버립니다).
- **Resume** 로 저장된 지점부터 다시 시작합니다. **재개 단위는 array 값**이라,
  그 값의 second 설정·Settle Wait·cycle 전부를 처음부터 다시 하고 cycle 파일도 새로
  씁니다. (중단 시점의 실제 온도·전압을 알 수 없어 cycle 중간부터 이어붙이면 데이터가
  어긋나기 때문입니다.)
- Stop 으로 직접 멈춰도 같은 방식으로 재개 지점이 남습니다.

메인 창에서 단일 sweep 이 돌고 있으면 이 창은 시작할 수 없고, 반대도 마찬가지입니다.

---

## 16. VNA Control — Power Sweep 모드

**VNA Control 창 → ◆ Double Sweep 구획 → `Power Sweep 모드 (First 축을 power 로 대체)`**

특정 자기장 값에 **자기장을 고정한 채 VNA power 를 한 점씩 바꾸며 매 점마다 측정**하고,
끝나면 다음 자기장 값으로 넘어가 power 를 처음부터 다시 훑습니다.

> ### ⚠️ 먼저 할 일 — power 명령 등록
>
> **프로그램은 power 명령을 내장하고 있지 않습니다.** `power ch` 에서 고른 명령을
> 그대로 보낼 뿐이고, 그 목록은 ⚙ Config 의 Sweep 명령 = VISA 라이브러리에서 옵니다.
>
> `power ch` 콤보에는 **등록된 sweep 명령이 전부** 나옵니다. 주파수나 게이팅 명령을
> 잘못 골라도 측정은 그대로 돌고 파일도 정상적으로 쌓입니다 — power 만 안 바뀔 뿐입니다.
> 그래서 콤보 아래에 **첫 점에서 실제로 나갈 VISA 문자열**을 그대로 보여 줍니다.
> 시작 전에 반드시 확인하세요.
>
> ```
> ↳ 첫 점에 보낼 명령:  [VNA] :SOUR1:POW -20        ← 이런 게 나와야 정상
> ↳ 첫 점에 보낼 명령:  [VNA] :CALC1:FILT:TIME:STAR -20   ← 게이팅 명령. power 안 바뀜
> ```
>
> **등록 방법** — Settings → VISA Library → VNA 에 Write Command 추가:
>
> | 필드 | 값 (Keysight ENA 계열 예시) |
> |---|---|
> | Description | `VNA_set_power` |
> | VISA Command | `:SOUR{ch}:POW {p}` |
> | Figure Axis | `power` · Unit `dBm` |
>
> 그다음 VNA Control 의 ⚙ Config 에서 이 명령을 Sweep 명령에 추가하고, `ch` 는 고정값
> (보통 1), `p` 는 `[parameter]`(user input)로 지정하면 `power ch` 콤보에 나타납니다.
>
> 명령 문법은 **장비 모델마다 다릅니다.** 반드시 쓰시는 VNA 의 프로그래밍 매뉴얼로
> 확인하세요.

### 16.1 실제 진행 순서

```
[자기장 점마다 반복]
  ① 자기장 변화 속도 전송            (입력한 T/min)
  ② 자기장 목표값 전송
  ③ ramp 시작 트리거                 (등록했을 때만 — 예: iPS 의 RTOS)
  ④ 실제로 도달할 때까지 폴링         (Second 채널의 Read Cmd 로 확인)
  ⑤ 도달 후 대기                      (기본 60초)
      [power 점마다 반복]
        power 이동 → 대기(기본 5초) → 측정 → 대기(기본 5초)
```

예: power `-20 → 0` N=11, 자기장 `0 → 1 T` N=5, 속도 0.3 T/min 이면

```
자기장 0 T 도달 → 60초 대기 → power -20 → 5초 → 측정 → 5초 → -18 → 5초 → 측정 …  (11점)
자기장 0.25 T 도달 → 60초 대기 → power -20 부터 다시 11점  …
… 1 T 까지 끝나면 측정 완료 (총 55점)
```

### 16.2 입력 항목

| 항목 | 설명 |
|------|------|
| **power ch** | power 를 설정하는 sweep 명령 (⚙ Config 의 Sweep 명령 목록에서 선택) |
| **Start / Stop / N** | power 범위와 **처음·끝을 포함한** 점 개수 (`N=2` 면 처음·끝 두 점) |
| **이동 후 측정까지** | power 를 옮긴 뒤 측정을 시작하기까지 (기본 5초) |
| **측정 후 다음 점까지** | 측정이 끝난 뒤 다음 power 로 넘어가기까지 (기본 5초) |
| **변화 속도** | 다음 자기장 점으로 넘어갈 때의 속도. **Mercury iPS 는 최대 0.3 T/min** — 넘으면 옆에 경고가 뜹니다 |
| **속도 명령** | 위 속도 값을 장비로 보내는 명령. **비워 두면 Second 채널의 `Advance 전 명령`을 그대로 씁니다** (보통 여기 이미 등록돼 있습니다) |
| **ramp 시작 명령** | 목표를 보낸 뒤 실제 ramp 를 시작시키는 트리거. 목표 설정만으로 움직이는 장비면 비워 두세요 |
| **도달 후 대기** | 자기장이 목표에 도달한 뒤 power sweep 을 시작하기까지 (기본 60초) |
| **Second sweep channel** | 바깥 축 — 자기장. 기존대로 ch·Start·Stop·N 을 정합니다 |

**자기장 도달 판정**은 Second 채널의 **Controlled Sweep** 설정(Read Cmd·Tolerance·
Noise Floor·Poll Interval)을 그대로 씁니다. ⚙ Config 에서 그 명령을 Controlled Sweep
(`feedback`)으로 등록하고 Read Cmd(예: `READ:DEV:GRPZ:PSU:SIG:FLD`)를 넣어 두세요.

시작 버튼을 누를 때 아래 항목을 자동으로 점검하고, 걸리는 게 있으면 목록으로 보여 준 뒤
계속할지 물어봅니다.

- 자기장 채널에 도달 확인용 Read Cmd 가 있는지 (없으면 **ramp 중에 측정이 시작됩니다**)
- 속도 명령이 있는지 (없으면 입력한 속도가 장비로 전송되지 않습니다)
- 변화 속도가 0.3 T/min 을 넘지 않는지

### 16.3 주의

- 이 모드를 켜면 **`First sweep ch` 콤보의 Start / Stop / N 은 쓰이지 않습니다**
  (입력칸이 잠깁니다). 콤보 자체는 `⏱ Time` 으로 전환하는 통로라 잠그지 않습니다.
- power 는 **항상 Start→Stop 방향**으로만 훑습니다. `First 방향` 을 다중방향으로
  두어도 무시하고, 자기장 점이 바뀔 때마다 power 는 Start 부터 다시 시작합니다.
- **`Double Sweep with Time` 과 동시에 켤 수 없습니다.** 둘 다 First 축을 대체하기
  때문이며, Power 를 켜면 field-time 이 자동으로 꺼집니다.
- `Advance 전 명령 값 (pre-advance)` 구획은 이 모드에서 **전송되지 않습니다.** 그건
  First 축이 자기장일 때 쓰는 것이고, 여기서는 First 가 power 이기 때문입니다.
  자기장 속도는 위 `속도 명령` 이 담당합니다.
- 모든 대기는 **Stop 을 누르면 즉시 빠져나옵니다** — 60초 대기 중이라도 붙잡히지 않습니다.

### 16.4 측정이 끝나면 어디에 멈추나

**아무것도 체크하지 않으면 마지막으로 쓴 값에 그대로 멈춥니다.**

| 축 | 체크 안 했을 때 멈추는 곳 |
|---|---|
| power | 단방향이면 **Stop 값**. 다중방향이면 자기장 점 개수가 홀수→Stop, 짝수→Start |
| 자기장 | **마지막 array 값**(second Stop) |

되돌리려면 **`⏎ 측정 완료 후 복귀`** 에서 원하는 축을 체크하고 값을 넣으세요.

```
⏎ 측정 완료 후 복귀 (선택):
  ☑ First  →  [ -30 ]      ☑ Second  →  [ 0 ]
```

- **First 를 먼저** 내리고 그다음 Second 를 옮깁니다 — 시료에 신호를 걸어 둔 채
  마그넷을 움직이지 않기 위해서입니다. (Power Sweep 모드에서 First = power)
- Second 는 그 채널의 advance 방식을 그대로 씁니다 — `feedback` 이면 **실제 도달까지
  기다립니다**.
- **Stop·오류로 끊긴 경우에는 복귀하지 않습니다.** 그때는 `⏹ Stop 시 실행 명령`
  (예: iPS 의 HOLD)이 대신 나갑니다. 복귀 도중 Stop 을 눌러도 남은 축은 건드리지 않습니다.
- 복귀에만 실패하면 경고창만 뜨고 측정 데이터는 그대로 정상 완료로 남습니다
  (측정이 '중단됨'으로 바뀌거나 Resume 대상이 되지 않습니다).

> ⚠️ 정상 완료 시에는 `⏹ Stop 시 실행 명령`이 **나가지 않습니다.** 측정이 끝난 뒤
> iPS 에 HOLD 를 걸고 싶다면 위 복귀 옵션으로 자기장을 원하는 값에 보내거나,
> Stop 을 눌러 명시적으로 끊으세요.

### 16.5 자기장 포인트 사이 이동 속도

Power Sweep 모드는 자기장 이동 방식을 따로 갖지 않습니다. **⚙ Config 에서 그
자기장 명령을 어떻게 등록했는지**가 그대로 적용됩니다.

| Config 의 그 명령 | 이동 방식 | 속도를 정하는 것 |
|---|---|---|
| 일반 (기본) / Controlled+`simple_hop` | 값만 write, **도달 대기 없음** | 장비 내부 ramp rate. 프로그램은 곧바로 측정 시작 ⚠️ |
| Controlled + `sweep` | 소프트웨어가 잘게 쪼개 write | `sweep_rate` (units/min), 스텝 간격 0.1초 |
| Controlled + `feedback` | write 후 도달·안정까지 폴링 | **장비 내부 ramp rate** (프로그램은 대기만) |
| Controlled + `wait_for_time` / `threshold_time` | write 후 시간 대기 | 장비 내부 |

> 자기장을 **일반**으로 등록해 두면 램프가 끝나기 전에 power 측정이 시작됩니다.
> 마그넷은 ⚙ Config 에서 **Controlled Sweep**(보통 `feedback`)으로 등록하세요.
> 램프 속도 자체(iPS 의 RFST 등)는 장비 설정이며, 프로그램에서 바꾸려면 Config 의
> `Advance 전 명령` 을 써야 합니다.

### 16.6 저장

기존 double sweep 규칙 그대로 **자기장 값이 하위폴더, power 값이 파일명**입니다.

```
{filename}_001/{자기장축}_300/{power축}_-20.dat
```

---

## 부록 A — 드라이버 목록

모두 `pythonization/instruments/drivers/` 안에 있으며, 모듈 이름은 `vendor_model.py`
규칙을 따릅니다.

| 드라이버 | 클래스 | 적합 장비 |
|----------|--------|-----------|
| `keithley_2636a.py` | `Keithley2636A` | Keithley 2636A SourceMeter |
| `generic_scpi.py` | `GenericSCPIInstrument` | SCPI 표준 장비 전반 (ITC, IPS, E5071 등) |
| `lakeshore_m81.py` | `M81Instrument` | M81, VNA, SIGGEN 등 LAN Raw Socket 장비 |
| `oxford_itc.py` | `OxfordITC` | Oxford Instruments ITC (비표준 CR 종단) |
| `oxford_ips.py` | `OxfordIPS` | Oxford Instruments IPS 마그넷 전원 |
| `srs_sr830.py` | `SR830` | SRS SR830 lock-in |
| `zurich_mfli.py` | `ZurichMFLI` | Zurich MFLI (LabOne Data Server 경유, 非VISA) |
| `dummy.py` | `DummyInstrument` | 하드웨어 없이 테스트용 |

> 이전 버전에서 등록한 장비는 설정 파일에 옛 경로(`driver.m81.M81Instrument` 등)가
> 저장돼 있지만 그대로 동작합니다. Instrument Settings 에서 한 번 저장하면 새 경로로
> 갱신됩니다.

### 커스텀 드라이버 추가

1. `pythonization/instruments/drivers/` 에 `vendor_model.py` 생성
2. `from pythonization.instruments.base import BaseInstrument` 임포트
3. `BaseInstrument` 상속 클래스 작성
4. Instrument Settings에서 드라이버 드롭다운에 자동으로 표시됨

---

## 부록 B — 설정 파일 위치

기본 폴더는 `%USERPROFILE%\Documents\pythonization\settings\` 이며,
**Settings → Config** 의 `data_dir` 로 바꿀 수 있습니다(재시작 후 적용).

| 파일 | 내용 |
|------|------|
| `logs/app.log` | 실행 로그 + 오류 트레이스백 — **문제 신고 시 이 파일** |
| `logs/fault.log` | 프로그램이 갑자기 사라졌을 때의 크래시 덤프 |
| `instruments.yaml` | 등록된 장비 목록 및 연결 설정 |
| `visa_libraries.yaml` | VISA 라이브러리 (측정/Sweep/Write 명령) |
| `profiles/` | 측정 프로파일 (Parameter Manager 설정) |
| `profiles/double_sweep.yaml` | Double Sweep 파라미터 |
| `meta_data_config.yaml` | Meta Data 설정 |
| `resume_points.json` | 중단된 측정의 재개 지점 |

프로그램 폴더의 `app_config.yaml` 에는 측정 임계값·병렬 측정 여부와 알람 전송 수단
(SMTP·텔레그램)이 들어갑니다. **비밀번호와 토큰이 담기므로 남에게 그대로 보내지 마세요.**

---

## 부록 C — 단축키

| 키 | 동작 |
|----|------|
| `Escape` | 현재 보조 창 닫기 (숨김) |
| `Enter` | Debug 콘솔에서 명령 전송 |
| `Ctrl+Shift+C` | Cycle Sweep 창 열기 |
| `Ctrl+Shift+B` | Double Sweep+ (Cycle) 창 열기 |
