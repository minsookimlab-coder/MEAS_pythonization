"""측정 리포트 — 그래프 PNG + 장비 값 + 측정 조건을 휴대폰으로 보내기.

실제 ntfy 서버 없이 127.0.0.1 가짜 서버로 확인한다:
  - 저장된 .dat 로 그래프 PNG 가 만들어진다 (matplotlib 없으면 글만)
  - 상수 열(설정값)은 자동 y 축에서 빠지고, 변하는 열만 그려진다
  - 리포트 본문에 측정 조건·장비 마지막 값·데이터 파일이 들어간다
  - send_report 가 글(JSON, markdown)과 그래프(PUT 첨부)를 각각 보낸다
  - 파일이 없거나 깨져도 리포트는 만들어지고 사유가 남는다
  - ntfy 가 꺼져 있으면 글은 기존 경로로 가고 그래프는 못 간다고 알린다
"""
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pythonization.config.models import AlarmDeliveryConfig            # noqa: E402
from pythonization.notify.alarm_manager import AlarmManager            # noqa: E402
from pythonization.notify.report import (Report, _pick_axes,           # noqa: E402
                                         build_report, last_values,
                                         render_graph_png)

PNG_MAGIC = b"\x89PNG"


def write_dat(path, n=50):
    """실제 저장 형식과 같은 .dat — 1행 헤더, 2행 단위, 그 뒤 탭 구분 숫자."""
    head = ["S21", "freq", "Temperature", "Bfield", "setpoint"]
    units = ["dB", "Hz", "K", "T", "T"]
    lines = ["\t".join(head), "\t".join(units)]
    for i in range(n):
        lines.append("\t".join([
            "%.6E" % (-40 - (i % 7)),        # 변함
            "%.6E" % (3.5e8 + i * 1e5),      # x 축
            "%.6E" % (2.0 + i * 0.001),      # 변함
            "%.6E" % (-4.0 + i * 0.01),      # 변함
            "1.000000E+00",                  # 상수 — 자동 y 축에서 빠져야 한다
        ]))
    path.write_text("\n".join(lines), encoding="utf-8")


def has_matplotlib():
    try:
        import matplotlib                    # noqa: F401
        return True
    except Exception:
        return False


class Handler(BaseHTTPRequestHandler):
    """POST / → JSON 발행, PUT /<topic> → 첨부 업로드."""

    def log_message(self, *a):
        pass

    def _read(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n)

    def do_POST(self):
        self.server.published.append(json.loads(self._read().decode("utf-8")))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def do_PUT(self):
        self.server.attachments.append({
            "path": self.path,
            "filename": self.headers.get("Filename"),
            "auth": self.headers.get("Authorization"),
            "body": self._read(),
        })
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")


class ReportBuildTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.dat = self.tmp / "vna_data_001.dat"
        write_dat(self.dat)

    def test_graph_and_body(self):
        rep = build_report(
            title="측정 리포트",
            conditions={"축": "Bfield", "범위": "-4 ~ 4 T", "속도": 0.1},
            data_file=self.dat, progress="12/20 (60%)")
        self.assertIn("측정 조건", rep.text)
        self.assertIn("범위: -4 ~ 4 T", rep.text)
        self.assertIn("장비 상태", rep.text)
        self.assertIn("Temperature", rep.text)
        self.assertIn("vna_data_001.dat", rep.text)
        self.assertIn("12/20", rep.text)
        if not has_matplotlib():
            self.skipTest("matplotlib 없음 — 글 리포트만 확인")
        self.assertTrue(rep.has_graph(), rep.warnings)
        self.assertTrue(rep.png.startswith(PNG_MAGIC), "PNG 시그니처가 아니다")
        self.assertEqual(rep.png_name, "vna_data_001.png")

    def test_constant_column_excluded_from_auto_axes(self):
        from pythonization.automation.summarize import read_dat
        headers, _units, rows = read_dat(self.dat)
        x, ys = _pick_axes(headers, rows, None, None)
        self.assertEqual(x, "freq")                   # 이름 우선순위
        self.assertNotIn("setpoint", ys)              # 상수 열은 빠진다
        self.assertIn("S21", ys)

    def test_explicit_axes(self):
        rep = build_report(title="t", data_file=self.dat,
                           x_col="Bfield", y_cols=["S21"])
        if not has_matplotlib():
            self.skipTest("matplotlib 없음")
        self.assertTrue(rep.has_graph(), rep.warnings)

    def test_instrument_values_override(self):
        rep = build_report(title="t", data_file=self.dat,
                           instrument_values={"VTI": 22.0, "Magnet": -3.88})
        self.assertIn("VTI: 22", rep.text)
        self.assertIn("Magnet: -3.88", rep.text)

    def test_missing_file_still_builds(self):
        rep = build_report(title="t", conditions={"축": "Bfield"},
                           data_file=self.tmp / "nope.dat")
        self.assertIn("측정 조건", rep.text)
        self.assertTrue(rep.warnings)
        self.assertFalse(rep.has_graph())

    def test_no_data_file_at_all(self):
        rep = build_report(title="t", conditions={"a": 1}, progress="시작")
        self.assertIn("진행", rep.text)
        self.assertFalse(rep.has_graph())

    def test_last_values(self):
        from pythonization.automation.summarize import read_dat
        headers, _units, rows = read_dat(self.dat)
        vals = last_values(headers, rows, skip=["S21"])
        self.assertNotIn("S21", vals)
        self.assertAlmostEqual(vals["Bfield"], -4.0 + 49 * 0.01, places=6)

    def test_render_returns_none_without_columns(self):
        self.assertIsNone(render_graph_png(["a"], [[1.0]], "a", []))


