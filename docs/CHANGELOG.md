# Changelog

> 이 파일은 이전에 저장소 루트의 `PATCH_NOTES.md` 였습니다.

---

## v1.8.0 — 2026-08-03 (구조 개편)

기능 변경은 없습니다. 디렉토리 구조·코드 정리와 그 과정에서 드러난 버그 수정입니다.
**기존 설정 파일과 프로파일은 그대로 쓸 수 있습니다.**

### ■ 사용자가 체감하는 변화

- **없음** — 창 구성, 조작, 저장 형식, 프로파일 모두 그대로입니다.
- 예외적으로 **패키징된 exe 에서 이메일·텔레그램 알람이 동작하게 됐습니다**(아래 버그 수정).
- 기존 `instruments.yaml` 의 드라이버 경로는 자동으로 새 경로로 해석됩니다.
  Instrument Settings 에서 한 번 저장하면 파일 내용도 갱신됩니다.

### ■ 디렉토리 구조

`core/` `gui/` `driver/` `config/` 4개 평면 패키지에 62개 파일이 성격 구분 없이
섞여 있어 어떤 기능이 어느 파일인지 예측할 수 없었습니다. 단일 패키지
`pythonization/` 아래 도메인별로 나눴습니다.

```
app/  config/  instruments/(+drivers/)  measurement/
profiles/  analysis/  notify/  util/
ui/(main_window + widgets/ dialogs/ panels/ modules/ assets/)
```

드라이버 모듈명을 `vendor_model.py` 로 통일했습니다.
`m81` → `lakeshore_m81`, `sr830` → `srs_sr830`, `mfli` → `zurich_mfli`,
`dummy_instrument` → `dummy`.

진입점도 정리했습니다. `main.py` 는 얇은 셸이고 실제 기동 순서는
`app/bootstrap.py` 에 있습니다. `python -m pythonization` 으로도 실행됩니다.

### ■ 버그 수정

- **[치명] 패키징된 exe 에서 이메일·텔레그램 알람이 죽던 문제** — `build_exe.spec` 의
  `excludes` 에 `email` / `urllib` / `http` 가 들어 있었는데 `alarm_manager` 가
  `smtplib`·`email.mime`·`urllib.request` 를 씁니다. 소스 실행에서는 멀쩡하고 exe
  에서만 ImportError 로 실패해 알아채기 어려웠습니다.
- **[치명] exe 빌드가 아예 실패하던 상태** — `build_exe.spec` 의 `datas` 가 존재하지
  않는 `settings` 폴더를 가리키고 있었습니다. GUI 자산 폴더로 교체했습니다.
- **VNA 플롯에서 y 소스를 바꾸면 TypeError** — `currentIndexChanged(int)` 를 인자
  없는 시그널의 `emit` 에 직접 연결했습니다. MFLI 쪽 복사본에는 고쳐져 있었는데
  VNA 쪽만 남아 있었습니다(복붙이 갈라진 전형적인 사례).
- **드라이버 초기화 오류가 엉뚱한 메시지로 둔갑** — `InstrumentFactory` 의 `try` 범위가
  인스턴스 생성까지 덮고 있어, 드라이버 `__init__` 이 던진 `ValueError` 가
  "class_name 형식 오류"로 보고됐습니다.
- **의존성 명세에 `zhinst`·`scipy` 누락** — `.bat` 이 설치하던 하드코딩 목록에
  MFLI 측정용 `zhinst` 와 미분 채널용 `scipy` 가 빠져 있었습니다. 둘 다 선택
  의존성으로 `requirements-optional.txt` 에 명시했습니다.
- **Double Sweep 의 미분값이 실제와 달랐음** — 스텝마다 미분을 두 번 계산했는데
  (`.dat` 기록용 1회 + 그래프용 1회), 계산 함수가 슬라이딩 윈도우에 값을 push 도
  하므로 같은 점이 두 번 쌓였습니다. 한 번만 계산해 재사용하도록 고쳤습니다.
- **`Optional` import 누락 (잠재)** — 어노테이션에만 쓰이는 이름이라 Python 3.14
  (지연 평가)에서는 드러나지 않지만 3.10~3.13 에서는 모듈 import 가 실패합니다.
  `test_annotations` 가 이를 강제로 확인합니다.

### ■ 코드 정리

