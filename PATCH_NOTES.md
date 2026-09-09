# Patch Notes

---

## v1.07.5 — 2026-09-09 (기능 추가)

### VNA Calibration 창

VNA Control 의 **Config 옆 [⚙ Calibration]** 버튼으로 연다. VISA Library 에 등록해 둔
명령을 고르면 **그 항목 하나가 버튼 하나**가 되고, 누르면 그 명령을 바로 보낸 뒤
전역 OPC 응답이 올 때까지 **모든 버튼을 잠근다**. 교정은 한 단계가 끝나기 전에 다음
단계를 누르면 안 되기 때문이다.

    [1port short] 클릭 -> short 교정 명령 전송 -> OPC 대기 -> 잠금 해제
    [1port open]  클릭 -> ...

- `gui/vna_calibration_window.py` — 버튼 격자(한 줄 3개), 상태줄, OPC 표시.
  버튼 라벨은 라이브러리의 figure_axis(없으면 description)를 쓴다. 값이 필요한
  명령이면 버튼 옆에 입력칸이 붙는다.
- `gui/vna_calibration_editor.py` — [버튼 구성…] 다이얼로그. 명령 편집 UI 는 VNA
  Config 의 `_CmdListWidget` 을 **재사용**한다(같은 일을 하는 목록을 두 벌 만들면
  한쪽만 고쳐진다). ↑ ↓ 순서가 버튼 배치 순서다.
- **OPC 는 전역 설정** — 모든 버튼이 같은 완료 대기 쿼리를 쓴다. 비워 두면 대기 없이
  바로 끝난다.
- **설정은 프로파일이 아니라 전역**으로 저장한다. 교정 절차는 장비의 성질이지
  측정 프로파일마다 달라지는 값이 아니다 → `SETTINGS_DIR/vna_calibration.yaml`
  (규칙 3: 사용자 설정은 Documents 아래).

실행은 섹션 실행과 **같은 워커·같은 잠금**을 쓴다(`_VnaWorker` + `_set_section_busy`).
같은 VISA 세션으로 같은 장비를 건드리므로, 교정 중에는 측정 Start 와 섹션 Execute 도
함께 잠긴다. 반대로 **측정 중에는 교정을 시작할 수 없다** — 이유를 상태줄에 알린다.

헤드리스 검증: 라이브러리에 short/open/load + `*OPC?` 를 등록하고 ① 전역 파일 저장·로드
② 버튼 3개가 figure_axis 라벨로 생성 ③ [1port short] 클릭 -> `SENS:CORR:COLL:SHOR1`
전송 -> OPC 3회 폴링 -> 그동안 버튼·Start 모두 비활성 -> 완료 후 해제
④ 측정 중 교정 요청은 거부 확인.

---

## v1.07.4 — 2026-09-09 (기능 추가)

### VNA Section 에 OPC(완료 대기) 추가

섹션은 묶음 명령을 보내기만 하고 바로 끝났다. 장비가 그 명령을 아직 처리 중인데
측정을 시작하면 이전 상태의 값이 섞여 들어온다.

- `VnaSectionConfig.opc_cmds` 추가 — 섹션마다 완료 대기용 query 를 등록한다.
  기존 설정 YAML 은 기본값(빈 목록)으로 채워져 그대로 열리고, 비워 두면 예전처럼
  명령만 보내고 끝난다.
- **VNA Config → Sections 탭**에 `OPC (완료 대기 — query)` 목록이 생겼다.
  Acquire 탭의 Wait 단계와 같은 규칙으로 동작한다.
- **실행 순서**: 섹션의 write 명령을 전부 보낸 뒤 → OPC 쿼리를 폴링(0.1초 간격)해
  응답이 `1` 이 될 때까지 대기 → 완료.
- **대기 중에는 측정 Start(Single/Sweep)와 모든 섹션 Execute 버튼이 잠긴다.**
  상태줄에 `완료 대기 중 (OPC)…` 이 표시된다.
- 장비가 끝내 `1` 을 주지 않으면 창이 영영 잠기므로 **10분 상한**을 두고, 넘으면
  마지막 응답과 명령을 담은 오류로 끊는다. 창을 닫을 때도 `stop()` 으로 대기를 끊는다
  (`shutdown_threads`).

### 함께 정리

- acquire 잠금과 섹션 잠금이 서로 덮어쓰던 문제 — `_set_acquire_busy` 와
  `_set_section_busy` 가 각자 상태를 두고 `_apply_busy_state()` 에서 합쳐 적용한다.
  예전엔 섹션 실행이 끝나면서 acquire 중인 버튼까지 풀릴 수 있었다.

헤드리스 검증: 가짜 계측기가 4번째 쿼리에 `1` 을 주도록 두고 ① Config 에 OPC 를
등록·저장·재로드 ② Execute → `*RST` 전송 후 OPC 4회 폴링 ③ 실행 중
`Start=False, Execute=False` → 완료 후 둘 다 `True` 확인.

