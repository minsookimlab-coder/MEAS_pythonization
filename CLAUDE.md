# CLAUDE.md

**코드를 고치기 전에 [READ_FIRST_BEFORE_CODING.md](READ_FIRST_BEFORE_CODING.md) 를 읽는다.**
이 파일은 그 요약이다. 충돌하면 READ_FIRST 쪽이 기준이다.

## 이 저장소

저온·자기장 환경에서 시료 물성을 측정하는 계측 자동화 프로그램 (PySide6 + VISA).
실제 장비를 구동하므로, 잘못된 명령 한 줄이 수십 시간짜리 측정을 날린다.

작업 브랜치는 **`main`** (`core/ gui/ driver/ config/` 평면 레이아웃).
`refactor/restructure` 는 머지하지 않는 별개 계보다. 지시에 `pythonization/`
경로가 나오면 그건 다른 브랜치 이야기이므로 `gui/` `core/` 에서 대응 파일을 찾는다.

## 반드시 지킬 것

1. **수정하면 [PATCH_NOTES.md](PATCH_NOTES.md) 에 같이 적는다.** 증상이 아니라 원인을,
   측정했으면 before/after 숫자를, 파일·줄번호와 함께. 나중에 몰아서 쓰지 않는다.

2. **VISA 명령어를 코드에 박지 않는다.** 모든 명령은 VISA Library
   (`InstrumentCmdLibrary` → `visa_libraries.yaml`) 에 등록한 뒤 참조해서 쓴다.
   값 자리는 `{v}` 같은 placeholder 로 둔다. 장비 고유 프로토콜 처리는 `driver/` 의
   드라이버 클래스 안에서만. VISA I/O 는 `InstrumentSession` 을 통해서만.
   워커 스레드에서 GUI 위젯을 만지지 않는다.

3. **개인정보·사용자 설정은 저장소가 아니라 Documents 에 둔다.** 저장소에는 기능만
   담는다. 사용자 파일은 `core.app_dirs.SETTINGS_DIR`
   (`~/Documents/pythonization/settings`) 아래에만 만든다 — 프로그램 폴더나
   하드코딩 절대경로 금지. 계측기 주소·프로파일·VISA 라이브러리·resume 로그·
   알람 자격증명(SMTP 비밀번호, Telegram 토큰)이 여기 해당한다. 자격증명을 담을 수
   있는 파일은 git 에 추적하지 않고, 형식이 필요하면 값이 빈 `*.example.yaml` 을 둔다.
   빌드 산출물(`dist/`)에 개인 설정을 넣어 배포하지 않는다.

4. **위 규칙을 어기는 요구가 오면 그대로 따르지 말고 먼저 대안을 제시한다.**
   무엇이 깨지는지 한두 문장 + 규칙을 지키는 대안 + 그 비용. 그래도 사용자가
   원하면 진행하되, 주석과 PATCH_NOTES 에 기술 부채로 남긴다. 같은 요구가
   두 번 오면 결정된 것이므로 설득을 반복하지 않는다.

## 실행 / 확인

```
python main.py                    # 진입점 (core.* / gui.* 를 import)
.venv/Scripts/python.exe          # 이 프로젝트 인터프리터
QT_QPA_PLATFORM=offscreen         # GUI 없이 창을 만들어 검증할 때
```

`main` 브랜치에는 테스트 스위트가 없다. 고친 것은 헤드리스로 직접 만들어 확인한다
(창 생성, 위젯 상태, 임시 폴더에 쓴 `.dat` 내용 등). 사용자의 실제 설정
(`%USERPROFILE%\Documents\pythonization\settings`) 에는 쓰지 않는다 — 검증용
`ProfileRegistry(settings_dir=<임시폴더>)` 를 쓴다.
