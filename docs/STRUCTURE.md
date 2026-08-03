# Pythonization — 시스템 구조

> Last updated: 2026-08-03 | Version: 1.8.0

사용법은 [USER_MANUAL.md](USER_MANUAL.md), 변경 이력은 [CHANGELOG.md](CHANGELOG.md).

---

## 디렉토리 레이아웃

```
pythonization/                      (저장소 루트)
├── main.py                         실행 진입점 (얇은 셸 — 실제 순서는 app/bootstrap.py)
├── pyproject.toml                  패키지 메타 + 의존성
├── requirements.txt                필수 패키지
├── requirements-optional.txt       선택 패키지 (zhinst / scipy)
├── build_exe.spec                  PyInstaller 빌드 설정
├── app_config.example.yaml         전역 설정 템플릿 (실제 파일은 .gitignore 대상)
├── docs/                           STRUCTURE / USER_MANUAL / CHANGELOG
├── tests/                          stdlib unittest — 추가 설치 없이 실행
└── pythonization/                  ── 애플리케이션 패키지 ──
```

### `pythonization/app/` — 앱 기동

| 파일 | 역할 |
|---|---|
| `bootstrap.py` | 기동 순서: 로깅 → QApplication → 프로파일 선택 → MainWindow |
| `paths.py` | `APP_DIR` / `SETTINGS_DIR` 확정. 사용자 데이터 폴더 포인터 관리 |
| `logging_setup.py` | 로깅 + 크래시 캡처 (faulthandler / excepthook / Qt 메시지 핸들러) |

### `pythonization/config/` — 설정 모델

| 파일 | 역할 |
|---|---|
| `models.py` | 전체 데이터 모델 (Pydantic) — 라이브러리·인스턴스·프로파일 계층 |
| `app_config.py` | 전역 앱 설정 로드/저장 (`app_config.yaml`) |

### `pythonization/instruments/` — 계측기

| 파일 | 역할 |
|---|---|
| `session.py` | VISA I/O 허브. **물리 리소스 단위 락**으로 thread-safe |
| `base.py` | 모든 드라이버의 추상 기반 클래스 |
| `factory.py` | class_name 문자열 → 드라이버 인스턴스. 구 경로 매핑(`resolve_class_path`) |
| `registry.py` | `instruments.yaml` 로더 |
| `parameter.py` | `MeasurementParameter` / `SweepParameter` / `_parse_float` |
| `errors.py` | VISA 오류 분류 — 통신 오류 판정(`is_comm_error`), 사람이 읽을 설명 |
| `command_library.py` | `visa_libraries.yaml` 로더/저장 |

`instruments/drivers/` — 모듈명은 `vendor_model.py` 규칙을 따른다.

| 파일 | 장비 |
|---|---|
| `generic_scpi.py` | 범용 SCPI (Mercury iPS/iTC 포함) |
| `keithley_2636a.py` | Keithley 2636A (TSP) |
| `oxford_itc.py` / `oxford_ips.py` | Oxford ITC / IPS |
| `lakeshore_m81.py` | Lake Shore M81 / LAN Raw Socket (VNA 등) |
| `srs_sr830.py` | SRS SR830 lock-in |
| `zurich_mfli.py` | Zurich MFLI — **非VISA** (LabOne Data Server + zhinst 노드 트리) |
| `dummy.py` | 테스트용 Mock |

### `pythonization/measurement/` — 측정 엔진

| 파일 | 역할 |
|---|---|
| `sweep.py` | `SweepConfig` + `calculate_next_step()` |
| `channel.py` | `SweepChannel` / `TimeChannel` |
| `sweep_worker.py` | `SweepWorker` (QThread — VISA I/O, 병렬 측정) |
| `second_channel_worker.py` | `SecondChannelWorker` (QThread — advance 4종) |
| `second_channel_model.py` | 스레드 안전 second 값 테이블 모델 (순수 Python + Lock) |
| `derivative.py` | 실시간 dA/dB 계산 (슬라이딩 윈도우) |
| `data_saver.py` | `.dat` 저장 (자동번호 배타 생성, 매 행 fsync) |
| `metadata.py` | 측정 T/B 메타데이터 버퍼/통계 |
| `resume_log.py` | 중단 지점 스냅샷 저장/로드 |

