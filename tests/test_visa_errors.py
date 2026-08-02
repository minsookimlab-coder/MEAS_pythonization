"""is_comm_error — 통신 오류 vs 그 외 구분.

이 판정이 자동 재개(retry) 경로를 가른다. 통신 오류를 놓치면 장시간 측정이
그냥 죽고, 반대로 파싱 오류를 통신 오류로 오인하면 잘못된 값으로 무한 재시도한다.
"""
import unittest

from core.visa_errors import is_comm_error


class TestIsCommError(unittest.TestCase):
    def test_none_is_not_an_error(self):
        self.assertFalse(is_comm_error(None))

    def test_timeout_exception(self):
        self.assertTrue(is_comm_error(TimeoutError("timed out")))

    def test_connection_reset_exception(self):
        self.assertTrue(is_comm_error(ConnectionResetError()))

    def test_broken_pipe_exception(self):
        self.assertTrue(is_comm_error(BrokenPipeError()))

    def test_os_error_is_comm(self):
        self.assertTrue(is_comm_error(OSError("WinError 10054")))

    def test_visa_timeout_text(self):
        self.assertTrue(is_comm_error("VI_ERROR_TMO: Timeout expired"))

    def test_resource_not_found_text(self):
        self.assertTrue(is_comm_error("VI_ERROR_RSRC_NFOUND"))

    def test_parse_failure_is_not_comm_error(self):
        # 측정값 파싱 실패는 재시도 대상이 아니다 — 즉시 중단해야 한다
        self.assertFalse(is_comm_error(ValueError("숫자 해석 실패(모호하거나 비숫자)")))

    def test_plain_text_is_not_comm_error(self):
        self.assertFalse(is_comm_error("측정 완료"))


if __name__ == "__main__":
    unittest.main()
