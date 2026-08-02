"""mfli_merge — LabOne sweep CSV 와 MFLI 모듈 .dat 의 sweep 단위 병합.

Qt·numpy 비의존 순수 로직이라 전부 직접 검증할 수 있다. 특히 두 가지 회귀를
막는다:
  1. 파일 정렬을 문자열로 하면 x1000 이 x101 앞에 와서 1000 sweep 측정이 통째로 밀린다.
  2. 우리 주파수를 rank(순번)로 grid에 붙이면 poll로 놓친 점 때문에 전부 어긋난다.
     → '로그-주파수 최근접' 매칭이어야 한다.
"""
import math
import tempfile
import unittest
from pathlib import Path

from core.mfli_merge import (
    _num_key,
    interp_clamped,
    iter_our_sweeps,
    merge_pair,
    parse_ours,
    split_by_wrap,
    write_dat,
)


class TestNaturalSort(unittest.TestCase):
    def test_numeric_order_beats_string_order(self):
        names = ["test_x1000", "test_x101", "test_x9", "test_x1"]
        self.assertEqual(
            ["test_x1", "test_x9", "test_x101", "test_x1000"],
            sorted(names, key=_num_key),
            "문자열 정렬이면 x1000이 x101 앞에 온다 — 1000 sweep에서 순서가 깨진다",
        )

    def test_name_without_number(self):
        self.assertEqual((-1, "plain"), _num_key("plain"))


class TestSplitByWrap(unittest.TestCase):
    def test_empty(self):
        self.assertEqual([], split_by_wrap([]))

    def test_single_monotonic_sweep_is_one_segment(self):
        self.assertEqual([(0, 4)], split_by_wrap([1.0, 2.0, 3.0, 4.0]))

    def test_wrap_starts_new_segment(self):
        # 1,2,3 → 1,2,3 : 3→1 이 전체 범위의 50%를 넘는 역방향 점프
        self.assertEqual([(0, 3), (3, 6)],
                         split_by_wrap([1.0, 2.0, 3.0, 1.0, 2.0, 3.0]))


class TestInterpClamped(unittest.TestCase):
    def test_midpoint(self):
        self.assertAlmostEqual(50.0, interp_clamped(5.0, [0.0, 10.0], [0.0, 100.0]))

    def test_below_range_clamps_to_first(self):
        self.assertAlmostEqual(0.0, interp_clamped(-1.0, [0.0, 10.0], [0.0, 100.0]))

    def test_above_range_clamps_to_last(self):
        self.assertAlmostEqual(100.0, interp_clamped(99.0, [0.0, 10.0], [0.0, 100.0]))

    def test_single_point(self):
        self.assertAlmostEqual(7.0, interp_clamped(123.0, [1.0], [7.0]))

    def test_empty_is_nan(self):
        self.assertTrue(math.isnan(interp_clamped(1.0, [], [])))