### 그 밖의 도메인 패키지

| 경로 | 역할 |
|---|---|
| `profiles/registry.py` | 명명된 프로파일 관리 |
| `analysis/mfli_merge.py` | LabOne CSV + MFLI `.dat` sweep별 병합. **Qt·numpy 비의존 → CLI 겸용** |
| `notify/alarm_manager.py` | 사운드·이메일·텔레그램 알람 (데몬 오프로드) |
| `util/network.py` | LAN·MAC 유틸 (DHCP 대응 IP 추적) |

### `pythonization/ui/` — GUI

| 경로 | 역할 |
|---|---|
| `main_window.py` | MainWindow — 주 제어 UI + sweep 루프 |
| `widgets/plot_panel.py` | **공용** 라이브 플롯 패널 (VNA·MFLI 공유) |
| `widgets/help_button.py` | 공통 '?' 도움말 버튼 |
| `assets/` | 정적 자산 + `asset_path()` 헬퍼 |

`ui/dialogs/` — 설정·구성 다이얼로그

| 파일 | 역할 |
|---|---|
| `profile_launch.py` | 시작 시 프로파일 선택 |
| `instrument_settings.py` | 하드웨어 등록. 드라이버 목록을 `drivers/` 에서 자동 검색 |
| `visa_library.py` | VISA 명령어 라이브러리 편집 |
| `parameter_manager.py` | 측정 항목 구성 (`{p}` 채우기) |
| `app_config.py` | 앱 설정 |
| `alarm_config.py` | 알람 설정 |
| `meta_data.py` | 측정 전 메타 기록 |
| `resume.py` | 재개 지점 선택 |

`ui/panels/` — 측정 중 보조 창

| 파일 | 역할 |
|---|---|
| `graph_window.py` | 실시간 그래프 + 맵 |
| `data_window.py` / `timing_window.py` / `sweep_array_window.py` | 측정값 / 타이밍 / 진행 |
| `debug_window.py` | VISA 로그 + 콘솔 |
| `command_window.py` / `console_handler.py` | 즉석 명령 실행 + 파서 |
| `second_channel_table_window.py` | second 값 테이블 편집 |

`ui/modules/` — 독립 측정 모듈 창 (각자 자기 설정·워커를 가진다)

| 경로 | 역할 |
|---|---|
| `vna/window.py` | VNA 제어 + Double Sweep(field-time/resume) |
| `vna/config_window.py` | VNA 명령/advance 설정 |
| `vna/models.py` | VNA 데이터 모델 + config/resume IO |
| `mfli/window.py` | MFLI 주파수 noise sweep (+per-point 보조 읽기) |
| `mfli/models.py` | MFLI 측정 설정 모델 + config IO |
| `double_sweep/window.py` | 레거시 이중 sweep + 알람 |

### 런타임 파일 (저장소 밖)

`SETTINGS_DIR` 기본값은 `~/Documents/pythonization/settings`.
Settings → Config 의 `data_dir` 로 바꿀 수 있다(재시작 후 적용).

```
settings/
├── logs/app.log            실행 로그 + 미처리 예외 (오류 신고 시 이 파일)
├── logs/fault.log          네이티브 크래시 스택 덤프
├── instruments.yaml        기기 연결 설정
├── visa_libraries.yaml     VISA 명령어 라이브러리
├── active_profile.txt      현재 활성 프로파일 이름
├── resume_points.json      중단 지점
└── profiles/{name}.yaml    명명된 사용자 프로파일
```

---

## 전체 구조도

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              CONFIGURATION                                  │
│  settings/instruments.yaml   settings/visa_libraries.yaml                   │
│  settings/profiles/{name}.yaml   settings/active_profile.txt                │
│                                                                             │
│  instruments.registry      instruments.command_library    profiles.registry │
└──────────────┬──────────────────────────┬───────────────────┬──────────────┘
               │                          │                   │
               ▼                          ▼                   ▼
