# Patch Notes

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
