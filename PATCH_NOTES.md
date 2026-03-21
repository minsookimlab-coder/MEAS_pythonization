# Patch Notes

---

## v1.04.0 — 2026-03-21

### 디버그 창 개편

- **VISA Log "Show" 토글** 추가 — 체크 시에만 VISA 명령어가 표시됨. 기본 Off 상태로 측정 중 불필요한 렌더링 부하 없음
- **Sweep Log 패널** 추가 — 스윕 진행 중 단계별 상태 및 오류를 별도 패널에 표시
  - **Verbose 토글**: 체크 시 매 스텝 측정값 요약도 함께 표시
  - 오류·경고는 Verbose 여부와 무관하게 항상 표시
- 각 패널에 **Clear 버튼** 추가

---

### 측정 오류 처리

- 측정 채널이 하나라도 ERR을 반환하면 **즉시 스윕 중단 + 오류 창 표시**
  - **Single Sweep**: 무조건 중단
  - **Double Sweep**: 중단 + 알람 패널에 **"측정값 ERR" 트리거** 추가 (체크 시 사운드/이메일 알람 동시 발동)

---

### Sweep Parameters UI 개편

- **소숫점 자동 입력 제거** — 숫자 입력창에 자동으로 `.0000`이 붙지 않음. 소숫점은 직접 입력
- **입력창 크기** 약 2배로 확대
- **단위 표시 분리** — 숫자창 오른쪽에 단위 레이블이 별도로 표시됨
- **Sweep Rate 단위 자동 반영** — 현재 선택된 Sweep Channel의 단위로 즉각 업데이트 (예: `T/min`, `V/min`)
- **Start / Stop 버튼 위치 변경** — Sweep Parameters 바로 아래로 이동 (Graph / Double Sweep / Connection Test 버튼 위)

---

### Parameter Manager 개편

- **Alarm / Meta Data 항목 분류 패널** 통합 — 우측 패널에서 각 계층에 넣을 measurement 항목을 일관된 Add/Delete 인터페이스로 관리
- **프로파일 저장 보강** — active measurement의 체크박스 상태, 타입(none/contact/temperature/bfield), axis suffix가 프로파일에 저장되고 복원됨

---

## v1.03.0 — 2026-03-21

### Meta Data Config 기능

Sweep 종료 시(Single) 또는 각 Second Channel Step 완료 시(Double) 측정 조건을 JSON 파일로 저장하는 기능 추가.

**저장 시점**
- Single Sweep: 스윕 정상 완료 후 1회
- Double Sweep: RETRACE 완료 → 알람 체크 후, 다음 step으로 넘어가기 전 (step 수만큼 생성)

**파일명**
- .dat 파일과 동일한 경로·이름, 확장자만 `.json`
- Single: `run20260321X001.json`
- Double: `run20260321_B_1.500.json` (trace 파일 기준)

**JSON 구조**
```json
{
  "timestamp": "2026-03-21T12:34:56",
  "T_itc":  { "value": 4.2,    "unit": "K" },   ← VISA 쿼리 항목
  "B_ips":  { "value": 1.5,    "unit": "T" },
  "T_mean": { "value": 4.18,   "unit": "K" },   ← temperature class 자동
  "T_std":  { "value": 0.03,   "unit": "K" },
  "B_mean": { "value": 1.501,  "unit": "T" },   ← bfield class 자동
  "B_std":  { "value": 0.001,  "unit": "T" }
}
```

**VISA 쿼리 항목 (수동 설정)**
- View → Meta Data Config... 에서 설정
- Parameter Manager에 등록된 measurement 항목을 체크박스로 선택
- 선택된 항목만 스윕 종료 시 기기에 쿼리
- 우측 미리보기 패널에서 저장될 JSON 형태를 실시간 확인 (값은 `---` 표시)

**T/B class 자동 기록**
- MeasType.TEMPERATURE / BFIELD가 부여된 active measurement는 추가 설정 없이 자동 포함
- 스윕 전 phase (dummy+trace+retrace) 에 걸쳐 측정된 모든 값의 평균(mean)과 표준편차(std) 기록

