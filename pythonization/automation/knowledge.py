"""실험 지식 저장소 — 측정된 물리량과 물리적 의미를 이어 두는 로컬 기억.

두 가지를 저장한다:

  규칙(KnowledgeEntry)   "어떤 현상이 보이면(condition) → 무슨 뜻이고(meaning) → 어떻게
                          대응한다(action)". 예:
                            gate 0 V 근처 lock-in peak → 그래핀 Dirac point → 그 근처를 촘촘히
                            gate 전극 전류 수 µA → 게이트 누설(leak) → 측정 중단 + 알람
  관찰(Observation)      실제 측정에서 본 것 — 수치, 데이터 파일, 해석, 그리고 어떤 규칙을
                          뒷받침했는지(supports) 또는 반박했는지(contradicts).

편향 방지 (참고용이지 정답이 아니다):
  - 해석 규칙은 처음부터 LLM 에게 주지 않는다. LLM 이 데이터 특징을 먼저 스스로 기술한
    뒤 recall 로 꺼내 비교한다. 첫 메시지에는 안전·절차 규칙만 넣는다 (행동 기준이라서).
  - 규칙마다 뒷받침/반박 관찰 수를 센다. 반박이 쌓인 규칙은 그 숫자가 그대로 보인다.
  - AI 가 만든 규칙은 사용자가 확인하기 전까지 '가설(미확인)'로 표시된다.
  - 조회 결과에는 '참고 자료 — 현재 데이터가 근거' 라는 주의문을 붙인다.

저장 위치: SETTINGS_DIR/knowledge/ (저장소 밖 — 랩·시료 정보, 규칙 3).
  knowledge.json      규칙 목록 (사람이 직접 열어 고칠 수 있는 JSON)
  observations.jsonl  관찰 기록 (덧붙이기만)

Qt 를 쓰지 않는다. 에이전트 스레드와 GUI 스레드가 함께 쓰므로 내부 락으로 보호하고,
규칙 파일은 임시 파일에 쓴 뒤 교체해 중간에 끊겨도 깨지지 않게 한다.
"""
import json
import os
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Literal, Optional, Union

from pydantic import BaseModel, Field

from pythonization.app.paths import SETTINGS_DIR

KIND_LABELS = {
    "interpretation": "해석",     # 현상 → 물리적 의미
    "safety": "안전",             # 현상 → 즉시 대응 (중단·알람)
    "procedure": "절차",          # 측정 관례 (속도, 순서, 되돌리기 …)
    "note": "메모",
}
Kind = Literal["interpretation", "safety", "procedure", "note"]

REFERENCE_NOTE = ("가이드일 뿐이다. 판단은 네가 현재 측정 데이터로 직접 한다. 데이터가 이 "
                  "규칙과 맞지 않거나 다른 설명이 가능하면 그렇게 적고, record_observation 에 "
                  "contradicts 로 남겨라. supports/contradicts 수는 지금까지의 근거다.")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class KnowledgeEntry(BaseModel):
    id: str = Field(default_factory=lambda: _new_id("k"))
    kind: Kind = "interpretation"
    title: str
    condition: str = ""          # 관찰되는 현상 (어떤 채널에서 어떤 값·모양)
    meaning: str = ""            # 물리적 의미
    action: str = ""             # 대응
    tags: List[str] = Field(default_factory=list)
    sample: str = ""             # 특정 시료에만 해당하면 그 이름
    source: Literal["user", "ai"] = "user"
    confirmed: bool = True       # AI 가 만들고 사용자가 확인 안 했으면 False (= 가설)
    supports: int = 0            # 이 규칙을 뒷받침한 관찰 수
    contradicts: int = 0         # 이 규칙과 어긋난 관찰 수
    created: str = Field(default_factory=_now)
    updated: str = Field(default_factory=_now)

    def brief(self) -> dict:
        """LLM 에게 보여 줄 형태."""
        out = {
            "id": self.id, "kind": self.kind, "title": self.title,
            "condition": self.condition, "meaning": self.meaning, "action": self.action,
            "tags": self.tags,
            "status": "확인됨" if self.confirmed else "가설(미확인, AI 제안)",
            "evidence": {"supports": self.supports, "contradicts": self.contradicts},
        }
        if self.sample:
            out["sample"] = self.sample
        return out


class Observation(BaseModel):
    id: str = Field(default_factory=lambda: _new_id("o"))
    time: str = Field(default_factory=_now)
    profile: str = ""
    sample: str = ""
    summary: str                                   # 무엇을 봤는지 (수치 포함)
    quantities: Dict[str, Union[float, str]] = Field(default_factory=dict)
    interpretation: str = ""                       # 해석 (확신 정도 포함)
    files: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    supports: List[str] = Field(default_factory=list)      # 뒷받침한 규칙 id
    contradicts: List[str] = Field(default_factory=list)   # 어긋난 규칙 id
    run: str = ""                                  # AI 실행 기록 폴더

    def brief(self) -> dict:
        out = self.model_dump(exclude_defaults=True)
        out["id"], out["time"], out["summary"] = self.id, self.time, self.summary
        return out


