# Pythonization — 시스템 구조

> Last updated: 2026-07-14 | Version: 1.07

---

## 디렉토리 레이아웃

```
pythonization/
├── main.py                         앱 진입점 (QApplication + MainWindow)
├── config/
│   └── config_models.py            전체 데이터 모델 (Pydantic)
├── core/
│   ├── applog.py                   로깅 + 크래시 캡처 (faulthandler/excepthook/Qt)
│   ├── app_dirs.py                 APP_DIR / SETTINGS_DIR 경로 관리
│   ├── instrument_base.py          모든 드라이버의 추상 기반 클래스
│   ├── instrument_factory.py       동적 드라이버 인스턴스 생성
│   ├── instrument_registry.py      instruments.yaml 로더
│   ├── instrument_session.py       VISA I/O 허브 (물리 리소스 단위 락, thread-safe)
│   ├── instrument_parameter.py     MeasurementParameter / SweepParameter / _parse_float
│   ├── sweep_channel.py            SweepChannel / TimeChannel
│   ├── sweep.py                    SweepConfig + calculate_next_step()
│   ├── sweep_worker.py             SweepWorker (QThread — VISA I/O, 병렬 측정)
│   ├── second_channel_worker.py    SecondChannelWorker (QThread — advance 4종)
│   ├── alarm_manager.py            AlarmManager — 사운드/이메일/텔레그램 (데몬 오프로드)
│   ├── derivative_channel.py       실시간 dA/dB 계산 (슬라이딩 윈도우)
│   ├── data_saver.py               .dat 파일 저장 (자동번호 배타 생성)
│   ├── meta_data_manager.py        측정 T/B 메타데이터 버퍼/통계
│   ├── visa_library_registry.py    visa_libraries.yaml 로더/저장
│   ├── profile_registry.py         ProfileRegistry — 명명된 프로파일 관리
│   ├── visa_errors.py / network_utils.py  VISA 오류 분류 / LAN·MAC 유틸
│   ├── mfli_merge.py               LabOne CSV + MFLI .dat sweep별 병합(Noise level 계산·CLI 겸용)
│   └── (레거시) parameter_manager_registry.py
├── driver/                         계측기 드라이버 (instruments.yaml의 class_name)
│   ├── generic_scpi.py             범용 SCPI (Mercury iPS/iTC: split_semicolons 동기)
│   ├── m81.py                      M81 / LAN Raw Socket (VNA 등)
│   ├── keithley_2636a.py           Keithley 2636A TSP
│   ├── oxford_itc.py               Oxford ITC
│   ├── mfli.py                     Zurich MFLI (zhinst/LabOne Data Server, 노드 트리 — 非VISA)
│   └── dummy_instrument.py         테스트용 Mock
├── gui/
│   ├── main_window.py              MainWindow — 주 제어 UI + sweep 루프
│   ├── vna_window.py               VnaWindow — VNA 제어 + Double Sweep(field-time/resume)
│   ├── vna_config_window.py        VnaConfigWindow — VNA 명령/advance 설정
│   ├── vna_models.py               VNA 데이터 모델 + config/resume IO
│   ├── double_sweep_window.py      DoubleSweepWindow — 레거시 이중 스윕 + 알람
│   ├── mfli_window.py              MfliWindow — MFLI 주파수 noise sweep 측정 창(+per-point 보조 읽기)
│   ├── mfli_models.py              MFLI 측정 설정 모델 + config IO
│   ├── second_channel_model.py     SecondChannelModel — 스레드 안전 second 값 테이블 모델
│   ├── second_channel_table_window.py  SecondChannelTableWindow — second 값 테이블 편집 창
│   ├── parameter_manager_window.py ParameterManagerWindow — 측정 항목 구성
│   ├── visa_library_window.py      VisaLibraryWindow — VISA 명령어 라이브러리
│   ├── graph_window.py             GraphWindow — 실시간 그래프
│   ├── config_window.py / alarm_config_window.py  앱 설정 / 알람 설정
│   ├── instrument_settings_ui.py   InstrumentSettingsUI — 하드웨어 설정
│   ├── debug_window.py             DebugWindow — VISA 로그 + 콘솔
│   ├── data_window.py / sweep_array_window.py / timing_window.py  측정값/진행/타이밍
│   ├── help_button.py              공통 '?' 도움말 버튼
│   └── console_handler.py          콘솔 명령어 파서/실행
└── settings/
    ├── logs/                       app.log / fault.log (크래시 진단)
    ├── instruments.yaml            기기 연결 설정
    ├── visa_libraries.yaml         VISA 명령어 라이브러리
    ├── active_profile.txt          현재 활성 프로파일 이름
    └── profiles/
        └── {name}.yaml             명명된 사용자 프로파일 (YAML)
```

---

