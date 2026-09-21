# CLAUDE.md

**코드를 고치기 전에 [READ_FIRST_BEFORE_CODING.md](READ_FIRST_BEFORE_CODING.md) 를 읽는다.**
이 파일은 그 요약이다. 충돌하면 READ_FIRST 쪽이 기준이다.

## 이 저장소

저온·자기장 환경에서 시료 물성을 측정하는 계측 자동화 프로그램 (PySide6 + VISA).
실제 장비를 구동하므로, 잘못된 명령 한 줄이 수십 시간짜리 측정을 날린다.

코드는 저장소 루트의 단일 패키지 **`pythonization/`** 안에 있다.

```
main.py                 얇은 셸 (기동 순서는 pythonization/app/bootstrap.py)
pythonization/
  app/ config/ instruments/(+drivers/) measurement/
  profiles/ analysis/ notify/ util/
  ui/ (main_window + widgets/ dialogs/ panels/ modules/ assets/)
tests/                  표준 라이브러리 unittest (334개)
docs/                   STRUCTURE / USER_MANUAL / CHANGELOG
```

옛 대화나 문서에 `gui/vna_window.py` 같은 평면 경로가 나오면 `v1.11.0` 이전 이야기다.
`pythonization/ui/modules/vna/window.py` 처럼 대응하는 파일을 찾는다.

## 반드시 지킬 것

1. **수정하면 [docs/CHANGELOG.md](docs/CHANGELOG.md) 에 같이 적는다.** 증상이 아니라
   원인을, 측정했으면 before/after 숫자를, 파일·줄번호와 함께. 나중에 몰아서 쓰지 않는다.

2. **VISA 명령어를 코드에 박지 않는다.** 모든 명령은 VISA Library
   (`InstrumentCmdLibrary` → `visa_libraries.yaml`) 에 등록한 뒤 참조해서 쓴다.
   값 자리는 `{v}` 같은 placeholder 로 둔다. 장비 고유 프로토콜 처리는
   `pythonization/instruments/drivers/` 의 드라이버 클래스 안에서만.
   VISA I/O 는 `InstrumentSession` 을 통해서만. 워커 스레드에서 GUI 위젯을 만지지 않는다.

3. **개인정보·사용자 설정은 저장소가 아니라 Documents 에 둔다.** 저장소에는 기능만
   담는다. 사용자 파일은 `pythonization.app.paths.SETTINGS_DIR`
   (`~/Documents/pythonization/settings`) 아래에만 만든다 — 프로그램 폴더나
   하드코딩 절대경로 금지. 계측기 주소·프로파일·VISA 라이브러리·resume 로그·
   알람 자격증명(SMTP 비밀번호, Telegram 토큰)이 여기 해당한다. 자격증명을 담을 수
   있는 파일은 git 에 추적하지 않고, 형식이 필요하면 값이 빈 `*.example.yaml` 을 둔다.
   빌드 산출물(`dist/`)에 개인 설정을 넣어 배포하지 않는다.

4. **위 규칙을 어기는 요구가 오면 그대로 따르지 말고 먼저 대안을 제시한다.**
   무엇이 깨지는지 한두 문장 + 규칙을 지키는 대안 + 그 비용. 그래도 사용자가
   원하면 진행하되, 주석과 CHANGELOG 에 기술 부채로 남긴다. 같은 요구가
   두 번 오면 결정된 것이므로 설득을 반복하지 않는다.

5. **요청한 수정이 모두 끝나고 검증되면 커밋하고 푸시한다** (`git push origin main`).
   별도 지시를 기다리지 않는다. 커밋은 의미 단위로 나누고 CHANGELOG 갱신을 포함한다.
   검증에 실패했거나 작업이 안 끝났으면 커밋하지 말고 상태를 보고한다.

6. **브랜치를 만들지 않는다.** 브랜치는 `main` 하나다. 큰 변경도 `main` 에서 작은
   커밋으로 나눠 진행하고, 되돌려야 하면 `git revert` 를 쓴다. 갈라진 계보를 7주
   방치했다가 동작이 달라진 사고가 있었다 (`v1.11.0` 에서 통합하고 브랜치는 지웠다).
   사용자가 명시적으로 만들라고 할 때만 만든다.

## 실행 / 확인

```
python main.py                                   # 진입점 (python -m pythonization 도 동일)
.venv/Scripts/python.exe                         # 이 프로젝트 인터프리터
python -m unittest discover -s tests -t .        # 테스트 334개 (추가 설치 불필요)
QT_QPA_PLATFORM=offscreen                        # GUI 없이 창을 만들어 검증할 때
```

테스트가 닿지 않는 것(측정 흐름, 창 동작)은 헤드리스로 직접 만들어 확인한다
(창 생성, 위젯 상태, 임시 폴더에 쓴 `.dat` 내용 등). 사용자의 실제 설정
(`%USERPROFILE%\Documents\pythonization\settings`) 에는 쓰지 않는다 — 검증용
`ProfileRegistry(settings_dir=<임시폴더>)` 를 쓴다.