#: 저장소를 처음 만들 때 넣는 예시 — 사용자가 든 예. 지우거나 고쳐 쓰면 된다.
SEED_ENTRIES = [
    KnowledgeEntry(
        kind="interpretation", title="gate 0 V 근처 lock-in peak → Dirac point (그래핀)",
        condition="게이트 전압 sweep 에서 lock-in 신호(저항)가 0 V 근처에서 peak 를 보인다",
        meaning="그래핀의 Dirac point (전하 중성점). peak 위치는 도핑에 따라 0 V 에서 벗어날 "
                "수 있다",
        action="peak 위치·높이·폭을 기록하고, 필요하면 그 근처를 더 촘촘히 측정",
        tags=["graphene", "gate", "lock-in", "dirac_point"]),
    KnowledgeEntry(
        kind="safety", title="gate 전극 전류 수 µA → 게이트 누설(leak)",
        condition="게이트에 연결된 전극의 전류(누설전류)가 수 µA 수준으로 커진다",
        meaning="게이트 절연막을 통한 누설 — 시료·절연막 손상 위험",
        action="즉시 측정 중단, urgent 메신저 알람. 게이트를 더 올리지 않는다. "
               "누설전류를 기록하는 sweep 에는 abort_if_abs_above 를 건다",
        tags=["gate", "leak", "current", "safety"]),
]


_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _bigrams(text: str) -> set:
    """한국어 조사가 붙어도 걸리도록 글자 2-gram 으로 비교한다."""
    grams = set()
    for word in _WORD_RE.findall(text.lower()):
        if len(word) == 1:
            grams.add(word)
        grams.update(word[i:i + 2] for i in range(len(word) - 1))
    return grams


def _score(query_grams: set, text: str) -> float:
    if not query_grams:
        return 0.0
    return len(query_grams & _bigrams(text)) / len(query_grams)