---

## v1.07.3 — 2026-09-09 (기능 추가 · 버그픽스)

### VISA Library 변경을 쓰는 곳 전부에 다시 적용

라이브러리에서 명령을 고쳐도 **활성 프로파일의 main_ui 만** 갱신됐다. 다른 프로파일은
옛 명령을 든 채 남아, 나중에 그 프로파일로 전환한 사용자가 조용히 틀린 명령으로
측정하게 됐다. 파라미터가 안 맞을 때의 처리도 위험했다 —

- 측정 항목: `entry.cmd_query.format(**fill_params)` 가 `KeyError` 로 실패하면
  **치환되지 않은 템플릿을 그대로** `resolved_cmd` 에 넣었다. `{ch}` 가 남은 문자열이
  장비로 나간다.
- sweep/write: 조용히 **옛 명령을 유지**해서, 라이브러리를 고쳤는데 반영이 안 된
  것처럼 보였다.

바뀐 동작:

- `ProfileRegistry.propagate_library_change()` 신설 — 라이브러리 저장 시
  **모든 프로파일**을 재인스턴스화해 저장한다.
- `check_placeholders()` 신설 — 라이브러리 템플릿의 placeholder 를 그 항목의
  `fill_params` 로 채울 수 있는지 판정한다. 비워 두는 자리 수(sweep 축)를
  `axis_slots` 로 받는다: 측정 0, sweep value 1, write cmd 는 기존 명령의 `{v}` 유무.
- 맞으면 명령·figure_axis·unit 을 갱신하고, **안 맞으면 옛 명령을 그대로 둔 채**
  `needs_fix` 에 사유를 남긴다. 반쯤 치환된 명령은 절대 만들지 않는다.
- `Instantiated{Measurement,SweepValue,WriteCmd}` 에 `needs_fix: str` 추가
  (빈 문자열 = 정상). 기존 프로파일 YAML 은 기본값으로 채워져 그대로 열린다.

UI:

- 못 쓰게 된 항목은 메인 창에서 **비활성화**된다 — 측정 체크박스는 체크 해제 후
  잠기고(`checked=False` 로 프로파일에도 반영), sweep 채널 라디오는 선택할 수 없다.
- 빨간색 + `⚠` 표시로 구분되고, **마우스를 올리면** 무엇이 어떻게 달라졌는지와
  "Parameter Manager 에서 이 항목을 지우고 다시 등록하면 해결됩니다" 안내가 뜬다.
- 라이브러리 저장 직후, 프로파일별로 어떤 항목이 막혔는지 요약 다이얼로그와
  Console 로그로 알린다.

헤드리스 검증: 프로파일 2개(A·B)에 같은 명령을 등록한 뒤 ① 파라미터 수가 같은 변경 →
양쪽 모두 새 명령으로 갱신 ② 파라미터가 늘어난 변경 → 양쪽 모두 옛 명령 유지 +
needs_fix + 체크박스/라디오 비활성화 + 툴팁 확인.

### 알람 Test 의 인증서 오류

- **[중요] Telegram/이메일 Test 가 `CERTIFICATE_VERIFY_FAILED: self-signed certificate
  in certificate chain` 으로 실패했다** — MITM 이 아니었다. 같은 서버를 Windows 자체
  검증기로 확인하면 정상(`SslPolicyErrors.None`)이고 GoDaddy 루트도 저장소에 있는데,
  Python 의 `ssl.create_default_context()` 만 실패했다. Windows 는 필요한 루트를 처음
  쓸 때 자동으로 내려받는데(automatic root update), Python 은 **그 시점의 저장소
  스냅샷**을 OpenSSL 에 넘기므로 아직 받아오지 않았으면 체인을 못 세운다. 그래서
  '가끔 된다'로 보였다.
- `core/net_ssl.py` 신설 — 검증은 켜 둔 채 검증 주체만 옮긴다:
  `truststore`(Windows 자체 검증 API 위임) → `certifi` 번들 → 표준 라이브러리 순.
  Telegram(`urlopen`)과 이메일(`smtplib.starttls`) 모두 이 컨텍스트를 쓴다.
  두 패키지는 선택 의존성이며 `pythonization.bat` 이 자동 설치한다.
- 그래도 실패하면 `explain_ssl_error()` 가 원인과 조치(패키지 설치 / 사내 CA 설치 /
  시스템 시각 확인)를 담은 안내를 Test 결과에 보여 준다.
- **인증서 검증은 끄지 않는다.** 예전에 `CERT_NONE` 으로 껐다가 봇 토큰이 MITM 에
  노출된 전례가 있다.

---