- **중복 제거** — VNA 창과 MFLI 창이 플롯 패널 코드를 각각 ~325줄씩 복사해 갖고
  있었습니다(diff 149줄). `ui/widgets/plot_panel.py` 하나로 합치고 두 창의 차이는
  생성자 플래그(`square_hint`, `log_toggles`)로 남겼습니다.
- **함수 내부 import 정리** — 166곳이 함수 안에 숨어 있었습니다. 의존 그래프로
  확인해 보니 순환 참조 때문인 것은 하나도 없어 대부분 모듈 상단으로 옮겼습니다.
  선택 의존성·플랫폼 분기·부트스트랩의 의도적 지연만 남겼습니다.
- **거대 함수 분해 — 100줄 넘는 함수 21개 → 0개.** 가장 컸던 것부터:
  `DoubleSweepWindow._build_ui` 513→44, `MainWindow._build_sequence_panel` 328→14,
  `ProfileRegistry.rebuild_main_ui_from_library` 292→28,
  `AddEntryDialog._build_ui` 238→36, `AddEntryDialog._on_ok` 212→8,
  `MainWindow._on_step_done` 200→40, `MfliWindow._build_ui` 194→32,
  `VnaWindow._build_double_sweep_section` 183→33,
  `DoubleSweepWindow._on_step_done` 150→46, `VnaWindow._build_acquire_group` 150→28,
  `VnaWindow._start_double_sweep` 146→25, `MainWindow._on_start` 146→25,
  `InstrumentSettingsUI._setup_ui` 137→12,
  `SecondChannelWorker._do_feedback` 129→24, `MapPanel._build_ui` 128→10,
  `CommandWindow._build_ui` 118→21, `DoubleSweepWindow._prepare_run` 107→19,
  `merge_sweeps` 107→46, `MainWindow._build_deriv_panel` 107→48,
  `MainWindow.__init__` 101→21.
- 미사용 import 18건 제거, 중복 로컬 import 16건 제거.
- `resume_log` 가 재노출하던 `is_comm_error` 제거 — 같은 함수가 두 경로로 보였습니다.

### ■ 저장소 위생

- **`.gitignore` 신규** (없었습니다). `dist/` 950개(exe·Qt DLL 포함), `build/` 16개,
  `.pyc` 57개가 추적되고 있었습니다. 추적 파일 1084 → 71개.
- **`app_config.yaml` 추적 해제** — SMTP 비밀번호와 텔레그램 봇 토큰이 담기는
  파일입니다. `app_config.example.yaml` 템플릿을 대신 뒀습니다.
- 루트 임시 파일 정리 (`_diff_vna.txt` 94KB, `memo.txt`).
- 의존성 명세 신규: `requirements.txt` / `requirements-optional.txt` / `pyproject.toml`.

### ■ 테스트 (기존 0개 → 182개)

표준 라이브러리 `unittest` 만 써서 추가 설치 없이 어느 랩 PC에서도 돌아갑니다.

```
python -m unittest discover -s tests -t .
```

구조 변경 회귀를 잡는 축이 다섯입니다: 전 모듈 import, 소스의 모든 import 문 정적
검사(ast — 함수 내부 포함), 메뉴가 여는 창 10개 실제 생성, 다른 창이 쓰는
`MainWindow` 내부 이름 존재 확인, 어노테이션 강제 평가.

이 중 셋은 실제로 리팩터링 도중 발생한 회귀를 잡았습니다 — 창 생성 검사가
`ResumeLog` import 유실을, 교차 참조 검사가 `_deriv_val_for_step` 유실(Double Sweep
첫 스텝에서 죽는 상태)을, 어노테이션 검사가 `Optional` 누락을 잡았습니다.

나머지는 sweep 진행 규칙, 스텝 기록(값/nan/ERR), `.dat` 저장, 응답 파싱, 통신 오류
판정, 미분 채널, FEEDBACK 도달·안정화 판정, 프로파일 재구성 시 사용자 설정 보존,
placeholder 치환, double sweep 축 순서, LabOne 병합, 공용 플롯 패널의 계약을
고정합니다.

### ■ 문서

- `README.md` 신규 — 실행 방법, 무엇이 어디 있는지, 로그·설정 파일 위치, 테스트 목록.
- `STRUCTURE.md` / `USER_MANUAL.md` / `PATCH_NOTES.md` 를 `docs/` 로 이동.
  `PATCH_NOTES.md` → `CHANGELOG.md` 로 이름 변경.
