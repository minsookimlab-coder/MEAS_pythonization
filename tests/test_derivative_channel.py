"""DerivativeChannel — 슬라이딩 윈도우 실시간 미분.

측정 중 GUI 스레드에서 매 스텝 호출되므로, 값이 없을 때 예외 대신 None을
반환하는 계약이 중요하다(예외가 나면 sweep 슬롯이 죽는다).
"""
import unittest

from pythonization.measurement.derivative import (
    OUTPUT_KEY,
    OUTPUT_KEY_2,
    OUTPUT_KEY_3,
    DerivativeChannel,
    DerivativeConfig,
)


def _push_all(ch, pairs):
    out = None
    for a1, a2 in pairs:
        out = ch.push(a1, a2)
    return out


class TestDerivativeChannel(unittest.TestCase):
    def test_first_order_slope(self):
        # A1 = 3 * A2 → dA1/dA2 = 3
        ch = DerivativeChannel(DerivativeConfig(order=1, window_size=5))
        val = _push_all(ch, [(3.0 * x, float(x)) for x in range(5)])
        self.assertIsNotNone(val)
        self.assertAlmostEqual(3.0, val, places=6)

    def test_second_order(self):
        # A1 = 2 * A2² → d²A1/dA2² = 4
        ch = DerivativeChannel(DerivativeConfig(order=2, window_size=7))
        val = _push_all(ch, [(2.0 * x * x, float(x)) for x in range(7)])
        self.assertIsNotNone(val)
        self.assertAlmostEqual(4.0, val, places=6)

    def test_third_order(self):
        # A1 = A2³ → d³A1/dA2³ = 6
        ch = DerivativeChannel(DerivativeConfig(order=3, window_size=9))
        val = _push_all(ch, [(float(x) ** 3, float(x)) for x in range(9)])
        self.assertIsNotNone(val)
        self.assertAlmostEqual(6.0, val, places=5)

    def test_returns_none_until_enough_points(self):
        ch = DerivativeChannel(DerivativeConfig(order=1, window_size=10))
        self.assertIsNone(ch.push(0.0, 0.0))
        self.assertIsNone(ch.push(3.0, 1.0))
        self.assertIsNotNone(ch.push(6.0, 2.0), "order=1은 3점이면 계산 가능")

    def test_constant_denominator_returns_none(self):
        # ΔA2 = 0 → div/zero 대신 None (min_delta guard)
        ch = DerivativeChannel(DerivativeConfig(order=1, window_size=5))
        val = _push_all(ch, [(float(x), 5.0) for x in range(5)])
        self.assertIsNone(val)

    def test_savgol_method_falls_back_when_scipy_missing(self):
        """scipy가 없어도 예외 없이 poly로 fallback 해 값을 낸다."""
        ch = DerivativeChannel(DerivativeConfig(order=1, window_size=7,
                                                method="savgol"))
        val = _push_all(ch, [(3.0 * x, float(x)) for x in range(7)])
        self.assertIsNotNone(val)
        self.assertAlmostEqual(3.0, val, places=6)

    def test_reset_clears_buffer(self):
        ch = DerivativeChannel(DerivativeConfig(order=1, window_size=5))
        _push_all(ch, [(3.0 * x, float(x)) for x in range(5)])
        ch.reset()
        self.assertIsNone(ch.push(0.0, 0.0), "reset 후에는 다시 점이 모자라야 한다")

    def test_window_size_is_clamped(self):
        # 3 ≤ N ≤ 50 으로 clamp — 0이나 10000을 넣어도 죽지 않는다
        for n in (0, 1, 10_000):
            ch = DerivativeChannel(DerivativeConfig(order=1, window_size=n))
            _push_all(ch, [(3.0 * x, float(x)) for x in range(60)])

    def test_output_key_per_order(self):
        for order, key in ((1, OUTPUT_KEY), (2, OUTPUT_KEY_2), (3, OUTPUT_KEY_3)):
            ch = DerivativeChannel(DerivativeConfig(order=order))
            self.assertEqual(key, ch.output_key)

    def test_col_info_uses_explicit_label(self):
        ch = DerivativeChannel(DerivativeConfig(order=1, output_label="dV/dI",
                                                output_unit="ohm"))
        self.assertEqual((OUTPUT_KEY, "dV/dI", "ohm"), ch.col_info())

    def test_col_info_generates_label_from_keys(self):
        ch = DerivativeChannel(DerivativeConfig(
            order=2, numerator_key="__sweep__", denominator_key="current"))
        _key, label, _unit = ch.col_info()
        self.assertEqual("d²sweep__/dcurrent²", label)


if __name__ == "__main__":
    unittest.main()