## v1.07.2 — 2026-09-09 (성능 · 버그픽스 · 저장소 정리)

### 측정 중 GUI 지연

- **[치명] 측정 내내 테두리 glow 애니메이션이 GUI 스레드의 25~43%를 먹고 있었다** —
  `gui/main_window.py` 의 `_glow_frame` 은 **centralWidget** 이라 그 안에 UI 전체(자식
  위젯 369개)가 들어 있는데, `_update_glow()` 가 30 ms 마다 `setStyleSheet()` 을 다시
  지정했다. Qt 는 스타일시트가 바뀌면 그 위젯과 **모든 자식**의 스타일을 재계산·polish
  한다. 실측 갱신 7.55 ms + 리페인트까지 12.89 ms 로, 30 ms 주기 중 최대 12.89 ms 를
  애니메이션이 차지했다. 스텝 처리 타이머와 glow 타이머가 독립이라 두 틱이 겹칠 때만
  스텝이 밀려 **'간헐적'으로만** 드러났다.
  → 공용 위젯 `gui/glow_frame.py` 를 신설. 스타일시트는 최초 1회(투명 테두리로 여백
  확보)만 쓰고 빛나는 테두리는 `paintEvent` 에서 직접 그린다. 갱신 시 인자 없는
  `update()` 는 자식 전부를 다시 그리므로 **테두리 띠 영역만** 무효화한다.
  `12.89 ms → 0.33 ms` (39배). 같은 패턴이던 `gui/double_sweep_window.py` 도 함께 교체.
- **Sweep/Console 로그가 버퍼 상한에 닿으면 한 줄당 3.2 ms** — `gui/debug_window.py`
  `_append()` 가 `moveCursor` + `insertHtml` 에 직접 만든 트림 루프까지 돌렸다.
  로그가 짧을 땐 0.36 ms 지만 상한(2000줄) 도달 후 평균 3.19 ms, 최대 15.2 ms.
  Verbose 를 켜면 매 스텝 호출된다. → `QTextDocument.setMaximumBlockCount()` 로 트림을
  맡기고 `append()` 한 번으로 대체. `3.19 ms → 0.029 ms` (110배). `_trim()` 과
  `_line_counts` 장부 제거.
- **`.dat` 한 줄마다 `os.fsync()` 로 물리 디스크 동기화** — `core/data_saver.py`
  `append_row()`. 로컬 SSD 에서도 평균 0.83 ms, 최대 7.9 ms 이고 네트워크·USB 드라이브면
  훨씬 커진다. → `flush()` 는 매 행 유지(프로세스가 죽어도 OS 캐시에 남는다),
  `fsync` 만 `_FSYNC_INTERVAL_S`(1초) 간격으로. `0.83 ms → 0.27 ms`.
  **주의:** 정전·BSOD 시 최대 1초분 행이 유실될 수 있다(프로세스 크래시는 영향 없음).

30 ms 주기당 GUI 점유 **43% → 1.1%**. 측정값은 헤드리스로 렌더링을 이미지로 떠서 확인
(glow 켤 때 테두리 픽셀만 바뀌고 내부는 그대로, 끄면 복귀).

### VNA

- **[중요] VNA Config 의 [Save & Apply] 가 VNA Control 설정을 통째로 되돌렸다** —
  `gui/vna_config_window.py` 의 `VnaConfigWindow` 는 보관형인데 **생성 시 한 번만**
  디스크를 읽었고(`set_config_path()` 는 경로가 바뀔 때만 재로드), `_on_save()` 는
  `self._cfg` **전체**를 파일에 쓴다. Config 를 열어둔 채 Control 에서 값을 바꿔 저장한
  뒤 Config 에서 저장하면 옛 스냅샷이 덮어썼다. 되돌아가는 값은 섹션 명령 체크박스만이
  아니라 명령 입력값·단위·sweep role(start/stop/n_points)·저장 폴더·플롯 설정·acquire
  sweep 범위·time mode 전부다.
  → `reload_from_disk()` 신설, `set_config_path()` 는 항상 재로드, `showEvent` 에서도
  재로드. `gui/vna_window.py:_open_config()` 는 Config 를 띄우기 전에 `_save_ui_state()`
  로 현재 상태를 파일에 먼저 반영한다. 재로드 시 `_cur_sec_idx` 가 −1 로 남아 **선택된
  섹션의 편집분이 저장에서 통째로 누락되던** 문제(같은 행이면 `setCurrentRow` 가 신호를
  내지 않음)도 함께 수정.