- `STRUCTURE.md` 재작성 — 기존 문서는 이미 낡아서 `resume_log`, `command_window`,
  `meta_data_window`, `profile_launch_dialog`, `resume_dialog`, `oxford_ips`,
  `sr830`, `app_config` 등 8개 파일이 빠져 있었습니다.

---

## v1.07.1 — 2026-07-30 (버그픽스)

- **[치명] 장시간 측정 중 워커가 조용히 멈추던 race 수정** — `gui/mfli_window.py` follow 폴 루프와
  `gui/vna_window.py` 스텝 대기의 `time.sleep(min(X, deadline - perf_counter()))`. `while` 조건과
  sleep 인자가 `perf_counter()`를 **각각** 호출하므로, 그 사이에 deadline을 넘기면 인자가 음수가 되어
  `ValueError: sleep length must be non-negative`로 워커가 죽는다. 수천 번 중 한 번 걸리는 race라
  실제로 MFLI follow 측정이 **~38시간 뒤** 이 예외로 중단됐다(냉각 측정이 216 sweep에서 조용히 멈춤;
  데이터는 그때까지 정상 저장·flush됨). `max(0.0, min(X, …))`로 음수를 0으로 클램프해 수정. 동일 패턴이
  있던 VNA 창(`:244`)도 함께 수정(`:441`은 `if wait>0` 가드가 있어 무관). 폴 대기는 `_sleep_poll()`로
  일원화.
- **MFLI follow 견고성 강화** — 장시간 무인 측정이 계측기 순간 글리치로 통째로 멈추지 않도록: MFLI
  읽기(freq/noise)가 **일시적으로 실패하면 그 포인트만 건너뛰고 계속**하고, **연속** 실패가
  `_FAIL_ABORT_S`(기본 300s)를 넘을 때만 '장비 소실'로 보고 중단한다(이전엔 단 1회 실패로도 `break`).
  aux(ITC/M81)는 `_read_aux`가 개별 실패를 NaN 처리하므로 원래도 안 죽는다. freq 읽기 성공 시 연속
  실패 streak 리셋. 헤드리스 검증: 일시 1회 실패 → 건너뛰고 계속, 연속 실패 → 임계 초과 시 중단.

---

## v1.07.0 — 2026-07-14

Zurich MFLI(lock-in) 전용 측정 모듈 추가 — 주파수 sweep noise 측정 + 포인트별 보조값 로깅.

### ■ MFLI Noise Sweep 모듈 (VNA Control과 동일한 독립 창)

- **목적**: MFLI로 주파수를 sweep하며 각 주파수에서 noise level을 측정하고, 같은 행에
  probe 온도(Oxford iTC)·M81 DC voltage를 함께 기록.
- **연결 방식**: MFLI는 SCPI/VISA가 아니라 LabOne Data Server + zhinst 노드 트리로 제어된다.
  신규 어댑터 드라이버 `driver/mfli.py`(`ZurichMFLI`)가 **이미 실행 중인 Data Server**
  (기본 localhost:8004)에 접속해 노드 경로를 명령 문자열로 번역한다:
  set `"/{dev}/oscs/0/freq = {v}"`→`setDouble`, get `"/{dev}/..."`→`getDouble`. `{dev}`는
  `device_id`로 치환. write(주파수 변경) 직후 `settle_s` 대기로 lock-in 정착을 캡슐화(GUI
  타이밍과 무관하게 정착값을 읽음). BaseInstrument 계약(문자열 in/float out)을 만족하므로
  InstrumentSession·MeasurementParameter가 다른 장비와 동일하게 다룬다.
- **독립 모듈**: `gui/mfli_window.py`(`MfliWindow` + `_MfliAcquireWorker` + 플롯 패널),
  `gui/mfli_models.py`(설정 모델 + YAML IO). VNA 창을 copy-and-trim(worker/QThread 스레드
  안전·플롯·저장·프로파일 저장 패턴 재사용, double-sweep/time-mode/section/alarm은 제외).