class TestDatRoundTrip(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_write_then_parse(self):
        path = self.tmp / "out.dat"
        write_dat(path, ["frequency", "probe_T"], ["Hz", "K"],
                  [[100.0, 4.2], [200.0, 4.3]])
        labels, units, cols = parse_ours(path)
        self.assertEqual(["frequency", "probe_T"], labels)
        self.assertEqual(["Hz", "K"], units)
        self.assertAlmostEqual(100.0, cols["frequency"][0])
        self.assertAlmostEqual(4.3, cols["probe_T"][1])

    def test_unparseable_cell_becomes_nan(self):
        path = self.tmp / "bad.dat"
        path.write_text("frequency\tprobe_T\nHz\tK\n100.0\tERR\n", encoding="utf-8")
        _labels, _units, cols = parse_ours(path)
        self.assertTrue(math.isnan(cols["probe_T"][0]))

    def test_iter_our_sweeps_sorts_folder_numerically(self):
        # 파일당 단조 증가 3점 → wrap 없이 sweep 1개로 잡힌다
        for n in (1, 9, 101, 1000):
            write_dat(self.tmp / f"run_x{n}.dat", ["frequency"], ["Hz"],
                      [[float(n)], [float(n) + 1], [float(n) + 2]])
        # 병합 결과 파일은 입력에서 제외되어야 한다
        write_dat(self.tmp / "sweep_1_merged_data.dat", ["frequency"], ["Hz"],
                  [[0.0]])
        sweeps = iter_our_sweeps(self.tmp)
        first_freqs = [cols["frequency"][a] for (_l, _u, cols, (a, _b), _n) in sweeps]
        self.assertEqual([1.0, 9.0, 101.0, 1000.0], first_freqs)


class TestMergePair(unittest.TestCase):
    """LabOne grid 전체가 기준 행이고, 우리가 못 읽은 점은 NaN으로 남는다."""

    def _lab(self):
        return {
            "grid":      [100.0, 200.0, 300.0, 400.0],
            "x":         [1.0, 2.0, 3.0, 4.0],
            "y":         [0.1, 0.2, 0.3, 0.4],
            "r":         [1.1, 2.2, 3.3, 4.4],
            "xstddev":   [2.0, 2.0, 2.0, 2.0],
            "rstddev":   [4.0, 4.0, 4.0, 4.0],
            "bandwidth": [4.0, 4.0, 4.0, 4.0],
        }

    def test_row_count_follows_labone_grid(self):
        # 우리는 4점 중 2점만 읽었지만 결과는 grid 크기(4행)이다
        cols = {"frequency": [100.0, 300.0], "probe_T": [4.2, 4.4]}
        labels, units, rows = merge_pair(
            self._lab(), (0, 2), ["frequency", "probe_T"], ["Hz", "K"], cols)
        self.assertEqual(4, len(rows))
        self.assertEqual(
            ["LabOne_frequency", "Module_frequency", "x", "y", "r",
             "X_noise", "R_noise", "NEPBW", "probe_T"], labels)
        self.assertEqual(["Hz", "Hz", "V", "V", "V",
                          "V/sqrtHz", "V/sqrtHz", "Hz", "K"], units)

    def test_matched_points_carry_our_values(self):
        cols = {"frequency": [100.0, 300.0], "probe_T": [4.2, 4.4]}
        _l, _u, rows = merge_pair(
            self._lab(), (0, 2), ["frequency", "probe_T"], ["Hz", "K"], cols)
        by_grid = {r[0]: r for r in rows}
        self.assertAlmostEqual(100.0, by_grid[100.0][1])   # Module_frequency
        self.assertAlmostEqual(4.2, by_grid[100.0][-1])    # probe_T
        self.assertAlmostEqual(300.0, by_grid[300.0][1])
        self.assertAlmostEqual(4.4, by_grid[300.0][-1])

    def test_unread_grid_points_are_nan(self):
        cols = {"frequency": [100.0, 300.0], "probe_T": [4.2, 4.4]}
        _l, _u, rows = merge_pair(
            self._lab(), (0, 2), ["frequency", "probe_T"], ["Hz", "K"], cols)
        by_grid = {r[0]: r for r in rows}
        for f in (200.0, 400.0):
            self.assertTrue(math.isnan(by_grid[f][1]), "Module_frequency는 NaN")
            self.assertTrue(math.isnan(by_grid[f][-1]), "aux도 NaN")
            # 노이즈는 LabOne 값이라 모든 grid 점에 존재해야 한다
            self.assertFalse(math.isnan(by_grid[f][5]), "X_noise는 있어야 한다")

    def test_noise_level_is_stddev_over_sqrt_bandwidth(self):
        cols = {"frequency": [100.0], "probe_T": [4.2]}
        _l, _u, rows = merge_pair(
            self._lab(), (0, 1), ["frequency", "probe_T"], ["Hz", "K"], cols)
        # xstddev 2 / sqrt(4) = 1.0,  rstddev 4 / sqrt(4) = 2.0
        self.assertAlmostEqual(1.0, rows[0][5])
        self.assertAlmostEqual(2.0, rows[0][6])

    def test_matching_is_by_frequency_not_rank(self):
        """우리가 첫 점을 놓쳐도 나머지가 밀리지 않아야 한다.

        rank 매칭이면 300→grid[0](100), 400→grid[1](200) 으로 통째로 어긋난다.
        """
        cols = {"frequency": [300.0, 400.0], "probe_T": [3.0, 4.0]}
        _l, _u, rows = merge_pair(
            self._lab(), (0, 2), ["frequency", "probe_T"], ["Hz", "K"], cols)
        by_grid = {r[0]: r for r in rows}
        self.assertAlmostEqual(300.0, by_grid[300.0][1])
        self.assertAlmostEqual(400.0, by_grid[400.0][1])
        self.assertTrue(math.isnan(by_grid[100.0][1]), "안 읽은 점은 비어 있어야 한다")

    def test_empty_grid_returns_none(self):
        self.assertIsNone(merge_pair({"grid": []}, (0, 0), ["frequency"], ["Hz"],
                                     {"frequency": []}))


if __name__ == "__main__":
    unittest.main()
