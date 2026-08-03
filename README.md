# Pythonization

저온·자기장 환경에서 시료 물성을 측정하는 계측 자동화 프로그램 (PySide6 + VISA).

계측기를 sweep 하며 측정값을 읽어 `.dat` 로 저장하고, 실시간 그래프·알람·중단 후
재개를 제공한다. VNA / MFLI noise sweep / Double Sweep 은 각자 독립 창으로 동작한다.

---

## 실행

```
pythonization.bat          # .venv 생성 + 필수 패키지 설치 + 실행 (Windows)
```

직접 실행하려면:

```
pip install -r requirements.txt
python main.py             # 또는  python -m pythonization
```

선택 패키지(없어도 동작한다):

```
pip install -r requirements-optional.txt
```

| 패키지 | 없을 때 |
|---|---|
| `zhinst` | MFLI 창에서 연결 시 안내 메시지. 나머지 기능은 정상 |
| `scipy` | 미분 채널이 Savitzky-Golay 대신 다항식 회귀로 자동 fallback |

**처음 사용하는 순서**: 장비 등록 → VISA 라이브러리 작성 → Parameter Manager 구성
→ 메인 창에서 Sweep. 자세한 절차는 [docs/USER_MANUAL.md](docs/USER_MANUAL.md).

---

## 어디에 무엇이 있나

### 문서

| 찾는 것 | 파일 |
|---|---|
| 프로그램 사용법, 창별 조작 | [docs/USER_MANUAL.md](docs/USER_MANUAL.md) |
| 코드 구조, 모듈 의존성, 스레드 규칙 | [docs/STRUCTURE.md](docs/STRUCTURE.md) |
| 버전별 변경 이력 | [docs/CHANGELOG.md](docs/CHANGELOG.md) |

### 코드

```
pythonization/
├── app/           앱 기동 — 진입점(bootstrap), 경로 확정(paths), 로깅(logging_setup)
├── config/        설정 데이터 모델(models) + 전역 설정 IO(app_config)
├── instruments/   계측기 — 세션(VISA I/O 허브), 팩토리, 레지스트리, 파라미터, 오류 분류
│   └── drivers/   장비별 드라이버 (vendor_model.py 규칙)
├── measurement/   측정 엔진 — sweep 계산, 워커 스레드, .dat 저장, 미분 채널, 재개 로그
├── profiles/      명명된 사용자 프로파일 저장/로드
├── analysis/      측정 후처리 (Qt 비의존 — CLI 로도 실행 가능)
├── notify/        알람 — 사운드·이메일·텔레그램
├── util/          도메인 비의존 유틸리티
└── ui/            PySide6 GUI
    ├── main_window.py   메인 창
    ├── widgets/         여러 창이 공유하는 위젯 (플롯 패널 등)
    ├── dialogs/         설정·구성 다이얼로그
    ├── panels/          측정 중 띄우는 보조 창 (그래프·데이터·타이밍·디버그)
    ├── modules/         독립 측정 모듈 창 — vna / mfli / double_sweep
    └── assets/          이미지 등 정적 자산
```

"이 기능은 어느 파일인가"는 [docs/STRUCTURE.md](docs/STRUCTURE.md) 의 디렉토리 레이아웃
표에 파일별 한 줄 설명이 있다.

### 실행 중 생기는 파일 (저장소 밖)

기본 위치는 `%USERPROFILE%\Documents\pythonization\settings\` 이고,
Settings → Config 의 `data_dir` 로 바꿀 수 있다(재시작 후 적용).

| 경로 | 내용 |
|---|---|
| `settings/logs/app.log` | 실행 로그 + 미처리 예외 트레이스백. **오류 신고 시 이 파일** |
| `settings/logs/fault.log` | 네이티브 크래시(segfault) 스택 덤프 |
| `settings/instruments.yaml` | 등록한 계측기 연결 설정 |
| `settings/visa_libraries.yaml` | VISA 명령어 라이브러리 |
| `settings/profiles/{name}.yaml` | 명명된 프로파일 |
| `settings/active_profile.txt` | 마지막으로 쓴 프로파일 이름 |
| `settings/resume_points.json` | 중단된 측정의 재개 지점 |

측정 데이터(`.dat`)는 메인 창의 Save Settings 에서 지정한 폴더에 저장된다.

프로그램 폴더의 `app_config.yaml` 은 전역 설정(측정 임계값·병렬 측정·알람 전송 수단)이다.
SMTP 비밀번호와 텔레그램 토큰이 들어가므로 **저장소에 커밋하지 않는다**
(`.gitignore` 대상). 형식은 [app_config.example.yaml](app_config.example.yaml) 참고.

---

## 테스트

표준 라이브러리 `unittest` 만 쓰므로 추가 설치 없이 어느 랩 PC에서도 돌아간다.

```
python -m unittest discover -s tests -t .
```

`pytest` 가 설치돼 있으면 `pytest tests` 로도 실행된다.

| 파일 | 지키는 것 |
|---|---|
| `test_imports` / `test_import_targets` | 모든 모듈과 모든 import 문(함수 내부 포함)이 해석되는지 |
| `test_windows` | 메뉴가 여는 창 10개가 실제로 생성되는지 |
| `test_sweep` | sweep 진행 규칙 — 방향, 목표 clamp, 0 rate 차단 |
| `test_step_pipeline` | 측정 한 스텝의 기록 — 값/nan/ERR 구분, 실패 판정 |
| `test_data_saver` | `.dat` 헤더·자동 번호·이어쓰기·실패 보고 |
| `test_instrument_parameter` | 계측기 응답 파싱 (모호하면 에러) |
| `test_visa_errors` | 통신 오류 vs 파싱 오류 판정 (자동 재개 경로를 가른다) |
| `test_derivative_channel` | 1~3차 미분, scipy 부재 시 fallback |
| `test_mfli_merge` | LabOne 병합 — 정렬, 주파수 매칭, NaN 채움 |
| `test_plot_panel` | 공용 플롯 패널 상태 저장/복원 |

---

## exe 빌드

```
pip install pyinstaller
pyinstaller build_exe.spec       # → dist/Pythonization/Pythonization.exe
```

`build/` 와 `dist/` 는 저장소에 넣지 않는다.

---

## 코드를 고칠 때

- **워커 스레드 → GUI 는 반드시 bound `@Slot`** 으로 연결한다. lambda 에 연결하면
  워커 스레드에서 직접 실행돼 QWidget 접근 시 네이티브 크래시가 난다.
- 그 밖의 스레드·VISA 안전 불변식은 [docs/STRUCTURE.md](docs/STRUCTURE.md) 의
  "안전 불변식" 절에 정리돼 있다. 위반하면 조용히 죽는 대신 크래시가 난다.
- 구조가 바뀌면 `docs/STRUCTURE.md`, 기능이 바뀌면 `docs/CHANGELOG.md` 를 함께 고친다.
- 드라이버를 추가하려면 `pythonization/instruments/drivers/` 에 `vendor_model.py` 를
  만들고 `BaseInstrument` 를 상속하면 된다. Instrument Settings 의 드라이버 목록은
  이 폴더를 훑어 자동으로 채워진다.