- **두 가지 측정 방식(창에서 토글)**:
  - **Driven** (이 프로그램이 직접 sweep): LINEAR 주파수 sweep → 포인트별 `write(freq)`→
    `read(noise)`→`read(ITC)`→`read(M81)` → 한 행 `[frequency, noise, probe_T, M81_V]`.
  - **Follow** (LabOne이 sweep, 나는 따라 읽기): 주파수를 쓰지 않고 `poll_interval`마다 **주파수
    노드만** 촘촘히 확인하다가, **주파수가 직전 기록값과 달라질 때만**(sweep 첫 폴 포함) noise+보조값
    (ITC·M81)을 읽어 `[time, frequency, (noise,) probe_T, M81_V]` 한 행을 기록 → **한 주파수당 1개**
    (`poll_count`=기록 포인트 수, 0이면 Stop까지). LabOne Sweeper와 오실레이터를 다투지 않도록 read-only.
    라이브 플롯은 주파수 **wrap(새 sweep 시작)** 감지 시 이전 sweep 표시를 지우고 **현재 sweep만**
    보여준다(방향 반전+큰 점프로 판정). 플롯/저장 버퍼는 분리 — 수동 'Clear plot'은 표시만 비우고
    저장 버퍼는 보존한다. 플롯 상단에 **`log x`·`log y` 체크박스**로 축 로그 스케일 전환(뷰 범위도
    log10로 맞춤, 프로파일에 저장).
    ※ 'X Noise 1Hz BW' 등은 Sweeper 모듈이 계산하는 값이라 `getDouble` 노드가 없다 → **noise
    node를 비우면 noise 열을 생략**하고(권장), LabOne이 저장한 noise-vs-freq와 우리 로그를
    **주파수로 병합**한다. `/oscs/0/freq` 노드를 폴링하는 방식이라 주파수 변경 직후 정착 중 값을
    잡을 수 있으니 poll 간격을 넉넉히.
  - 공통: **한 frequency sweep = `.dat` 파일 1개**(헤더 2줄: 라벨/단위). 긴 측정에서 Stop 전에
    데이터가 날아가지 않도록, wrap(새 sweep 시작)을 감지할 때마다 직전 sweep을 `filename_xNNN.dat`
    (번호 클수록 나중)로 **즉시 저장**하고 버퍼를 비운다. 마지막 부분 sweep은 Stop/종료 시 저장.
    driven 모드는 단조 sweep 1개라 종료 시 파일 1개. 보조값은 `aux_reads` 목록으로 alias·명령·
    라벨·단위를 사용자 편집 가능하며, 물리 리소스가 달라 병렬 읽기 안전.
  - 기본 aux 명령을 사전 세팅: ITC `READ:DEV:DB8.T1:TEMP:SIG:TEMP`, M81 `FETCh:SENSe{M}:DC?`.
    명령 안의 **`{M}`**은 창의 'M =' 값으로 전송 직전 치환(예: M=2 → `FETCh:SENSe2:DC?`). 기존
    프로파일의 옛 placeholder(빈칸/`R1`)는 로드 시 기본 명령으로 자동 시드.
- **LabOne sweep 병합** (`core/mfli_merge.py` + MFLI 창 "Merge with LabOne sweep" 섹션): LabOne
  sweeper CSV와 우리 측정을 각각 sweep으로 분리해 순차 짝짓는다. 우리 측정 입력은 **단일 `.dat`(주파수
  wrap으로 분리)와 per-sweep 저장 폴더(`test_xNNN.dat` 여러 개, 파일당 1 sweep) 둘 다** 지원
  (`iter_our_sweeps`; 창의 "측정 폴더/.dat"는 폴더 선택). **유효한 sweep만** 골라 짝짓는다 — LabOne은
  (측정 범위와 겹침 + full grid + 노이즈 유효)한 sweep, 우리는 완전한 segment만; 제외 내역·개수 불일치는
  경고, 매칭 sweep이 없으면 거부. 결과는 **LabOne grid 전체(예: 200점)를 기준 행**으로 하고, 각 grid 점에
  같은 주파수의 우리 측정을 붙인다(**rank가 아니라 '로그-주파수 최근접' 매칭** — 우리 주파수는 grid의
  **부분집합**이라 rank로 짝지으면 poll로 놓친 점 때문에 통째로 밀린다; 실측 rank ~0.9 decade vs 주파수매칭
  ~1e-8). **우리가 안 읽은 grid 점은 `Module_frequency`·aux(`probe_T`/`M81_V`)를 NaN**으로 채운다(노이즈
  x/y/r/X_noise/R_noise/NEPBW는 LabOne 값이라 grid 전 점에 존재 → 예: 200점 grid를 159점만 잡으면 200행 중
  41행이 aux=NaN). 하나로 보간하지 않고 **`LabOne_frequency`·`Module_frequency` 두 열을 모두** 내보낸다
  (겹치는 행은 둘이 거의 같아야 정상). **Noise level 컬럼**(`X_noise/R_noise = stddev/√NEPBW`) + x/y/r/NEPBW
  + 온도·M81 등을 담아 `sweep_N_merged_data.dat`로 저장(이전 잔여 파일은 정리). numpy/Qt 비의존이라
  `python -m core.mfli_merge …` CLI로도 실행.
  **대규모(≈1000 sweep) 대응**: 우리 파일은 **숫자(natural) 정렬**로 읽는다(`_num_key`) — 문자열 정렬은
  `x1000`을 `x101` 앞에 두어 1000개+에서 순서를 망친다. LabOne은 parse 순서(파일 이름순 + append순
  = 시간순)를 그대로 쓴다(chunk 번호로 재정렬하지 않음 — 큰 CSV가 `_00001.csv`로 롤오버하며 chunk 번호를
  재시작해도 파일순이 이를 흡수). 검증: 1000 sweep 병합 ~12초·peak 42MB, 순서/주파수/noise 전부 정확.
  단, 대응은 여전히 **양쪽 sweep이 1:1 시간순**이라는 전제 — 한쪽에서 sweep이 비대칭으로 빠지면(예:
  LabOne 첫 sweep 노이즈 미계산 → 제외) 이후가 1칸 밀린다. 이때 **"개수 불일치" 경고**가 뜨므로,
  경고가 없고 paired 수가 기대와 같으면 정렬이 맞다고 보장된다.
