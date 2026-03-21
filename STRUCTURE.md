# Pythonization — 시스템 구조

> Last updated: 2026-03-20 | Version: 1.01

---

## 디렉토리 레이아웃

```
pythonization/
├── main.py                         앱 진입점 (QApplication + MainWindow)
├── config/
│   └── config_models.py            전체 데이터 모델 (Pydantic)
├── core/
│   ├── instrument_base.py          모든 드라이버의 추상 기반 클래스
│   ├── instrument_factory.py       동적 드라이버 인스턴스 생성
│   ├── instrument_registry.py      instruments.yaml 로더
│   ├── instrument_session.py       VISA I/O 허브 (alias 기반, thread-safe)
│   ├── instrument_parameter.py     MeasurementParameter / SweepParameter
│   ├── sweep_channel.py            SweepChannel / TimeChannel
│   ├── sweep.py                    SweepConfig + calculate_next_step()
│   ├── sweep_worker.py             SweepWorker (QThread — VISA I/O)
│   ├── second_channel_worker.py    SecondChannelWorker (QThread — double sweep)
│   ├── alarm_manager.py            AlarmManager — 사운드/이메일 알람 발동
│   ├── derivative_channel.py       실시간 dA/dB 계산 (슬라이딩 윈도우)
│   ├── data_saver.py               .dat 파일 저장
│   ├── visa_library_registry.py    visa_libraries.yaml 로더/저장
│   ├── profile_registry.py         ProfileRegistry — 명명된 프로파일 관리
│   ├── parameter_manager_registry.py  (레거시, ProfileRegistry로 대체)
│   ├── network_utils.py            LAN/GPIB/MAC 주소 유틸리티
│   ├── dummy_instrument.py         테스트용 Mock 드라이버
│   ├── generic_scpi.py             범용 SCPI 드라이버
│   ├── keithley_2636a.py           Keithley 2636A TSP SourceMeter 드라이버
│   ├── m81.py                      M81 LAN Raw Socket 드라이버 템플릿
│   └── oxford_itc.py               Oxford ITC 온도 컨트롤러 드라이버
├── gui/
│   ├── main_window.py              MainWindow — 주 제어 UI + sweep 루프
│   ├── parameter_manager_window.py ParameterManagerWindow — 측정 항목 구성
│   ├── visa_library_window.py      VisaLibraryWindow — VISA 명령어 라이브러리
│   ├── graph_window.py             GraphWindow — 실시간 그래프
│   ├── double_sweep_window.py      DoubleSweepWindow + AlarmPanel — 이중 파라미터 스윕 + 알람
│   ├── instrument_settings_ui.py   InstrumentSettingsUI — 하드웨어 설정
│   ├── debug_window.py             DebugWindow — VISA 로그 + 콘솔
│   ├── data_window.py              DataWindow — 현재 측정값 테이블
│   ├── sweep_array_window.py       SweepArrayWindow — 스윕 진행 상태
│   ├── timing_window.py            TimingWindow — 성능 타이밍 분석
│   └── console_handler.py          콘솔 명령어 파서/실행
└── settings/
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
 ├─ MainWindow UI
 ├─ QTimer (sweep tick, ~0ms interval)
 └─ Signal/Slot 통신

Worker Thread 1: SweepWorker
 └─ VISA write + measurement query
    (InstrumentSession lock으로 thread-safe)

Worker Thread 2: SecondChannelWorker  [double sweep 시에만 활성]
 └─ Second channel 이동
    (InstrumentSession lock 공유)
```

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
