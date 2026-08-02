"""DataSaver — .dat 파일 생성/헤더/append/자동 번호.

측정 데이터 유실은 이 프로젝트에서 가장 비싼 실패(수십 시간짜리 측정)라
경로 규칙과 실패 보고 계약을 고정한다.
"""
import tempfile
import unittest
from datetime import date
from pathlib import Path

from pythonization.measurement.data_saver import DataSaver


def _saver(main_folder, **kw):
    s = DataSaver()
    s.set_main_folder(str(main_folder))
    s.set_custom_word(kw.get("custom_word", "test"))
    s.set_custom_folder(kw.get("custom_folder", ""))
    s.set_include_date(kw.get("include_date", False))
    s.set_enabled(kw.get("enabled", True))
    s.set_columns(kw.get("columns", [("B", "T"), ("R", "ohm")]))
    return s


class TestDataSaverStartSession(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_disabled_is_a_silent_noop(self):
        s = _saver(self.tmp, enabled=False)
        self.assertIsNone(s.start_session())
        self.assertIsNone(s.start_error(), "의도적 비활성은 오류가 아니다")

    def test_missing_main_folder_reports_error(self):
        s = _saver("", enabled=True)
        self.assertIsNone(s.start_session())
        self.assertIsNotNone(s.start_error(), "호출자가 측정을 중단할 수 있어야 한다")

    def test_missing_columns_reports_error(self):
        s = _saver(self.tmp, columns=[])
        self.assertIsNone(s.start_session())
        self.assertIsNotNone(s.start_error())

    def test_file_conflict_on_parent_path_reports_error(self):
        # main_folder 자리에 '파일'이 있으면 폴더를 만들 수 없다
        clash = self.tmp / "not_a_dir"
        clash.write_text("x", encoding="utf-8")
        s = _saver(clash)
        self.assertIsNone(s.start_session())
        self.assertIsNotNone(s.start_error())

    def test_writes_two_header_lines(self):
        s = _saver(self.tmp)
        path = s.start_session()
        self.assertIsNotNone(path)
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        self.assertEqual(["B\tR", "T\tohm"], lines,
                         "1행=라벨, 2행=단위 (두 줄 헤더)")

    def test_auto_numbering_increments(self):
        first = Path(_saver(self.tmp).start_session())
        second = Path(_saver(self.tmp).start_session())
        self.assertEqual("testX001.dat", first.name)
        self.assertEqual("testX002.dat", second.name,
                         "기존 파일을 덮어쓰면 안 된다")

    def test_include_date_creates_dated_subfolder(self):
        s = _saver(self.tmp, include_date=True)
        path = Path(s.start_session())
        today = date.today()
        self.assertEqual(today.strftime("%Y-%m-%d"), path.parent.name)
        self.assertIn(today.strftime("%Y%m%d"), path.name)

    def test_custom_folder_is_nested_under_main(self):
        s = _saver(self.tmp, custom_folder="run42")
        path = Path(s.start_session())
        self.assertEqual("run42", path.parent.name)

    def test_fixed_name_overrides_auto_numbering(self):
        s = _saver(self.tmp)
        s.set_fixed_name("trace.dat")
        path = Path(s.start_session())
        self.assertEqual("trace.dat", path.name)


class TestDataSaverAppend(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_append_writes_tab_separated_row(self):
        s = _saver(self.tmp)
        path = Path(s.start_session())
        self.assertTrue(s.append_row(["1.0", "2.0"]))
        self.assertTrue(s.append_row(["3.0", "nan"]))
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(["B\tR", "T\tohm", "1.0\t2.0", "3.0\tnan"], lines)

    def test_append_without_session_returns_false(self):
        s = _saver(self.tmp)
        self.assertFalse(s.append_row(["1.0"]),
                         "start_session 전 append는 실패를 알려야 한다")

    def test_append_when_disabled_returns_false(self):
        s = _saver(self.tmp, enabled=False)
        s.start_session()
        self.assertFalse(s.append_row(["1.0"]))

    def test_resume_appends_without_rewriting_header(self):
        s1 = _saver(self.tmp)
        path = s1.start_session()
        s1.append_row(["1.0", "2.0"])

        s2 = _saver(self.tmp)
        self.assertEqual(path, s2.resume_session(path))
        s2.append_row(["3.0", "4.0"])

        lines = Path(path).read_text(encoding="utf-8").splitlines()
        self.assertEqual(["B\tR", "T\tohm", "1.0\t2.0", "3.0\t4.0"], lines)

    def test_resume_missing_file_returns_none(self):
        s = _saver(self.tmp)
        self.assertIsNone(s.resume_session(str(self.tmp / "nope.dat")))


if __name__ == "__main__":
    unittest.main()