- **noise**: LabOne에서 구성한 스칼라 PSD/noise 노드를 읽음(클라이언트측 통계 없음).
- **통합**: main_window View 메뉴 "MFLI Noise Sweep…"(Ctrl+Shift+F), 싱글턴 런처, 프로파일
  전환/저장 훅(`on_profile_changed`/`_save_ui_state`), 종료 시 `shutdown_threads()`를
  `session.shutdown()` 이전에 호출. 설정은 `SETTINGS_DIR/profiles/mfli/{name}.yaml`.
- **의존성**: `zhinst`(LabOne Python API) 추가.

### ■ 리뷰로 잡은 결함 수정 (다중 에이전트 감사)

- 창을 sweep 중 닫아도 워커가 방치/파괴되지 않도록 `closeEvent`→ignore+저장+숨김(Esc 닫기 차단).
- 중복 aux 라벨이 플롯 data dict·소스 콤보에서 서로 덮어써 곡선이 사라지던 문제 → 라벨 유일화(_2…).
- View 메뉴 단축키 충돌(Ctrl+Shift+M가 Meta Data와 겹침) → MFLI는 Ctrl+Shift+F.
- 프로파일 전환 시 `_extra_cols` stale 캐시로 플롯 소스 복원이 조용히 실패하던 문제 → 항상 재계산.
- 드라이버 `{dev}` 치환이 `//devN` 이중 슬래시를 만들던 버그 수정.

---

## v1.06.0 — 2026-07-14

Double Sweep(VNA·레거시) second 채널 값 테이블 편집 + field-time 워크플로 옵션 2종.

### ■ Feature 1 — Second 값 테이블 편집 창 (VNA + 레거시 공용)

- **스레드 안전 모델**(`gui/second_channel_model.py`): second(array) 값 배열의 단일
  source of truth. 순수 파이썬 + `Lock`(Qt 미사용)이라 측정 워커 스레드에서 매 행 값을
  '새로' 읽어도 안전. 행 상태 `PENDING/CURRENT/DONE`을 하나의 락으로 직렬화.
- **편집 창**(`gui/second_channel_table_window.py`): 값·상태를 표로 표시. **완료(DONE) 행은
  잠금**(회색), **측정 중(CURRENT) 행은 색상**(파랑), **미래(PENDING) 행은 측정 중에도
  값 수정·행 추가·삭제 가능**. 워커는 창/위젯을 직접 만지지 않고, 워커→GUI는 bound
  `@Slot`(`_on_second_row`/`_begin_array_index`)으로만 갱신.
- **디폴트/커스텀**: 시작 시 Start/Stop/N(레거시는 From/To/Step)으로 테이블을 재생성하고
  배열을 만든다. **"테이블 초기화 안 함" 체크박스**를 켜면 사용자가 넣은 커스텀 테이블
  그대로 측정(`rearm`). 테이블은 세션 한정(프로파일에 미저장).
