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
| **Device Type (Driver)** | 장비 드라이버 클래스 (`driver/` 폴더에서 자동 검색됨) |
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

**Save & Apply** 버튼을 누르면 `settings/instruments.yaml`에 저장되고 즉시 시스템에 반영됩니다.

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

## 부록 A — 드라이버 목록

| 드라이버 | 클래스 | 적합 장비 |
|----------|--------|-----------|
| `driver/keithley_2636a.py` | `Keithley2636A` | Keithley 2636A SourceMeter |
| `driver/generic_scpi.py` | `GenericSCPIInstrument` | SCPI 표준 장비 전반 (ITC, IPS, E5071 등) |
| `driver/m81.py` | `M81Instrument` | M81, VNA, SR830, SIGGEN 등 LAN 장비 |
| `driver/oxford_itc.py` | `OxfordITC` | Oxford Instruments ITC (비표준 CR 종단) |
| `driver/dummy_instrument.py` | `DummyInstrument` | 하드웨어 없이 테스트용 |

### 커스텀 드라이버 추가

1. `driver/` 폴더에 `.py` 파일 생성
2. `from core.instrument_base import BaseInstrument` 임포트
3. `BaseInstrument` 상속 클래스 작성
4. Instrument Settings에서 드라이버 드롭다운에 자동으로 표시됨

---

## 부록 B — 설정 파일 위치

| 파일 | 내용 |
|------|------|
| `settings/instruments.yaml` | 등록된 장비 목록 및 연결 설정 |
| `settings/visa_libraries.yaml` | VISA 라이브러리 (측정/Sweep/Write 명령) |
| `settings/profiles/` | 측정 프로파일 (Parameter Manager 설정) |
| `settings/profiles/double_sweep.yaml` | Double Sweep 파라미터 |
| `settings/meta_data_config.yaml` | Meta Data 설정 |

---

## 부록 C — 단축키

| 키 | 동작 |
|----|------|
| `Escape` | 현재 보조 창 닫기 (숨김) |
| `Enter` | Debug 콘솔에서 명령 전송 |
