"""calculate_next_step / SweepConfig — sweep 진행 규칙.

이 함수가 틀리면 계측기에 잘못된 값이 써진다. 리팩터링으로 절대 바뀌면 안 되는
동작을 고정한다.
"""
import unittest

from core.sweep import SweepConfig, calculate_next_step


class TestCalculateNextStep(unittest.TestCase):
    # rate 60 units/min + 1 s/point → 스텝당 정확히 1 unit
    def test_forward_step(self):
        nxt, done = calculate_next_step(0.0, 10.0, 60.0, 1.0)
        self.assertAlmostEqual(1.0, nxt)
        self.assertFalse(done)

    def test_backward_step(self):
        nxt, done = calculate_next_step(10.0, 0.0, 60.0, 1.0)
        self.assertAlmostEqual(9.0, nxt)
        self.assertFalse(done)

    def test_overshoot_is_clamped_to_target(self):
        # 9.5 + 1.0 = 10.5 > 10 → 목표값으로 clamp (넘어가지 않는다)
        nxt, done = calculate_next_step(9.5, 10.0, 60.0, 1.0)
        self.assertAlmostEqual(10.0, nxt)
        self.assertFalse(done, "clamp된 스텝은 아직 done이 아니다 (다음 호출에서 done)")

    def test_reaching_target_reports_done(self):
        nxt, done = calculate_next_step(10.0, 10.0, 60.0, 1.0)
        self.assertAlmostEqual(10.0, nxt)
        self.assertTrue(done)

    def test_within_epsilon_reports_done(self):
        # |diff| < 1e-11 이면 도달로 본다 (부동소수 오차 흡수)
        nxt, done = calculate_next_step(10.0 - 1e-13, 10.0, 60.0, 1.0)
        self.assertAlmostEqual(10.0, nxt)
        self.assertTrue(done)

    def test_negative_rate_does_not_run_away_from_target(self):
        """음수 rate가 들어와도 방향은 diff로만 정한다.

        회귀 방지: 부호를 rate에서 가져오면 목표 반대로 폭주한다.
        """
        nxt, _ = calculate_next_step(0.0, 10.0, -60.0, 1.0)
        self.assertAlmostEqual(1.0, nxt, msg="목표(10) 쪽으로 진행해야 한다")

    def test_negative_time_per_point_uses_magnitude(self):
        nxt, _ = calculate_next_step(0.0, 10.0, 60.0, -1.0)
        self.assertAlmostEqual(1.0, nxt)

    def test_zero_rate_raises(self):
        # 같은 값을 영원히 반환하며 조용히 멈추는 대신 명시적 오류로 끊는다
        with self.assertRaises(ValueError):
            calculate_next_step(0.0, 10.0, 0.0, 1.0)

    def test_zero_time_per_point_raises(self):
        with self.assertRaises(ValueError):
            calculate_next_step(0.0, 10.0, 60.0, 0.0)

    def test_full_sweep_terminates_and_never_overshoots(self):
        """0 → 1 을 0.3씩. 목표를 넘지 않고 유한 스텝 안에 done."""
        v, steps = 0.0, 0
        while steps < 1000:
            v, done = calculate_next_step(v, 1.0, 18.0, 1.0)  # 18/60 = 0.3
            steps += 1
            self.assertLessEqual(v, 1.0 + 1e-12, "목표를 초과했다")
            if done:
                break
        else:
            self.fail("1000 스텝 안에 종료하지 못했다")
        self.assertAlmostEqual(1.0, v)


class TestSweepConfig(unittest.TestCase):
    def test_step_size(self):
        cfg = SweepConfig(sweep_to=10.0, sweep_rate=30.0, time_per_point=2.0)
        self.assertAlmostEqual(1.0, cfg.step_size())  # 30/60 * 2

    def test_next_step_without_source_value(self):
        # 시작 전(기기에서 아직 안 읽음)에는 계산하지 않는다
        cfg = SweepConfig(sweep_to=10.0)
        self.assertEqual((None, False), cfg.next_step())

    def test_next_step_delegates(self):
        cfg = SweepConfig(source_value=0.0, sweep_to=10.0,
                          sweep_rate=60.0, time_per_point=1.0)
        nxt, done = cfg.next_step()
        self.assertAlmostEqual(1.0, nxt)
        self.assertFalse(done)


if __name__ == "__main__":
    unittest.main()