**On/Off**
- Meta Data Config 창의 "Enable Meta Data" 체크박스로 전체 활성/비활성
- 개별 항목 체크박스로 VISA 쿼리 항목별 활성/비활성 가능

**설정 저장**: `settings/meta_data.yaml`

**신규 파일**
- `core/meta_data_manager.py`: T/B 버퍼 관리, VISA 쿼리, JSON 저장
- `gui/meta_data_window.py`: 설정 UI (체크박스 목록 + JSON 미리보기)

**변경 파일**
- `config/config_models.py`: `MetaDataEntry`, `MetaDataConfig` 추가; `FullProfile.meta_data` 필드 추가
- `core/data_saver.py`: `get_filepath()` 추가
- `core/parameter_manager_registry.py`: `meta_data.yaml` 로드/저장 추가
- `gui/main_window.py`: `_meta_manager` 인스턴스 + View 메뉴 "Meta Data Config..." + sweep 흐름 통합
- `gui/double_sweep_window.py`: `_trace_filepath` 추적 + step당 메타 데이터 저장 + T/B 버퍼 clear

---

## v1.02.0 — 2026-03-21

### 측정 타입 시스템 (Measurement Type)

각 active measurement 항목에 **type 선택 콤보박스** 추가 (suffix 왼쪽).

| Type | 동작 |
|---|---|
| `none` | 기존 방식: 컬럼명 = `{figure_axis}_{suffix}` |
| `contact` | 컬럼명 = `contact_{suffix}` (figure_axis 완전 대체) |
| `temperature` | class 부여, 컬럼명은 기존 방식 유지 |
| `bfield` | class 부여, 컬럼명은 기존 방식 유지 |

**변경 파일**: `config/config_models.py` (`MeasType` enum + `InstantiatedMeasurement.meas_type`), `gui/main_window.py`

---

### Double Sweep 파일명 규칙 변경

Double Sweep 실행 중 각 phase(.dat) 파일의 넘버링(`X001`) 대신 **second channel의 현재 step 값**으로 대체됨.

- 기존: `{prefix}{YYYYMMDD}X001.dat`
- 변경: `{prefix}{YYYYMMDD}_{fig_axis}_{step_val}.dat`
  - 예: `run20260321_B_1.500.dat`

**변경 파일**: `core/data_saver.py` (`set_fixed_name()`), `gui/double_sweep_window.py`

---

### 2D Map Plot 기능

Graph Window 우측에 **2D colour-map 패널** 추가.

**UI 구성 (Graph Window)**
```
┌─ Graph ─────────────────────────────────────────────────────┐
│ [＋ Add Graph] [Clear] [2D Map ●] [Save Image]              │
├──────────────────────────┬──────────────────────────────────┤
│ 좌: XY 플롯 패널 (스크롤) │ 우: 2D Map 패널 (토글)           │
│                          │  Base: [경로] […]               │
│                          │  Phase: [trace ▼] Date: [▼] [↻]│
│                          │  X: [▼]  Y: [▼]  Z: [▼]        │
│                          │  Z min: [  ] max: [  ] [Auto Z] │
│                          │  Colormap: [Warming ▼]          │
│                          │  [Plot] [Save Image…]           │
│                          │  ──────────────────────────────  │
│                          │  pyqtgraph 2D ImageItem         │
└──────────────────────────┴──────────────────────────────────┘
```

**동작 방식**
- Double Sweep 시작 시 2D Map 패널의 Base 폴더 자동 설정
- Phase 콤보박스: `trace` / `retrace` / `dummy` 자동 탐지
- Date 콤보박스: 선택된 phase 내 날짜 서브폴더 목록 (최신순)
- **수동 "Plot" 버튼**: 배경 스레드(`QThread`)에서 .dat 파일 로드 후 렌더링
- X/Y/Z 모두 파일 컬럼에서 선택 (Y는 파일 내 상수값 기준으로 행 구분)

**컬러맵 선택**
| 이름 | 설명 |
|---|---|
| Warming | Origin 스타일: 파랑→흰→빨강 (저값=파랑, 고값=빨강) |
| Jet | 파랑→시안→녹→노랑→빨강 |
| Viridis | 보라→청록→노랑 |
| Inferno | 검정→보라→빨강→노랑 |
| Gray | 흑백 |
| RdBu | 발산형: 빨강→흰→파랑 |