## 전체 구조도

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              CONFIGURATION                                  │
│                                                                             │
│  settings/instruments.yaml     settings/visa_libraries.yaml                │
│  settings/profiles/{name}.yaml settings/active_profile.txt                 │
│                                                                             │
│  InstrumentRegistry            VisaLibraryRegistry   ProfileRegistry        │
│  (instruments.yaml 로드)        (visa_libraries.yaml)  (profiles/*.yaml)    │
└──────────────┬──────────────────────────┬───────────────────┬──────────────┘
               │                          │                   │
               ▼                          ▼                   ▼
┌──────────────────────┐   ┌──────────────────────┐  ┌──────────────────────┐
│   InstrumentSession  │   │  VisaLibraryWindow   │  │ ParameterManagerWin  │
│  ┌────────────────┐  │   │  (VISA 명령어 편집)   │  │  (측정 항목 구성)     │
│  │ InstrumentFact │  │   │  MeasurementParamDlg │  │  AddEntryDialog      │
│  │ BaseInstrument │  │   │  SweepValueDialog    │  │  SectionPanel×3      │
│  │  ├ Keithley    │  │   │  WriteCmdDialog      │  │  (Sweep/Meas/2nd)    │
│  │  ├ GenericSCPI │  │   └──────────────────────┘  └──────────┬───────────┘
│  │  ├ OxfordITC   │  │                                        │ selection_applied
│  │  └ Dummy       │  │                                        ▼
│  └────────────────┘  │   ┌─────────────────────────────────────────────────┐
│  thread-safe lock    │   │                  MainWindow                      │
└──────────┬───────────┘   │  ┌──────────────┐  ┌──────────────────────────┐│
           │               │  │ Sweep Params │  │  Sweep Channel (radio)   ││
           │ VISA I/O      │  │ start / stop │  │  Measurements (checkbox) ││
           ▼               │  │ rate / t/pt  │  └──────────────────────────┘│
┌──────────────────────┐   │  └──────────────┘  ┌──────────────────────────┐│
│     SweepWorker      │   │  ┌──────────────┐  │   Save Settings          ││
│  (QThread worker)    │◄──┤  │ Profile bar  │  │   main_folder            ││
│                      │   │  │ combo+CRUD   │  │   custom_folder          ││
│  1. calculate_next   │   │  └──────────────┘  │   DataSaver              ││
│  2. safety ramp      │   │  ┌──────────────┐  └──────────────────────────┘│
│     (batch if Δt=0)  │   │  │ Quick Btns   │  ┌──────────────────────────┐│
│  3. write VISA cmd   │   │  │ Graph /      │  │   Derivative Channel     ││
│  4. batch meas query │   │  │ Double Sweep │  │   dA1/dA2 sliding window ││
│     (TSP 한번에)      │   │  │ Conn Test    │  └──────────────────────────┘│
│  → StepResult emit   │   │  └──────────────┘                              │
└──────────┬───────────┘   └────────────────────────────┬────────────────────┘
           │ step_done                                   │
           └─────────────────────────────────────────────┘
                        step_done 수신 후:
                        ┌──────────────────────────────────────┐
                        │  DataSaver.append_row()  → .dat 파일 │
                        │  GraphWindow.append_point()          │
                        │  DataWindow 업데이트                  │
                        │  DerivativeChannel.push()            │
                        │  TimingWindow / SweepArrayWindow      │
                        └──────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│                          DOUBLE SWEEP                                       │
│                                                                             │
│  DoubleSweepWindow                                                          │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  State Machine:                                                      │   │
│  │                                                                      │   │
│  │  IDLE → PRE_INIT → ADVANCING_SECOND ──────────────────────────┐    │   │
│  │                         ▲                                      │    │   │
│  │                         │ array[i+1]                           ▼    │   │
│  │                    RETRACE ◄──── TRACE ◄──── DUMMY             │    │   │
│  │                         │                                      │    │   │
│  │                         └────── array 소진 ──────── IDLE ◄─────┘    │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│  SecondChannelWorker (QThread)                                              │
│    SIMPLE_HOP  → write 1회                                                  │
│    SWEEP       → calculate_next_step 루프 (safety 포함)                      │
│    FEEDBACK    → write → poll 루프 (tolerance % 도달 시 종료)                 │
│    WAIT_FOR_TIME → write → sleep(wait_time)                                 │
│                                                                             │
│  각 Phase(DUMMY/TRACE/RETRACE)는 자체 SweepWorker + DataSaver 사용           │
│  저장 경로: main_folder / custom_folder / [phase] / YYYY-MM-DD / file.dat   │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│                          데이터 모델 계층                                    │
│                                                                             │
│  [Library 계층]              [Instantiated 계층]     [Profile 계층]         │
│  MeasurementParamDef   →    InstantiatedMeasurement                        │
│  SweepValueDef         →    InstantiatedSweepValue   ┐                     │
│  WriteCmdDef           →    InstantiatedWriteCmd     ├→ MainUIProfile       │
│  (visa_libraries.yaml)      InstantiatedSecond...   ┘   ┐                  │
│                             {p} 채워짐                    ├→ FullProfile      │
│                                                          │   (profiles/     │
│  ParameterManagerProfile (SelectedEntry 목록)        ────┘    {name}.yaml)  │
│  DoubleSweepConfig                                                          │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 모듈 의존성 (핵심 경로)

```
main.py
 └─ gui/main_window.py
     ├─ core/instrument_session.py
     │   ├─ core/instrument_factory.py
     │   │   └─ core/instrument_base.py  ←─ {Keithley, GenericSCPI, Oxford, Dummy}
     │   └─ core/instrument_registry.py
     ├─ core/sweep_worker.py
     │   ├─ core/sweep.py
     │   ├─ core/sweep_channel.py
     │   └─ core/instrument_parameter.py
     ├─ core/second_channel_worker.py
     │   ├─ core/sweep.py
     │   └─ core/sweep_channel.py
     ├─ core/data_saver.py
     ├─ core/derivative_channel.py
     ├─ core/profile_registry.py
     │   ├─ config/config_models.py
     │   └─ core/visa_library_registry.py
     ├─ gui/parameter_manager_window.py
     ├─ gui/visa_library_window.py
     ├─ gui/graph_window.py
     ├─ gui/double_sweep_window.py
     │   ├─ core/sweep_worker.py
     │   └─ core/second_channel_worker.py
     ├─ gui/debug_window.py
     ├─ gui/data_window.py
     ├─ gui/sweep_array_window.py
     ├─ gui/timing_window.py
     └─ gui/instrument_settings_ui.py
```

---

## 스레드 구조

```
Main Thread (Qt Event Loop)
 ├─ MainWindow / VnaWindow / DoubleSweepWindow UI (모든 QWidget·플롯)
 ├─ QTimer (sweep tick, ~0ms interval)
 └─ Signal/Slot 수신 (워커→GUI는 모두 bound @Slot = 자동 큐잉)

Worker Thread: SweepWorker (메인 sweep) / _AcquireWorker (VNA)
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
6. **`SecondChannelModel`은 순수 파이썬 + `Lock`(Qt 미사용)** — 워커가 매 행 값을 `value_at`/
   `count`로 '새로' 읽고 상태를 int 플립(`mark_current`/`mark_done`)만 한다. GUI는 PENDING
   행만 수정/추가/삭제. **`BlockingQueuedConnection`으로 대체 금지**(`shutdown_threads`의
   `thread.wait()`와 데드락). 워커→테이블 창 갱신은 bound `@Slot`(`_on_second_row` /
   레거시 `_begin_array_index`)으로만. 완료(DONE) 행은 앞쪽 prefix 고정 → 미래 행 편집/추가
   안전.
7. **非VISA 드라이버(`driver/mfli.py` `ZurichMFLI`)** — pyvisa 대신 `zhinst.core.ziDAQServer`로
   **이미 떠 있는** LabOne Data Server(localhost:8004)에 접속하고 노드 경로를 명령 문자열로
   번역(`connect`/`write`/`query` override). `self.inst`는 가드 통과용 sentinel. 두 번째 Data
   Server를 새로 띄우지 않는다. **`connectDevice`는 `/zi/devices/connected`에 장비가 없을 때만**
   호출한다 — 이미 연결돼 있으면(LabOne 사용 중) 재연결 시도가 오래 걸리다 timeout(예: PCIe)나므로
   건너뛰고 노드만 읽는다. 주의: `_evict_broken`은 `pyvisa.VisaIOError`만 처리하므로 MFLI 오류엔
   세션 자동 eviction/재연결이 걸리지 않는다(설계상 허용). 락 키(`lan|localhost|8004`)가 ITC/M81과
   달라 per-point 다중 alias 병렬 읽기 안전.

### 로깅 / 크래시 캡처 — `core/applog.py`

`main.py`가 `setup_logging(SETTINGS_DIR/"logs")`를 1회 호출:
- **faulthandler**(`fault.log`): segfault 등 네이티브 크래시 스택 덤프(평소 오버헤드 0).
- **sys/threading excepthook + Qt 메시지 핸들러**(`app.log`): 메인·워커 미처리 예외 전체
  트레이스백 + Qt 경고/치명. RotatingFileHandler(2MB×3)로 가벼움.

---

## 데이터 흐름 요약

```
[설정]
instruments.yaml ──→ InstrumentRegistry ──→ InstrumentSession ──→ 기기

[라이브러리]
visa_libraries.yaml ──→ VisaLibraryRegistry ──→ VisaLibraryWindow (편집)
                                                        │
                                             ParameterManagerWindow (인스턴스화)
                                                        │ {p} 채움
                                                        ▼
[프로파일]                              InstantiatedX → MainUIProfile
profiles/{name}.yaml ◄──── ProfileRegistry ◄──────────────────────
                                   │
                                   ▼
[측정]
MainWindow.start() → QTimer → request_step → SweepWorker
                                                  │ step_done
                                                  ▼
                         DataSaver(.dat) ◄── StepResult ──► GraphWindow
                                                  │
                                           DataWindow / TimingWindow
```