- **VNA Config 의 Sections 탭에서 On/Off 버튼 제거** — `VnaCommandEntry.enabled` 라는
  **한 개의 필드**를 Config 의 [On/Off] 버튼과 Control 의 체크박스가 각자 고치고 있었다.
  이제 섹션 명령의 활성/비활성은 **VNA Control 체크박스가 단독 소유**한다. 토글할 수
  없는 목록에서는 회색·`OFF` 표시도 하지 않는다(끌 수 없는 자리에 OFF 만 보이면 어디서
  켜는지 알 수 없다). **Acquire 탭의 Start/Wait/Read 는 그대로 유지** — 이쪽은 Control
  에 체크박스가 없어 Config 가 유일한 토글이다.
- **VNA Control 창이 최대화 상태로 열린다** — `gui/vna_window.py:1279`. `resize(1500,
  780)` 은 최대화를 푼 뒤의 복원 크기로 남겼다. `setWindowFlags()` 가 window state 를
  초기화할 수 있어 반드시 그 뒤에서 지정한다.

### 저장소 정리

- **`.gitignore` 신설** — 원래 아예 없어서 `.pyc` **172개**가 추적되고 있었고 `git status`
  가 매번 수십 줄로 덮였다. 추적 해제(디스크 파일은 유지).
- **`pythonization/` · `tests/` 껍데기 삭제** — `refactor/restructure` 브랜치에서
  전환할 때 남은 잔해. `.py` 파일 0개에 `__pycache__` 106개와 루트와 내용이 동일한
  `app_config.yaml` 1개뿐이었다. 실제 소스는 해당 브랜치에 그대로 있다.
- `_diff_vna.txt`(94 KB `git diff` 덤프) 삭제, `build/`(PyInstaller 중간 산출물) 추적 해제.
- `version_info.txt` 를 PATCH_NOTES 버전에 맞췄다 — `1.0.0.0` 에 멈춰 있어 exe 버전이
  실제 버전과 달랐다.

### 개인정보 분리

- **[중요] `app_config.yaml` 이 공개 저장소에 추적되고 있었다** — 이 파일에는
  `smtp_password`·`telegram_bot_token`·`smtp_user`·`email_to`·`telegram_chat_id` 가
  들어가고, Settings → Config 에서 저장하면 `save_app_config()` 가 **프로그램 폴더**
  (`core/app_dirs.py` 의 `GLOBAL_CONFIG_PATH`) 에 그대로 쓴다. 즉 알람용 이메일·텔레그램을
  설정하는 순간 자격증명이 커밋 대상이 됐다. 이력 전체를 확인한 결과 **값이 올라간 적은
  없다**(모든 버전에서 빈 문자열). 값이 비어 있는 지금 `.gitignore` 에 넣어 추적을 끊고,
  형식만 담은 `app_config.example.yaml` 을 대신 추적한다. 디스크의 실제 파일은 그대로
  두므로 동작 변화는 없고, 파일이 없으면 `load_app_config()` 가 `AppConfig()` 기본값으로
  시작한다.
- **[중요] `dist/` 배포본 안에 실제 랩 설정이 들어간 채 추적되고 있었다** —
  `dist/Pythonization/_internal/settings/` 의 `instruments.yaml`(장비 IP·MAC),
  `profiles/SangIl.yaml`·`default.yaml`·`test.yaml`, `visa_libraries.yaml`,
  `active_profile.txt` 등. 배포본은 빈 상태로 나가야 한다.
  → `dist/` 전체를 추적 해제하고 `.gitignore` 에 넣었다. 앞으로 배포본은 저장소가 아니라
  **GitHub Releases** 로 올린다. 디스크의 `dist/` 와 exe 는 그대로 두므로 빌드·실행에는
  영향이 없다. 추적 파일이 **1027개 → 77개**(약 172 MB 감소)로 줄었다.
  **주의:** 이 파일들은 git *이력*에는 남아 있다. 완전히 지우려면 이력 재작성
  (`git filter-repo`)과 강제 푸시가 필요하며, 저장소를 공유 중이면 협의가 필요하다.

### 문서

- **`READ_FIRST_BEFORE_CODING.md` 신설** — AI 코딩 도구로 작업하기 전에 읽는 지침.
  ① 수정사항은 PATCH_NOTES 에 반드시 기입 ② VISA 명령어는 라이브러리에 등록한 뒤에만
  사용(코드에 문자열 박기 금지, `InstrumentSession` 이 유일한 I/O 창구, 장비 고유
  프로토콜은 `driver/` 안에서만) ③ 개인정보·사용자 설정은 저장소가 아니라
  `SETTINGS_DIR`(`~/Documents/pythonization/settings`) 에 두고 저장소에는 기능만 담는다
  ④ 이를 어기는 요구가 오면 그대로 따르지 말고 더 나은 설계를 먼저 제안
  ⑤ 요청한 수정이 끝나고 검증되면 커밋·푸시까지 한다.
- **`CLAUDE.md` 신설** — 위 지침의 요약. Claude Code 가 매 세션 자동으로 읽는 파일이라
  지침이 자동 적용되게 하는 진입점이다.

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