**변경/신규 파일**: `gui/graph_window.py` (`MapPanel`, `_MapLoadWorker`, colormaps 추가, GraphWindow 좌/우 splitter 분리)

---

### Double Sweep Window 개선

- **Trace 폴더 경로 프리뷰**: Start/Stop 버튼 아래에 실제 저장 경로(trace 폴더 기준, step 값 template 포함) 표시
- **Open Folder (📂)**: 프리뷰 경로의 상위 폴더를 파일 탐색기로 열기
- **DataWindow 갱신 수정**: Double Sweep 도중 main DataWindow / TimingWindow가 갱신되지 않던 문제 수정

---

### Save Settings 개선

- **Open Folder 버튼**: 저장 경로 프리뷰 옆에 추가. 실제 저장 디렉토리를 파일 탐색기로 열기 (폴더 미생성 시 상위 폴더로 이동)

**변경 파일**: `gui/main_window.py`

---

## v1.01.0 — 2026-03-20

### 알람 기능 (Double Sweep 전용)

**개요**
Double Sweep에서 특정 조건 발생 시 사운드 Beep 및/또는 이메일을 전송하는 알람 기능 추가.
알람 UI는 Double Sweep 창 우측 패널로 통합.

**트리거 조건**
- 측정값 조건: active measurement 중 하나를 선택해 `>`, `<`, `>=`, `<=`, `==`, `!=` + 임계값 비교
- 통신 오류 / Timeout: VISA 오류 또는 타임아웃으로 측정이 중단될 때

**트리거 체크 시점**
RETRACE 완료 후 다음 array step으로 넘어가기 직전 (second source channel 이동 전).
통신 오류는 발생 즉시.

**알람 발동 방법**
- 사운드: `winsound.Beep` 3회 (880Hz → 1100Hz → 880Hz), 비 Windows는 콘솔 벨 시도
- 이메일: `smtplib` STARTTLS, daemon thread에서 비동기 전송 (UI 블로킹 없음)

**UI (Double Sweep 우측 패널)**
```
┌─ 알람 패널 ──────────────────────┐
│ [✓] Alarm 활성화                 │
│ ── 알림 방법 ─────────────────── │
│ [✓] 사운드 (Beep)                │
│ [ ] 이메일 전송                   │
│     수신: [________________]     │
│     SMTP/Port/User/Pass...       │
│ ── 트리거 ────────────────────── │
│ [ ] 통신 오류 / Timeout           │
│ 측정값 조건:                      │
│ [Meas ▼][> ▼][thresh____] [+]   │
│ ┌──────────────────────────┐    │
│ │[✓] smua_I  >  0.001     │    │
│ └──────────────────────────┘    │
│ ── 마지막 알람 ───────────────── │
│ smua_I > 0.001 (actual: 0.0023) │
└──────────────────────────────────┘
```

**신규 파일**
- `core/alarm_manager.py`: 조건 평가 + 사운드/이메일 발동

**변경 파일**
- `config/config_models.py`: `AlarmOperator`, `AlarmTrigger`, `AlarmConfig` 추가; `DoubleSweepConfig.alarm` 필드 추가
- `gui/double_sweep_window.py`: `AlarmPanel` 클래스 추가, 창 레이아웃 좌/우 2열 재구성 (720×600), 알람 체크 통합

---

## v1.0.0 — 2026-03-20

초기 릴리즈. 실험실 계측기 제어·측정·시각화를 위한 GUI 애플리케이션.

---

### 핵심 측정 기능

**Single Sweep**
- Sweep channel: sweep value, time(카운터) 선택 가능
- Start / Stop point, Rate (단위/분), Time per point 설정
- Safety Ramp: N 스텝 분할 이동 (interval = 0 시 배치 전송으로 최적화)
- 측정 시작 전 전체 기기 자동 Connection Test (*IDN?)

**Measurement 채널**
- 복수 측정 채널 체크박스로 ON/OFF
- TSP 기기: 여러 `print()` 명령어를 단일 쿼리로 배치 전송
- 실시간 측정값 표시 (DataWindow)

