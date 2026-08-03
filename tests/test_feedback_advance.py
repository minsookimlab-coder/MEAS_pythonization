"""FEEDBACK advance 의 판정 계산.

자기장처럼 명령을 줘도 실제 도달까지 시간이 걸리는 채널에 쓴다. 판정이 너무
빡빡하면 측정이 영영 진행되지 않고, 너무 헐거우면 아직 흔들리는 값에서 측정을
시작한다 — 둘 다 수십 시간짜리 측정을 버린다.
"""
import math
import unittest

from pythonization.measurement.second_channel_worker import (
    _feedback_band,
    _stability_metric,
)


class TestFeedbackBand(unittest.TestCase):
    """도달 판정 허용 오차 = max((1-tol%)·이동거리, noisefloor)"""

    def test_large_move_uses_ratio(self):
        # 1 T 이동을 95% 허용 → 0.05 T 안에 들어오면 도달
        self.assertAlmostEqual(0.05, _feedback_band(95.0, 1.0, 0.0))

    def test_tighter_tolerance_narrows_band(self):
        self.assertAlmostEqual(0.02, _feedback_band(98.0, 1.0, 0.0))

    def test_noisefloor_is_absolute_lower_bound(self):
        """작은 이동에서 비율 허용오차가 노이즈보다 작아지면 noisefloor 가 이긴다.

        이게 없으면 시작점이 목표에 가까울 때 노이즈보다 작은 오차를 요구하게 되어
        영영 도달 판정이 나지 않는다.
        """
        # 0.001 T 이동 × 2% = 0.00002 T — 자기장 노이즈보다 작다
        self.assertAlmostEqual(0.001, _feedback_band(98.0, 0.001, 0.001))

    def test_ratio_wins_when_larger_than_noisefloor(self):
        self.assertAlmostEqual(0.05, _feedback_band(95.0, 1.0, 0.001))

    def test_zero_distance_falls_back_to_noisefloor(self):
        self.assertAlmostEqual(0.001, _feedback_band(95.0, 0.0, 0.001))

    def test_hundred_percent_tolerance_requires_noisefloor(self):
        # tol=100% 면 비율 항이 0 — noisefloor 가 없으면 정확히 일치해야만 도달
        self.assertAlmostEqual(0.0, _feedback_band(100.0, 1.0, 0.0))


class TestStabilityMetric(unittest.TestCase):
    """안정화 지표 = 표준편차 / (|목표값| + noisefloor)"""

    def test_constant_samples_are_perfectly_stable(self):
        self.assertAlmostEqual(0.0, _stability_metric([1.0, 1.0, 1.0], 1.0, 0.0))

    def test_scales_by_target_not_mean(self):
        """목표값으로 정규화한다 — overshoot 로 평균이 치우쳐도 척도가 일정하다."""
        samples = [9.0, 11.0]           # 표준편차 1.0, 평균 10.0
        # 목표가 100 이면 1/100, 평균(10)으로 나눴다면 1/10 이 됐을 것
        self.assertAlmostEqual(0.01, _stability_metric(samples, 100.0, 0.0))

    def test_noisefloor_prevents_divide_by_zero_near_target_zero(self):
        # 목표가 0 이면 noisefloor 가 분모를 잡아 준다
        metric = _stability_metric([-0.001, 0.001], 0.0, 0.001)
        self.assertTrue(math.isfinite(metric))
        self.assertAlmostEqual(1.0, metric)

    def test_zero_scale_is_infinite_not_a_crash(self):
        # 목표 0 + noisefloor 0 → 판정 불가. 예외 대신 inf 로 '아직 불안정' 처리
        self.assertEqual(float("inf"), _stability_metric([1.0, 2.0], 0.0, 0.0))

    def test_empty_window_is_infinite(self):
        self.assertEqual(float("inf"), _stability_metric([], 1.0, 0.0))

    def test_uses_population_standard_deviation(self):
        # 표본(n-1)이 아니라 모집단(n) 표준편차 — 창 크기가 고정이라 일관성이 우선
        samples = [0.0, 2.0]
        self.assertAlmostEqual(1.0, _stability_metric(samples, 1.0, 0.0))

    def test_realistic_magnet_case(self):
        """자기장 권장값(threshold 0.0003)에서 실제로 통과/탈락하는지."""
        target, noisefloor = 1.0, 0.001
        steady = [1.0000, 1.0001, 0.9999, 1.0000]
        noisy = [1.000, 1.010, 0.990, 1.005]
        self.assertLess(_stability_metric(steady, target, noisefloor), 0.0003)
        self.assertGreater(_stability_metric(noisy, target, noisefloor), 0.0003)


if __name__ == "__main__":
    unittest.main()
