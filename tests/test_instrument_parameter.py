"""_parse_float — 계측기 응답 문자열 → float.

Mercury iTC/iPS 는 'STAT:...:TEMP:235.7446K' 처럼 경로+단위가 붙은 응답을 준다.
반면 모호한 응답에서 아무 숫자나 뽑으면 조용히 잘못된 측정값이 기록되므로,
'못 읽겠으면 에러'가 이 함수의 계약이다.
"""
import unittest

from core.instrument_parameter import _parse_float


class TestParseFloat(unittest.TestCase):
    def test_plain_number(self):
        self.assertAlmostEqual(1.23, _parse_float("1.23"))

    def test_scientific_notation(self):
        self.assertAlmostEqual(-4.5e-3, _parse_float("-4.5e-3"))

    def test_surrounding_whitespace(self):
        self.assertAlmostEqual(7.0, _parse_float("  7.0  "))

    def test_mercury_path_response_with_unit(self):
        raw = "STAT:DEV:MB1.T1:TEMP:SIG:TEMP:235.7446K"
        self.assertAlmostEqual(235.7446, _parse_float(raw))

    def test_mercury_response_negative_value(self):
        self.assertAlmostEqual(-1.5, _parse_float("STAT:DEV:X:SIG:FLD:-1.5T"))

    def test_ambiguous_ratio_is_rejected(self):
        # '12.5/3.0' 에서 12.5를 뽑아버리면 조용한 오측정이 된다
        with self.assertRaises(ValueError):
            _parse_float("12.5/3.0")

    def test_ambiguous_multi_dot_is_rejected(self):
        with self.assertRaises(ValueError):
            _parse_float("1.2.3")

    def test_trailing_number_is_rejected(self):
        with self.assertRaises(ValueError):
            _parse_float("3 of 5")

    def test_non_numeric_is_rejected(self):
        with self.assertRaises(ValueError):
            _parse_float("ERROR")

    def test_empty_is_rejected(self):
        with self.assertRaises(ValueError):
            _parse_float("")


if __name__ == "__main__":
    unittest.main()