class KnowledgeStore:
    """규칙 + 관찰 저장소. 여러 스레드에서 써도 된다."""

    def __init__(self, root: Optional[Path] = None, seed: bool = True):
        self.root = Path(root) if root is not None else SETTINGS_DIR / "knowledge"
        self._rules_path = self.root / "knowledge.json"
        self._obs_path = self.root / "observations.jsonl"
        self._lock = threading.RLock()
        self._entries: List[KnowledgeEntry] = []
        self._load(seed)

    # ── 파일 ───────────────────────────────────────────────────────────
    def _load(self, seed: bool) -> None:
        with self._lock:
            if not self._rules_path.exists():
                # 예시 규칙은 메모리에만 두고, 처음 무언가를 바꿀 때 함께 저장한다
                # (창을 열기만 해서 사용자 폴더에 파일이 생기지 않게).
                self._entries = [e.model_copy(deep=True) for e in SEED_ENTRIES] if seed else []
                for e in self._entries:          # 저장소마다 고유 id
                    e.id = _new_id("k")
                return
            try:
                data = json.loads(self._rules_path.read_text(encoding="utf-8"))
                self._entries = [KnowledgeEntry(**d) for d in data.get("entries", [])]
            except Exception:
                # 깨진 파일을 덮어쓰지 않도록 옆에 보관하고 빈 상태로 시작한다
                backup = self._rules_path.with_suffix(
                    f".broken-{datetime.now():%Y%m%d%H%M%S}.json")
                try:
                    self._rules_path.replace(backup)
                except Exception:
                    pass
                self._entries = []

    def _save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self._rules_path.with_suffix(".tmp")
        payload = {"entries": [e.model_dump(mode="json") for e in self._entries]}
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self._rules_path)

    # ── 규칙 ───────────────────────────────────────────────────────────
    def entries(self) -> List[KnowledgeEntry]:
        with self._lock:
            return [e.model_copy() for e in self._entries]

    def get(self, entry_id: str) -> Optional[KnowledgeEntry]:
        with self._lock:
            for e in self._entries:
                if e.id == entry_id:
                    return e.model_copy()
        return None

    def add_entry(self, **fields) -> KnowledgeEntry:
        with self._lock:
            entry = KnowledgeEntry(**fields)
            self._entries.append(entry)
            self._save()
            return entry.model_copy()

    def update_entry(self, entry_id: str, **fields) -> Optional[KnowledgeEntry]:
        with self._lock:
            for i, e in enumerate(self._entries):
                if e.id == entry_id:
                    data = e.model_dump()
                    data.update({k: v for k, v in fields.items() if k in data})
                    data["updated"] = _now()
                    self._entries[i] = KnowledgeEntry(**data)
                    self._save()
                    return self._entries[i].model_copy()
        return None

    def delete_entry(self, entry_id: str) -> bool:
        with self._lock:
            before = len(self._entries)
            self._entries = [e for e in self._entries if e.id != entry_id]
            if len(self._entries) != before:
                self._save()
                return True
        return False

    # ── 관찰 ───────────────────────────────────────────────────────────
    def add_observation(self, **fields) -> Observation:
        with self._lock:
            obs = Observation(**fields)
            known = {e.id for e in self._entries}
            obs.supports = [i for i in obs.supports if i in known]
            obs.contradicts = [i for i in obs.contradicts if i in known and i not in obs.supports]
            self.root.mkdir(parents=True, exist_ok=True)
            with open(self._obs_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(obs.model_dump(mode="json"), ensure_ascii=False) + "\n")
            # 규칙의 근거 수 갱신 — 반박도 똑같이 센다 (자기 강화 편향 방지)
            changed = False
            for e in self._entries:
                if e.id in obs.supports:
                    e.supports += 1
                    changed = True
                if e.id in obs.contradicts:
                    e.contradicts += 1
                    changed = True
            if changed:
                self._save()
            return obs

    def observations(self, limit: Optional[int] = None) -> List[Observation]:
        """최근 것이 뒤에 오는 순서."""
        with self._lock:
            if not self._obs_path.exists():
                return []
            out = []
            for line in self._obs_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    out.append(Observation(**json.loads(line)))
                except Exception:
                    continue    # 한 줄이 깨져도 나머지는 읽는다
        return out[-limit:] if limit else out

    def delete_observation(self, obs_id: str) -> bool:
        with self._lock:
            all_obs = self.observations()
            keep = [o for o in all_obs if o.id != obs_id]
            if len(keep) == len(all_obs):
                return False
            removed = next(o for o in all_obs if o.id == obs_id)
            tmp = self._obs_path.with_suffix(".tmp")
            tmp.write_text("".join(json.dumps(o.model_dump(mode="json"), ensure_ascii=False)
                                   + "\n" for o in keep), encoding="utf-8")
            os.replace(tmp, self._obs_path)
            for e in self._entries:
                if e.id in removed.supports:
                    e.supports = max(0, e.supports - 1)
                if e.id in removed.contradicts:
                    e.contradicts = max(0, e.contradicts - 1)
            self._save()
            return True

    # ── 조회 ───────────────────────────────────────────────────────────
    def search(self, query: str, kinds: Optional[Iterable[str]] = None,
               tags: Optional[Iterable[str]] = None, limit: int = 8,
               include_observations: bool = True) -> dict:
        """키워드로 규칙·관찰을 찾는다. 결과에는 항상 '참고 자료' 주의문이 붙는다."""
        q = _bigrams(query or "")
        kinds = set(kinds or [])
        tags = {t.lower() for t in (tags or [])}
        scored = []
        for e in self.entries():
            if kinds and e.kind not in kinds:
                continue
            text = " ".join([e.title, e.condition, e.meaning, e.action, e.sample, *e.tags])
            s = _score(q, text) + (0.5 if tags & {t.lower() for t in e.tags} else 0.0)
            if s > 0.15 or (not q and not tags):
                scored.append((s, e))
        scored.sort(key=lambda p: -p[0])
        result = {"note": REFERENCE_NOTE,
                  "rules": [e.brief() for _, e in scored[:limit]]}
        if include_observations:
            obs_scored = []
            for o in self.observations():
                text = " ".join([o.summary, o.interpretation, o.sample, *o.tags,
                                 *map(str, o.quantities)])
                s = _score(q, text) + (0.5 if tags & {t.lower() for t in o.tags} else 0.0)
                if s > 0.15 or (not q and not tags):
                    obs_scored.append((s, o))
            # 점수가 같으면 최근 것 우선
            obs_scored.sort(key=lambda p: (p[0], p[1].time), reverse=True)
            result["observations"] = [o.brief() for _, o in obs_scored[:limit]]
        return result

    def digest(self) -> dict:
        """첫 메시지용 — 안전·절차 규칙 전문 + 해석 규칙은 개수만 (편향 방지)."""
        entries = self.entries()
        operational = [e.brief() for e in entries if e.kind in ("safety", "procedure")]
        n_interp = sum(1 for e in entries if e.kind in ("interpretation", "note"))
        return {
            "operational_rules": operational,
            "interpretation_rules_available": n_interp,
            "observations_recorded": len(self.observations()),
            "how_to_use": ("해석 규칙은 일부러 미리 보여 주지 않는다. 데이터를 받으면 먼저 "
                           "스스로 특징을 기술한 뒤 recall_knowledge 로 비교하라. "
                           + REFERENCE_NOTE),
        }
