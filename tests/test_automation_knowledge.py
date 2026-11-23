"""AI 자동 측정 — 실험 지식 저장소 (현상 → 물리적 의미 → 대응, 관찰 기록).

지키는 계약:
  - 저장소는 가이드다: 해석 규칙은 첫 메시지에 내용을 싣지 않고(편향 방지) 개수만 알린다
  - 관찰이 규칙을 뒷받침/반박한 수를 똑같이 센다 (자기 강화 방지)
  - AI 가 저장한 규칙은 승인·사용자 요청이 없으면 '가설'
  - 창을 열기만 해서는 사용자 폴더에 파일이 생기지 않는다
  - 데이터 파일 분석은 Main Folder 밖을 읽지 않는다
"""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pythonization.automation.executor import LabExecutor            # noqa: E402
from pythonization.automation.knowledge import (                      # noqa: E402
    REFERENCE_NOTE,
    KnowledgeStore,
)
from tests.automation_fakes import FakeLab, make_profile, make_settings  # noqa: E402
from tests.test_automation_executor import FakeBackend                 # noqa: E402


class StoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "knowledge"
        self.store = KnowledgeStore(self.root)

    def tearDown(self):
        self._tmp.cleanup()


class TestStore(StoreCase):
    def test_seed_examples_in_memory_until_first_change(self):
        titles = [e.title for e in self.store.entries()]
        self.assertTrue(any("Dirac" in t for t in titles))
        self.assertTrue(any("leak" in t for t in titles))
        self.assertFalse(self.root.exists(), "읽기만 했는데 파일이 생기면 안 된다")
        self.store.add_entry(title="x", condition="c", meaning="m")
        self.assertTrue((self.root / "knowledge.json").exists())
        self.assertEqual(3, len(KnowledgeStore(self.root).entries()), "예시 2 + 추가 1")

    def test_update_and_delete(self):
        e = self.store.add_entry(title="hall", condition="Rxy 선형", meaning="캐리어 밀도")
        self.store.update_entry(e.id, meaning="캐리어 밀도 n = B/(e Rxy)", confirmed=False)
        back = KnowledgeStore(self.root).get(e.id)
        self.assertEqual("캐리어 밀도 n = B/(e Rxy)", back.meaning)
        self.assertFalse(back.confirmed)
        self.assertTrue(self.store.delete_entry(e.id))
        self.assertIsNone(KnowledgeStore(self.root).get(e.id))

    def test_evidence_counts_support_and_contradiction(self):
        rule = self.store.add_entry(title="dirac", condition="peak", meaning="dirac")
        self.store.add_observation(summary="peak at 0.8 V", supports=[rule.id])
        self.store.add_observation(summary="no peak", contradicts=[rule.id])
        self.store.add_observation(summary="peak at 0.7 V", supports=[rule.id, "nope"])
        back = KnowledgeStore(self.root).get(rule.id)
        self.assertEqual((2, 1), (back.supports, back.contradicts))
        obs = self.store.observations()
        self.assertEqual([rule.id], obs[-1].supports, "없는 규칙 id 는 버린다")
        self.store.delete_observation(obs[1].id)
        self.assertEqual(0, self.store.get(rule.id).contradicts, "관찰을 지우면 근거도 줄어든다")

    def test_search_matches_korean_partial_words(self):
        res = self.store.search("게이트에서 누설전류가 커졌다")
        self.assertIn("leak", res["rules"][0]["title"])
        self.assertEqual(REFERENCE_NOTE, res["note"])
        self.assertIn("evidence", res["rules"][0])

    def test_search_filters_and_finds_observations(self):
        self.store.add_observation(summary="Dirac point 0.8 V at 2 K",
                                   quantities={"Vd (V)": 0.8}, tags=["dirac_point"])
        res = self.store.search("dirac point", kinds=["safety"])
        self.assertTrue(all(r["kind"] == "safety" for r in res["rules"]))
        self.assertEqual(0.8, res["observations"][0]["quantities"]["Vd (V)"])

    def test_digest_hides_interpretation_rules(self):
        d = self.store.digest()
        titles = [r["title"] for r in d["operational_rules"]]
        self.assertTrue(any("leak" in t for t in titles), "안전 규칙은 처음부터 준다")
        self.assertFalse(any("Dirac" in t for t in titles), "해석 규칙 내용은 싣지 않는다")
        self.assertEqual(1, d["interpretation_rules_available"])
        self.assertIn("먼저", d["how_to_use"])

    def test_broken_file_kept_aside(self):
        self.root.mkdir(parents=True)
        (self.root / "knowledge.json").write_text("{broken", encoding="utf-8")
        store = KnowledgeStore(self.root)
        self.assertEqual([], store.entries())
        self.assertTrue(list(self.root.glob("knowledge.broken-*.json")))

    def test_thread_safe_appends(self):
        def add(i):
            self.store.add_observation(summary=f"obs {i}")
        threads = [threading.Thread(target=add, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(20, len(self.store.observations()))


class TestKnowledgeTools(StoreCase):
    def setUp(self):
        super().setUp()
        self.data = Path(self._tmp.name) / "data"
        (self.data / "sampleA").mkdir(parents=True)
        self.dat = self.data / "sampleA" / "Vg_X001.dat"
        self.dat.write_text("Vg\tR\nV\tOhm\n-1\t100\n0\t900\n1\t120\n", encoding="utf-8")
        self.ex = LabExecutor(
            session=FakeLab(), profile=make_profile(), settings=make_settings(),
            backend=FakeBackend(self.data), stop_event=threading.Event(),
            emit=lambda ev: None, save_root=str(self.data), knowledge=self.store,
            profile_name="p1", run_dir="run1")
        self.ex.deadline = time.monotonic() + 600

    def test_save_knowledge_is_hypothesis_unless_confirmed(self):
        out = self.ex.execute("save_knowledge", {
            "kind": "interpretation", "title": "Rxx peak → Dirac point",
            "condition": "Vg≈0.8 V 에서 Rxx 최대", "meaning": "전하 중성점", "action": "기록"})
        self.assertFalse(out.is_error, out.content)
        self.assertEqual("가설(미확인, AI 제안)", out.content["saved"]["status"])
        out = self.ex.execute("save_knowledge", {
            "kind": "safety", "title": "x", "condition": "c", "meaning": "m", "action": "a",
            "user_requested": True})
        self.assertEqual("확인됨", out.content["saved"]["status"])
        self.ex.confirm_knowledge_on_save = True      # 승인 절차를 거친 경우
        out = self.ex.execute("save_knowledge", {
            "kind": "note", "title": "y", "condition": "c", "meaning": "m", "action": ""})
        self.assertEqual("확인됨", out.content["saved"]["status"])

    def test_save_knowledge_validates(self):
        self.assertTrue(self.ex.execute("save_knowledge", {
            "kind": "magic", "title": "x", "condition": "c", "meaning": "m"}).is_error)
        self.assertTrue(self.ex.execute("save_knowledge", {
            "kind": "note", "title": "", "condition": "c", "meaning": "m"}).is_error)
        self.assertTrue(self.ex.execute("save_knowledge", {
            "kind": "note", "title": "x", "condition": "c", "meaning": "m",
            "replaces_id": "k-nothere"}).is_error)

    def test_record_observation_links_rules(self):
        rule = self.store.entries()[0]
        out = self.ex.execute("record_observation", {
            "summary": "Vg=0.8 V 에서 R peak 900 Ω", "quantities": {"Vd (V)": 0.8},
            "interpretation": "Dirac point (확신 중간)", "files": [str(self.dat)],
            "supports": [rule.id], "tags": ["dirac_point"]})
        self.assertFalse(out.is_error, out.content)
        obs = self.store.observations()[-1]
        self.assertEqual(("p1", "run1"), (obs.profile, obs.run))
        self.assertEqual(1, self.store.get(rule.id).supports)

    def test_recall(self):
        out = self.ex.execute("recall_knowledge", {"query": "게이트 누설", "kinds": ["safety"]})
        self.assertIn("leak", out.content["rules"][0]["title"])
        self.assertIn("note", out.content)

    def test_analyze_data_file_inside_main_folder_only(self):
        out = self.ex.execute("analyze_data_file", {"file": "sampleA/Vg_X001.dat"})
        self.assertFalse(out.is_error, out.content)
        self.assertEqual(0.0, out.content["data"]["columns"]["R (Ohm)"]["x_at_max"])
        outside = Path(self._tmp.name) / "secret.dat"
        outside.write_text("a\nb\n1\n", encoding="utf-8")
        for bad in (str(outside), "../secret.dat", "sampleA/../../secret.dat"):
            with self.subTest(bad=bad):
                self.assertTrue(self.ex.execute("analyze_data_file", {"file": bad}).is_error)

    def test_lab_state_includes_digest(self):
        out = self.ex.execute("get_lab_state", {})
        self.assertIn("operational_rules", out.content["knowledge"])


if __name__ == "__main__":
    unittest.main()