**Derivative Channel**
- 두 채널의 비 dA1/dA2 실시간 계산
- 슬라이딩 윈도우 (3~50 포인트), 선형 회귀 / Savitzky-Golay 선택

**Double Sweep**
- 두 파라미터를 조합한 중첩 스윕
- Phase 순서: PRE_INIT → ADVANCING_SECOND → DUMMY → TRACE → RETRACE → (반복)
- Second channel 이동 전략: Simple Hop / Sweep / Feedback / Wait for Time
- 각 phase별 별도 폴더에 데이터 저장 (`dummy/`, `trace/`, `retrace/`)

---

### 데이터 저장

- 형식: `.dat` (탭 구분, 헤더 포함)
- 경로: `main_folder / custom_folder / YYYY-MM-DD / {word}{date}X{N:03d}.dat`
- 날짜 폴더 자동 생성, 파일 번호 자동 증가
- Auto-save 체크박스로 ON/OFF

---

### VISA 라이브러리 시스템

**VISA Library (visa_libraries.yaml)**
- 기기별 VISA 명령어 템플릿 저장
- Measurement: `cmd_query`, figure_axis, unit
- Sweep Value: `cmd_set {v}`, `paired_read_cmd`, figure_axis, unit
- Write Command: `cmd_set {v}`, figure_axis, unit
- `{p}` placeholder: 라이브러리 수준의 파라미터 (기기 모듈 등)

**Parameter Manager**
- 라이브러리 항목을 Add/Edit/Delete/Copy/Up/Down으로 인스턴스화
- `{p}` placeholder를 고정값 또는 `[SWEEP]`으로 채움
- figure_axis, description에 `{p}` 자동 치환 (중복 항목 구별)
- Safety Ramp: Sweep Value에 steps / interval 설정
- Second Sweep Channel: advance_type 설정 (SWEEP/FEEDBACK 시 sweep_rate 등 추가 설정)
- Apply 시 main UI 체크박스 상태 유지

---

### 프로파일 시스템

- 명명된 프로파일: `settings/profiles/{name}.yaml`
- 프로파일당 저장 내용: MainUIProfile + DoubleSweepConfig + Save Settings + Sweep Params
- 프로파일 Add / Copy / Rename / Delete
- 프로파일 전환 시 UI 전체 복원

---

### 그래프

- `pyqtgraph` 기반 실시간 렌더링 (~30 fps, dirty flag)
- 패널 추가/제거 가능, X/Y 축 자유 선택
- Phase 구분 선 스타일 (trace / retrace / dummy)
- 이미지 저장: 우클릭 메뉴 또는 Ctrl+S
- 스윕 도중 GraphWindow 열어도 전체 기록 유지

---

### 기기 지원

| 드라이버 | 프로토콜 | 비고 |
|---|---|---|
| `Keithley2636A` | TSP | SourceMeter, 배치 쿼리 지원 |
| `GenericSCPI` | SCPI | 범용 Keysight / R&S 등 |
| `OxfordITC` | 비SCPI | 온도 컨트롤러, ISOBUS |
| `M81` | LAN Raw | 템플릿 |
| `DummyInstrument` | — | 테스트용 Mock |

- MAC 기반 DHCP 자동 IP 추적 (instruments.yaml)
- `InstrumentSettingsUI`에서 기기 추가/제거/테스트

---

### UI / UX

- 메인 창: 좌(제어 패널) + 우(Sweep Channel + Measurements) 2열 레이아웃
- Quick Access 버튼: Graph / Double Sweep / Connection Test
- 모든 서브 창: ESC → 설정 저장 후 닫기, Ctrl+S → 저장
- Parameter Manager AddEntryDialog: Enter → OK
- 메인 Ctrl+S → 전체 프로파일 저장
- 메인 종료 시 모든 서브 창 자동 종료

---

### 빌드

- PyInstaller 6.x, 단일 폴더(`dist/Pythonization/`)
- 콘솔 없음 (windowed)
- 버전 정보 EXE 속성에 기록 (1.0.0.0)

---
