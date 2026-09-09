# READ FIRST BEFORE CODING

이 저장소에서 **AI 코딩 도구(Claude Code 등)로 작업하기 전에 반드시 읽는다.**
로컬 기록·대화가 사라져도 이 파일만 읽으면 규칙이 복원되도록 자족적으로 적는다.

작업 브랜치는 **`main`** 이다. `refactor/restructure` 는 별개 계보이며 머지하지 않는다
(자세한 내용은 맨 아래 참고).

---

## 규칙 1 — 수정사항은 반드시 PATCH_NOTES.md 에 남긴다

코드를 고쳤으면 **같은 작업 안에서** [PATCH_NOTES.md](PATCH_NOTES.md) 를 갱신한다.
"나중에 몰아서" 는 금지다. 이 파일이 이 프로젝트의 유일한 변경 이력이며,
git log 보다 먼저 읽히는 문서다.

**형식** — 최신 항목이 맨 위. 기존 문서의 스타일을 그대로 따른다.

```markdown
## v1.07.2 — 2026-09-09 (버그픽스)

- **[치명] 한 줄 요약** — 무엇이 왜 잘못됐는지, 어떤 조건에서 드러나는지,
  어떻게 고쳤는지. 파일·함수·줄번호를 적는다 (`gui/vna_window.py:1279`).
  재현 조건과 실측값이 있으면 함께 적는다.
```

지켜야 할 것:

- **증상이 아니라 원인을 적는다.** "느려서 고침"이 아니라 "centralWidget 에
  setStyleSheet 을 30 ms 마다 호출해 자식 369개의 스타일을 재계산하고 있었다".
- **측정했으면 숫자를 적는다.** before/after 수치는 다음 사람이 회귀를 알아채는 근거다.
- **[치명]** 은 측정 데이터 유실·장비 오동작·장시간 측정 중단에만 붙인다.
- 버전을 올렸으면 `version_info.txt` 도 같이 맞춘다.
- 사용자에게 보이는 동작이 바뀌었으면 [USER_MANUAL.md](USER_MANUAL.md),
  구조가 바뀌었으면 [STRUCTURE.md](STRUCTURE.md) 도 함께 고친다.

---

## 규칙 2 — VISA 명령어는 라이브러리에 등록한 뒤에만 쓴다

**소스 코드에 VISA 명령 문자열을 직접 박지 않는다.** 계측기에 나가는 모든 명령은
VISA Library 에 등록되어, 사용자가 GUI 에서 확인·수정할 수 있어야 한다.

### 지켜야 할 계층

```
VISA Library  (visa_libraries.yaml)          alias 별 명령어 카탈로그
  └ InstrumentCmdLibrary
      ├ measurements : MeasurementParamDef   cmd_query   읽기
      ├ sweep_values : SweepValueDef         cmd_set + paired_read_cmd  쓰기+되읽기
      └ write_cmds   : WriteCmdDef           cmd_set     단방향 쓰기
        ↓  Parameter Manager 에서 {placeholder} 를 채워 인스턴스화
Profile       (profiles/{name}.yaml)         Instantiated* 항목
        ↓
측정 엔진      sweep_worker / sweep_channel / instrument_parameter
        ↓
InstrumentSession                            유일한 VISA I/O 창구
```

### 하면 안 되는 것

```python
# 금지 — 명령어를 코드에 박는다
session.write("2636A", "smua.source.levelv = 1.5")
session.query("ITC", "READ:DEV:MB1.T1:TEMP:SIG:TEMP")

# 금지 — 드라이버가 아닌 곳에서 계측기 문자열을 조립한다
cmd = f"SENS:FREQ {freq};:READ?"
```

### 해야 하는 것

```python
# 라이브러리에 등록된 항목을 가져다 쓴다
lib      = lib_reg.get_library(entry.alias)     # VisaLibraryRegistry
template = get_template(lib, entry)
cmd      = build_cmd(template, entry.params, value)
session.write(entry.alias, cmd)                 # I/O 는 항상 InstrumentSession
```

- 새 명령이 필요하면 **먼저 VISA Library 에 항목을 추가**하고, 그 항목을 참조한다.
- 값이 들어갈 자리는 `{v}`, `{m}` 같은 **placeholder** 로 둔다.
  sweep 축은 `{v}` 로 정규화한다.
- 장비 고유의 프로토콜 처리(TSP 의 `print()` 감싸기, ISOBUS 접두어, raw socket
  종단 문자 등)는 **`driver/` 의 드라이버 클래스 안에서만** 한다.
  드라이버는 `core/instrument_base.py` 외에는 아무것도 모른다.
- VISA I/O 는 **`InstrumentSession` 을 통해서만** 한다. `pyvisa` 를 직접 열지 않는다.
- 워커 스레드에서 GUI 위젯을 만지지 않는다. 값은 Signal 로 넘기고
  bound `@Slot` 에서 받는다.

### 왜 이렇게 하나

- 랩 PC 마다 장비 구성과 명령이 다르다. 코드에 박으면 **사용자가 고칠 수 없다.**
- 같은 명령이 여러 창에 흩어져 복붙되면, 장비를 바꿀 때 어디를 고쳐야 하는지
  아무도 모른다.
- 이 프로젝트는 실제 저온·자기장 장비를 구동한다. 잘못된 명령 한 줄이
  **수십 시간짜리 측정을 날리거나 장비를 손상시킨다.**