- **완료 prefix 불변식**: DONE 행은 항상 앞쪽 prefix에 모여 이동/삭제되지 않아, 미래 행을
  추가/삭제해도 워커의 현재 위치(CURRENT) 이후만 바뀌므로 안전. 재개 시 완료분을
  `reset_from(vals, done_prefix=n)`으로 DONE 시딩.
- **VNA·레거시 동일 모델/창 재사용**: VNA는 `_run_double_sweep` 워커가 `value_at(gidx)`를
  전역 인덱스로 읽고, 레거시는 상태머신이 array 경계마다 `_begin_array_index()`로 모델을
  다시 읽어 편집/추가를 반영.

### ■ Feature 2 — "기다리지 않고 현재 온도에서 먼저 sweep" (VNA field-time)

- Double Sweep with Time에서 첫 second(온도) 목표 도달을 기다리지 않고 **현재 온도에서
  정상 field sweep을 1회 선행**한 뒤 T1 도달→sweep→T2 도달→sweep… 로 이어감. 첫 실행
  (offset==0)에만 적용, 재개 시에는 미적용. 선행 sweep은 `<figure>_initial` 하위폴더에 저장.

### ■ Feature 3 — dummy(복귀) 구간도 측정 (VNA field-time)

- 기존에는 First 채널의 시작점 복귀(dummy 램프)를 측정하지 않았으나, 체크 시 복귀 램프
  동안에도 acquire하여 `<second>_dummy` 하위폴더에 저장. 복귀 후 원래 second 폴더로 복원.

### ■ 지속성 / 재개

- 3개 체크박스(`second_use_custom_table`·`field_initial_sweep`·`dummy_measure`)를
  `VnaDoubleSweepControl`에 추가해 프로파일에 저장·복원.
- second 완료 시 라이브 모델 값으로 resume `second_values`를 재동기화 → 측정 중 편집·추가한
  미래 행이 재개 목록에도 반영.

---

## v1.05.0 — 2026-06-18

대규모 안정성·정합성 강화 + VNA Double Sweep 기능 확장. 2회의 다차원 크래시/버그 감사
(에이전트 ~97개)로 발견·검증한 항목을 반영.

### ■ VNA Double Sweep 기능 확장

- **시작점 복귀(go-to-start)**: 단방향 sweep이 매 회 시작점(예: 0 T)으로 controlled 복귀한
  뒤 측정하도록 함. field-time·standard 경로 모두 적용. 첫 회 초기 이동도 포함되어 자기장이
  엉뚱한 지점(예: 1.2 T)에서 시작되던 문제 해결.
- **pre-advance 타이밍 정리**: 매 sweep 직전 `dummy 속도 → 시작점 복귀 → sweep 속도 → 측정`
  순서로 적용. sweep/dummy 단계별 자기장 ramp 속도(RFST)를 정확히 제어.
- **새 advance 타입 `Threshold + Time`**: 목표 band 도달 후 std 안정화 대신 고정 시간 대기.
  목표가 0일 때 std 정규화가 과민해지는 문제를 회피. VNA·레거시 Double Sweep 양쪽 UI 지원.
- **FEEDBACK 도달 판정 개선**: 비율 대신 `band = max((1-tol%)·denom, noisefloor)` 절대
  허용오차. 시작점이 목표에 매우 가까운 작은 이동(예: 120.997→121)에서도 도달 판정 가능,
  overshoot 조기 도달 오판 제거.
- **3컬럼 레이아웃**: `[Sweep/Acquire 설정] | [섹션 Execute] | [플롯]`.
- **second 값별 하위폴더 저장**: `filename_xxx/<second값>/...dat` 구조.
- **진행 phase 세밀 표시**: second/First 카운터, ①복귀 ②ramp ③측정 ④HOLD, acquire 내부
  (OPC 대기·곡선 읽기), feedback '도달 중' 실시간 값.
- **Stop 시 실행 명령**: 측정 중단/오류 시 등록된 명령(예: 자기장 HOLD) 자동 전송.
- **Resume(재개)**: 매 second 완료 시 재개 상태를 프로파일 옆 파일에 저장 → 사용자 중단·오류·
  크래시 후 마지막 second 값부터 같은 폴더에 이어서 측정.
- **sweep 중 버튼 비활성화**: 섹션 Execute·Config·Alarm 등 충돌 유발 버튼 비활성화.