class SendReportTest(unittest.TestCase):

    def setUp(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.srv.allow_reuse_address = True
        self.srv.published = []
        self.srv.attachments = []
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        self.tmp = Path(tempfile.mkdtemp())
        self.dat = self.tmp / "run_007.dat"
        write_dat(self.dat)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def _delivery(self):
        return AlarmDeliveryConfig(use_ntfy=True, ntfy_server=self.url,
                                   ntfy_topic="lab-topic", ntfy_token="tk_x")

    def test_sends_text_and_graph(self):
        rep = build_report(title="자기장 sweep", conditions={"축": "Bfield"},
                           data_file=self.dat, progress="3/20")
        with mock.patch("pythonization.config.app_config.load_alarm_delivery",
                        return_value=self._delivery()):
            errs = AlarmManager().send_report(rep)
        self.assertEqual(errs, [])
        self.assertEqual(len(self.srv.published), 1)
        msg = self.srv.published[0]
        self.assertEqual(msg["topic"], "lab-topic")
        self.assertEqual(msg["title"], "자기장 sweep")
        self.assertTrue(msg.get("markdown"), "마크다운으로 보내야 목록이 보인다")
        self.assertIn("측정 조건", msg["message"])
        if not has_matplotlib():
            return
        self.assertEqual(len(self.srv.attachments), 1)
        att = self.srv.attachments[0]
        self.assertEqual(att["path"], "/lab-topic")
        self.assertEqual(att["filename"], "run_007.png")
        self.assertEqual(att["auth"], "Bearer tk_x")
        self.assertTrue(att["body"].startswith(PNG_MAGIC))

    def test_text_goes_even_if_graph_missing(self):
        rep = Report(title="t", text="본문", png=None)
        with mock.patch("pythonization.config.app_config.load_alarm_delivery",
                        return_value=self._delivery()):
            errs = AlarmManager().send_report(rep)
        self.assertEqual(errs, [])
        self.assertEqual(len(self.srv.published), 1)
        self.assertEqual(self.srv.attachments, [])

    def test_graph_failure_does_not_lose_text(self):
        rep = Report(title="t", text="본문", png=PNG_MAGIC + b"-fake",
                     png_name="g.png")
        with mock.patch("pythonization.config.app_config.load_alarm_delivery",
                        return_value=self._delivery()), \
             mock.patch("pythonization.notify.ntfy.publish_attachment",
                        return_value="413 too large"):
            errs = AlarmManager().send_report(rep)
        self.assertEqual(len(self.srv.published), 1)        # 글은 갔다
        self.assertTrue(any("그래프" in e for e in errs))

    def test_without_ntfy_falls_back_to_text_channels(self):
        rep = Report(title="t", text="본문", png=PNG_MAGIC, png_name="g.png")
        off = AlarmDeliveryConfig(use_ntfy=False)
        with mock.patch("pythonization.config.app_config.load_alarm_delivery",
                        return_value=off):
            errs = AlarmManager().send_report(rep)
        self.assertEqual(self.srv.published, [])
        self.assertTrue(any("ntfy" in e for e in errs))


if __name__ == "__main__":
    unittest.main()