---

## 규칙 3 — 개인정보·사용자 설정은 저장소가 아니라 Documents 에 둔다

**저장소에는 기능만 담는다.** 랩·사람마다 달라지는 값은 코드와 같은 폴더에 두지 않는다.

### 어디에 무엇을 두나

| | 위치 | 담는 것 |
|---|---|---|
| 프로그램 | 저장소 (`APP_DIR`) | 코드, 리소스, 기본값. **git 에 올라간다** |
| 사용자 데이터 | `SETTINGS_DIR` = `~/Documents/pythonization/settings` | 개인정보·랩 고유 설정. **git 에 올리지 않는다** |

`SETTINGS_DIR` 는 `core/app_dirs.py` 가 확정한다. 경로는 Settings → Config 에서 바꿀 수
있고(`set_data_dir()`, 재시작 후 적용), 새 코드는 이 값을 통해서만 사용자 파일에 접근한다.

```python
from core.app_dirs import SETTINGS_DIR
path = SETTINGS_DIR / "profiles" / f"{name}.yaml"     # OK

path = Path(__file__).parent / "my_settings.yaml"     # 금지 — 프로그램 폴더
path = Path("C:/Users/Main/Desktop/...")              # 금지 — 하드코딩된 절대경로
```

### Documents 로 가야 하는 것

계측기 주소·MAC, VISA 라이브러리, 프로파일, VNA/MFLI 설정, resume 로그,
알람 자격증명(SMTP 비밀번호·Telegram 봇 토큰·수신자), 마지막 저장 폴더,
사람 이름이 들어간 무엇이든, 측정 데이터(`.dat`).

### 저장소에 남아도 되는 것

코드, 아이콘 등 리소스, **값이 빈 예시 설정**(`*.example.yaml`), 문서.

### 지켜야 할 것

- **자격증명을 담을 수 있는 파일은 git 에 추적하지 않는다.** 형식을 알려야 하면
  값이 빈 `*.example.yaml` 을 대신 추적한다.
- 사용자 파일이 없을 때 프로그램이 죽지 않아야 한다. 없으면 기본값으로 시작하고
  필요할 때 만든다 (`mkdir(parents=True, exist_ok=True)`).
- 빌드 산출물(`dist/`) 안에 **개인 설정을 함께 넣어 배포하지 않는다.** 배포본은
  빈 상태로 나가고, 사용자가 자기 Documents 에서 구성한다.
- 검증 코드가 사용자의 실제 설정을 건드리지 않게 한다 —
  `ProfileRegistry(settings_dir=<임시폴더>)` 를 쓴다.

### 왜

이 저장소는 공개돼 있다. 한 번 커밋된 비밀번호·토큰은 `git rm` 해도 이력에 남는다.
그리고 랩 PC 마다 장비 구성이 다르므로, 설정이 코드에 섞이면 다른 PC 에서 프로그램이
그냥 돌지 않는다.

---

## 규칙 4 — 규칙 2·3 을 어기는 요구가 오면, 그대로 하지 말고 더 나은 설계를 제안한다

"급하니까 그냥 코드에 박아줘", "라이브러리 거치지 말고 바로 쏴줘" 같은 요구가 오면
**말없이 따르지 않는다.** 다음 순서로 대응한다.

1. **무엇이 깨지는지 한두 문장으로 말한다.**
   예: "그렇게 하면 이 명령은 VISA Library 에 안 남아서, 랩 PC 에서 장비가 바뀌면
   사용자가 GUI 로 고칠 수 없고 코드를 다시 배포해야 합니다."
2. **규칙을 지키면서 같은 목적을 이루는 대안을 제시한다.**
   보통은 "라이브러리에 항목 하나 추가 + Parameter Manager 에서 선택" 이면 끝난다.
   비용이 실제로 얼마나 드는지(파일 몇 개, 몇 줄) 같이 말한다.
3. **그래도 사용자가 그 방식을 원하면 그때는 요구대로 한다.** 대신
   - 왜 예외인지 코드에 주석으로 남기고,
   - PATCH_NOTES 에 **기술 부채로 명시**하고,
   - 나중에 정규 경로로 옮기는 방법을 한 줄 적어 둔다.

거부가 목적이 아니다. **결정을 사용자가 알고 내리게 하는 것**이 목적이다.
한 번 말했는데 사용자가 다시 같은 요구를 하면, 그건 결정이 내려진 것이다.
설득을 반복하지 말고 진행한다.

---

## 참고 — 브랜치

- **`main`** : 현재 사용하는 브랜치. `core/ gui/ driver/ config/` 평면 레이아웃.
- **`refactor/restructure`** : 2026-08-03 에 시작한 단일 `pythonization/` 패키지
  재구성 계보. **머지하지 않기로 했다.** GitHub(`origin`)에 보존돼 있으므로
  로컬이 날아가도 `git fetch origin refactor/restructure` 로 되찾을 수 있다.
  거기에만 있는 것: Cycle Sweep / Cycle Double Sweep 모듈, 테스트 100여 개, `docs/`.
  필요하면 파일 단위로 가져와 `main` 레이아웃에 맞게 옮긴다.

`main` 에서 작업할 때 `pythonization/` 경로를 언급하는 지시를 받으면,
그건 다른 브랜치 이야기다. `gui/` `core/` 쪽에서 대응하는 파일을 찾아 고친다.