### ■ 안정성 — 네이티브 크래시 제거

- **워커 스레드 GUI 접근 차단**: 워커 신호를 lambda가 아닌 bound `@Slot`으로 연결(자동
  메인스레드 큐잉). lambda 연결이 워커 스레드에서 GUI(QLabel)를 만져 access violation으로
  무작위 종료되던 근본 원인 제거. (VNA·레거시 Double Sweep 양쪽)
- **세션 eviction race 제거**: 통신 오류 시 `viClose`를 데몬 스레드로 던져 워커의 자동
  재오픈(`viOpen`)과 같은 리소스에서 동시 실행돼 힙 손상(0xC0000374)되던 문제 →
  alias 락 내 **동기 disconnect** + `open()` 직렬화.
- **종료 시 use-after-free 방지**: 앱 종료가 VNA 워커를 정지·대기한 뒤 ResourceManager를
  닫도록(`shutdown_threads()`), 세션에 `_shut_down` 가드, 드라이버 `disconnect`에서
  `inst=None`. 워커 QThread parent 제거 + `_sec` 워커 스레드 affinity 정리.
- **플롯 비유한값 방어**: `setData`의 `skipFiniteCheck` 제거 + NaN/Inf→gap, 범위 계산
  유한값만, step 슬롯 try/except. 드라이버 drain 루프 상한.

### ■ 런어웨이/행 방지

- **`sweep_rate ≤ 0` / `time_per_point ≤ 0`**: 무한 정지(rate=0)·역방향 폭주(rate<0)를
  명시적 오류로 차단(`calculate_next_step`이 크기는 abs, 방향은 목표차로). 메인·더블·VNA·
  second 채널 전부 적용 + 시작 전 GUI 검증.
- **feedback/threshold 폴링**: `sleep`→`stop_event.wait`(Stop 즉시 반영), 비유한값 5연속·
  통신오류 3연속이면 20~40분 워치독 대신 조기 에스컬레이션.

### ■ 데이터 정합성

- **Resume 세션 초기화**: 단일 sweep 재개 시 미분 reset/reconfigure·메타데이터 configure·
  컬럼 동기화를 수행(미분이 Stop 불연속을 가로질러 계산되거나 메타 버퍼가 어긋나던 문제).
- **`_parse_float` 강화**: 복합/모호 응답(`12.5/3.0`, `1.2.3`, `3 of 5`)에서 잘못된 값
  추출 대신 에러. 정상 `값[단위]`는 통과.
- **DataSaver 파일명**: 자동번호 파일을 배타 생성(`'x'`)+재시도 → 두 프로세스 동시 덮어쓰기 방지.
- **드라이버 버퍼 동기화**: Mercury(iPS/iTC) write 후 ack 확실히 읽기 + query 전 stale 입력
  flush → 자기장이 0/NaN으로 찍히거나 거짓 HOLD가 잡히던 desync 제거.

### ■ 병렬 측정

- **통신 락을 물리 리소스(주소) 단위로**: 같은 장비를 가리키는 두 alias가 병렬에서 동시
  통신해 응답이 섞이던 데이터 손상 차단(서로 다른 장비는 그대로 병렬).
- 측정 단계가 Stop을 빠르게 반영(early-return), `run_step`이 큐 대기 중 Stop을 삼키지 않음.

### ■ UI/보안/기타

- 더블스위프 second 채널 라디오 재빌드를 IDLE에서만(실행 중 무효화로 advance가 죽던 문제).
- 알람(winsound.Beep)을 데몬 스레드로 오프로드 → GUI ~1초 프리즈 제거.
- 텔레그램 TLS 인증서 검증 복구(토큰 MITM 노출 차단), 전송 실패를 로그로 기록.
- 닫힌 alias 접근 시 `KeyError` → 명확한 `ConnectionError`.
- sweep 파라미터를 측정 중 편집해도 현재 위치 보존(불필요한 readback/점프 방지).

### ■ 로깅/진단

- **`core/applog.py` 신설**: faulthandler(네이티브 크래시 스택) + 메인·워커 스레드 excepthook +
  Qt 메시지 핸들러 + RotatingFileHandler. 로그 위치 `settings/logs/`(app.log, fault.log).
  평소 오버헤드 거의 없음. 이 로깅으로 무작위 종료의 근본 원인(GUI in worker thread)을 포착.

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