┌──────────────────────┐   ┌──────────────────────┐  ┌──────────────────────┐
│ instruments.session  │   │ ui.dialogs.          │  │ ui.dialogs.          │
│  (VISA I/O 허브)     │   │   visa_library       │  │   parameter_manager  │
│  ┌────────────────┐  │   │  (명령어 편집)       │  │  (측정 항목 구성)    │
│  │ factory        │  │   └──────────────────────┘  └──────────┬───────────┘
│  │ base           │  │                                        │ selection_applied
│  │  └ drivers/*   │  │                                        ▼
│  └────────────────┘  │   ┌─────────────────────────────────────────────────┐
│  물리 리소스 단위 락 │   │              ui.main_window                     │
└──────────┬───────────┘   │  ┌──────────────┐  ┌──────────────────────────┐│
           │               │  │ Sweep Params │  │  Sweep Channel (radio)   ││
           │ VISA I/O      │  │ to/rate/tpp  │  │  Measurements (checkbox) ││
           ▼               │  └──────────────┘  └──────────────────────────┘│
┌──────────────────────┐   │  ┌──────────────┐  ┌──────────────────────────┐│
│ measurement.         │   │  │ Profile bar  │  │   Save Settings          ││
│   sweep_worker       │◄──┤  └──────────────┘  │   → measurement.         ││
│  (QThread)           │   │  ┌──────────────┐  │       data_saver         ││
│                      │   │  │ Quick Btns   │  └──────────────────────────┘│
│  1. calculate_next   │   │  │ Graph /      │  ┌──────────────────────────┐│
│  2. safety ramp      │   │  │ Double Sweep │  │  measurement.derivative  ││
│  3. write VISA cmd   │   │  │ Conn Test    │  │  dA1/dA2 sliding window  ││
│  4. batch meas query │   │  └──────────────┘  └──────────────────────────┘│
│  → StepResult emit   │   └────────────────────────────┬────────────────────┘
└──────────┬───────────┘                                │
           │ step_done                                  │
           └────────────────────────────────────────────┘
                        _on_step_done 이 순서대로:
                        ┌──────────────────────────────────────┐
                        │  _record_row()       → .dat 파일     │
                        │  metadata.record_step()              │
                        │  _push_graph_point() → GraphWindow   │
                        │  _update_step_status() / 남은 시간   │
                        │  _schedule_next_step()               │
                        └──────────────────────────────────────┘
                        기록에 실패하면 뒤 단계로 넘어가지 않고 측정을 멈춘다.

┌─────────────────────────────────────────────────────────────────────────────┐
│                          DOUBLE SWEEP                                       │
│  ui.modules.double_sweep.window                                             │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  IDLE → PRE_INIT → ADVANCING_SECOND ──────────────────────────┐     │   │
│  │                         ▲                                     │     │   │
│  │                         │ array[i+1]                          ▼     │   │
│  │                    RETRACE ◄──── TRACE ◄──── DUMMY            │     │   │
│  │                         │                                     │     │   │
│  │                         └────── array 소진 ──────── IDLE ◄────┘     │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  measurement.second_channel_worker (QThread)                                │
│    SIMPLE_HOP     → write 1회                                               │
│    SWEEP          → calculate_next_step 루프 (safety 포함)                  │
│    FEEDBACK       → write → poll 루프 (tolerance % 도달 + 안정화)           │
│    WAIT_FOR_TIME  → write → sleep(wait_time)                                │
│                                                                             │
│  각 Phase(DUMMY/TRACE/RETRACE)는 자체 SweepWorker + DataSaver 사용          │
│  저장 경로: main_folder / custom_folder / [phase] / YYYY-MM-DD / file.dat   │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│                     데이터 모델 계층 (config.models)                        │
│                                                                             │
│  [Library 계층]              [Instantiated 계층]     [Profile 계층]         │
│  MeasurementParamDef   →    InstantiatedMeasurement                         │
│  SweepValueDef         →    InstantiatedSweepValue   ┐                      │
│  WriteCmdDef           →    InstantiatedWriteCmd     ├→ MainUIProfile       │
│  (visa_libraries.yaml)      InstantiatedSecond...    ┘  ┐                   │
│                             {p} 채워짐                  ├→ FullProfile      │
│  ParameterManagerProfile (SelectedEntry 목록)        ───┘   (profiles/      │
│  DoubleSweepConfig                                           {name}.yaml)   │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 모듈 의존성 (핵심 경로)

```
main.py  (sys.path 설정만)
 └─ app/bootstrap.py
     ├─ app/logging_setup.py        ← 가장 먼저. 이후 단계가 죽어도 로그가 남는다
     ├─ app/paths.py
     ├─ profiles/registry.py
     ├─ ui/dialogs/profile_launch.py
     └─ ui/main_window.py
         ├─ instruments/session.py
         │   ├─ instruments/factory.py → instruments/base.py ← drivers/*
         │   └─ instruments/registry.py
         ├─ instruments/{command_library, errors}.py
         ├─ measurement/sweep_worker.py → {sweep, channel, parameter}.py
         ├─ measurement/{data_saver, derivative, metadata, resume_log}.py
         ├─ profiles/registry.py → config/{models, app_config}.py
         ├─ ui/dialogs/*            (instrument_settings, visa_library,
         │                            parameter_manager, app_config, meta_data, resume)
         ├─ ui/panels/*             (graph, data, timing, sweep_array, debug, command)
         ├─ ui/widgets/help_button.py
         └─ ui/modules/
             ├─ vna/window.py            → ui/widgets/plot_panel.py
             ├─ mfli/window.py           → ui/widgets/plot_panel.py
             │                           → analysis/mfli_merge.py
             └─ double_sweep/window.py   → measurement/second_channel_worker.py
                                         → notify/alarm_manager.py
```

계층 규칙: `ui/` 는 도메인 패키지를 부르지만 그 반대는 없다. `analysis/` 는 Qt 를
import 하지 않는다(CLI 겸용). 드라이버는 `instruments/base.py` 외에 아무것도 모른다.

---

## 스레드 구조

```
Main Thread (Qt Event Loop)
 ├─ MainWindow / VnaWindow / MfliWindow / DoubleSweepWindow UI (모든 QWidget·플롯)
 ├─ QTimer (sweep tick)
 └─ Signal/Slot 수신 (워커→GUI는 모두 bound @Slot = 자동 큐잉)

Worker Thread: SweepWorker (메인 sweep) / _AcquireWorker (VNA) / _MfliAcquireWorker
 └─ VISA write + measurement query, 신호 emit만 (GUI 직접 접근 금지)
    병렬 측정 시 ThreadPoolExecutor로 서로 다른 alias 동시 측정

Worker Thread: SecondChannelWorker  [double sweep advance 시 활성]
 └─ second channel 이동 / feedback·threshold 폴링
```

### 안전 불변식 (Threading / VISA) — 위반 시 네이티브 크래시

1. **워커 → GUI는 반드시 bound `@Slot` 메서드로 연결**한다. lambda/일반 함수에 연결하면
   수신 QObject가 없어 큐잉되지 않고 **워커 스레드에서 직접 실행** → QWidget 접근 시
   access violation. (예: `worker.progress.connect(self._on_acq_progress)`)
2. **같은 물리 리소스(주소)의 `viOpen`/`viClose`는 동시 실행 금지.** `InstrumentSession`의
   통신 락은 **물리 리소스 단위**(`interface|address|port`)로 묶고(`_resource_key`),
   eviction의 disconnect는 alias 락 안에서 **동기**로 수행한다(데몬 스레드 금지).
   같은 장비를 가리키는 서로 다른 alias도 같은 락 → 병렬에서 직렬화.
3. **프로세스 종료/`ResourceManager.close()` 전에 모든 워커 QThread를 quit+wait**한다.
   `MainWindow.closeEvent` → `VnaWindow.shutdown_threads()`·`MfliWindow.shutdown_threads()` →
   `session.shutdown()` 순서. `open/write/read/query`는 `_shut_down` 가드로 닫힌 핸들 I/O를 막는다.
   VNA·MFLI 창은 `closeEvent`에서 ignore+저장+hide(파괴 안 함) → 실행 중 워커가 방치되지 않는다.
4. **QObject는 자신의 affinity 스레드에서 파괴**한다(`_sec`는 워커 `run()`의 finally에서
   deleteLater). 워커 QThread는 parent 없이 만들고 참조+deleteLater로 수명 관리.
5. **pyqtgraph `setData`에 NaN/Inf 금지**(`skipFiniteCheck` 미사용, Inf→NaN gap 치환).
   공용 `ui/widgets/plot_panel.py` 의 `YCurveRow.set_data()` 가 이 정규화를 담당한다.
6. **`SecondChannelModel`은 순수 파이썬 + `Lock`(Qt 미사용)** — 워커가 매 행 값을 `value_at`/
   `count`로 '새로' 읽고 상태를 int 플립(`mark_current`/`mark_done`)만 한다. GUI는 PENDING
   행만 수정/추가/삭제. **`BlockingQueuedConnection`으로 대체 금지**(`shutdown_threads`의
   `thread.wait()`와 데드락). 워커→테이블 창 갱신은 bound `@Slot`으로만.
   완료(DONE) 행은 앞쪽 prefix 고정 → 미래 행 편집/추가 안전.
7. **非VISA 드라이버(`instruments/drivers/zurich_mfli.py`)** — pyvisa 대신
   `zhinst.core.ziDAQServer`로 **이미 떠 있는** LabOne Data Server에 접속하고
   노드 경로를 명령 문자열로 번역(`connect`/`write`/`query` override). `self.inst`는 가드
   통과용 sentinel. 두 번째 Data Server를 새로 띄우지 않는다.
   **`connectDevice`는 노드를 읽어 접근 불가일 때만** 호출한다(`_device_accessible()`) —
   이미 연결돼 있으면(LabOne 사용 중) 재연결이 오래 걸리다 timeout 나거나 'in use'로
   거부되고, 성공하면 오히려 LabOne 측정을 방해한다.
   **`server_host`는 LabOne이 접속한 Data Server 주소여야 한다.** MFLI는 장비 자체가
   Data Server를 돌리므로 보통 **장비 IP**(예: 192.168.0.13)이고, 로컬(127.0.0.1)을
   넣으면 장비가 `/zi/devices/visible`에는 보이지만 `/zi/devices/connected`에는 없어
   `DeviceInUseError`가 난다. `_connect_failure_hint()`가 두 목록을 대조해 이 상황을
   구체적으로 알려 준다. 주의:
   `_evict_broken`은 `pyvisa.VisaIOError`만 처리하므로 MFLI 오류엔 세션 자동 eviction/재연결이
   걸리지 않는다(설계상 허용). 락 키(`lan|localhost|8004`)가 ITC/M81과 달라 per-point 다중
   alias 병렬 읽기 안전.

### 조건부 import 규칙

함수 안의 import 는 모듈 상단으로 정리했지만, 다음은 **의도적으로 지역에 남긴다**:

- 선택 의존성 — `zhinst`(MFLI), `scipy`(SG 필터). 없어도 앱이 떠야 한다.
- 플랫폼 분기 — `winsound`.
- `app/bootstrap.py` — 로깅을 먼저 걸고 나서 무거운 GUI 를 import 해야
  import 단계에서 죽어도 원인이 `app.log` 에 남는다.

### 로깅 / 크래시 캡처 — `app/logging_setup.py`

`bootstrap.main()`이 `setup_logging(SETTINGS_DIR/"logs")`를 1회 호출:

- **faulthandler**(`fault.log`): segfault 등 네이티브 크래시 스택 덤프(평소 오버헤드 0).
- **sys/threading excepthook + Qt 메시지 핸들러**(`app.log`): 메인·워커 미처리 예외 전체
  트레이스백 + Qt 경고/치명. RotatingFileHandler(2MB×3)로 가벼움.

---

## 데이터 흐름 요약

```
[설정]
instruments.yaml ──→ instruments.registry ──→ instruments.session ──→ 기기
                                                    ▲
                                     구 경로는 factory.resolve_class_path 가 흡수

[라이브러리]
visa_libraries.yaml ──→ instruments.command_library ──→ ui.dialogs.visa_library
                                                        │
                                            ui.dialogs.parameter_manager
                                                        │ {p} 채움
                                                        ▼
[프로파일]                             InstantiatedX → MainUIProfile
profiles/{name}.yaml ◄──── profiles.registry ◄──────────────────
                                   │
                                   ▼
[측정]
MainWindow._on_start() → QTimer → request_step → measurement.sweep_worker
                                                  │ step_done
                                                  ▼
                measurement.data_saver(.dat) ◄── StepResult ──► ui.panels.graph_window
                                                  │
                                    ui.panels.{data_window, timing_window}
```

---

## 하위 호환 계층

구조를 바꾸면서 **기존 랩 PC 의 설정 파일을 그대로 읽어야** 하는 지점:

| 위치 | 흡수하는 것 |
|---|---|
| `instruments/factory.py` 의 `_LEGACY_MODULES` | `instruments.yaml` 에 저장된 구 드라이버 경로 (`driver.m81.M81Instrument` → `pythonization.instruments.drivers.lakeshore_m81.M81Instrument`). Instrument Settings 에서 저장하면 새 경로로 갱신된다 |
| `config/app_config.py` 의 `_LEGACY_CONFIG_PATH` | 구 위치(Documents)의 `app_config.yaml` |
| `ui/modules/*/window.py` 의 `_plot_state()` | `y_sources` 가 비면 구 버전 단일 `y_source` 로 폴백 |

---

## 테스트

```
python -m unittest discover -s tests -t .
```

### 구조 변경 회귀를 잡는 네 축

리팩터링에서 실제로 터진 실패들을 하나씩 막는다. 넷 다 GUI 를 띄우지 않고 돈다.

| 테스트 | 잡는 실패 |
|---|---|
| `test_imports` | 모듈이 아예 import 되지 않음 |
| `test_import_targets` | 함수 안에 숨은 import 가 깨짐 — 그 메뉴를 눌러야 드러난다 |
| `test_windows` | 창 생성이 깨짐 — 모듈 import 만으로는 안 드러난다 |
| `test_cross_references` | 다른 창이 쓰는 `MainWindow` 내부 이름이 사라짐 — **측정을 실제로 돌려야** 드러난다 |
| `test_annotations` | 어노테이션 전용 import 누락 — 3.14 는 지연 평가라 여기선 안 드러나고 3.10~3.13 에서 터진다 |

`test_cross_references` 와 `test_annotations` 는 각각 이 리팩터링 중 실제로 발생한
회귀(`_deriv_val_for_step` 유실, `Optional` 누락)를 잡아낸 것이다.

### 동작 계약을 고정하는 테스트

sweep 진행 규칙, 스텝 기록(값/nan/ERR), `.dat` 저장, 응답 파싱, 통신 오류 판정,
미분 채널, FEEDBACK 도달·안정화 판정, 프로파일 재구성, placeholder 치환,
double sweep 축 순서, LabOne 병합, 공용 플롯 패널.

### UI 변경 검증

UI 는 값으로 확인하기 어려워, 창의 위젯 트리를 통째로 덤프해 리팩터링 전후를
대조하는 방식을 썼다(클래스·텍스트·체크상태·표시여부·활성여부 + 레이아웃 순서).
`QScrollArea`·`QSplitter` 는 `layout()` 으로 자식이 안 잡히므로 `children()` 을
재귀 순회한다. 일회성 도구라 저장소에는 넣지 않았다 — UI 를 크게 손볼 때 다시
만들어 쓰면 된다.
