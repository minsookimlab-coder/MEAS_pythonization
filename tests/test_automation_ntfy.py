"""ntfy 연동 — 알람·AI 보고 발행, 휴대폰 지시 수신, 승인 버튼 (가짜 ntfy 서버로).

실험실 PC 의 ntfy 서버를 흉내 내는 127.0.0.1 서버를 띄워 확인한다:
  - JSON 발행 형식(주제·제목·우선순위·버튼)과 토큰 헤더
  - 구독 스트림에서 새 메시지만 받고, 끊기면 마지막 메시지 id 부터 다시 받는다
  - 기존 알람(Double Sweep·VNA)과 AI 보고가 ntfy 로 나간다
  - 기존 알람 설정 창이 저장해도 ntfy 설정이 지워지지 않는다
  - 휴대폰 글: 상태·중지·승인/거부·지시가 알맞은 곳으로 간다
  - 승인 요청 알림의 버튼은 '휴대폰 접속 주소'로 '/approve <번호>' 를 보낸다
"""
import json
import os
import queue
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace as NS
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pythonization.automation.remote import parse_remote                 # noqa: E402
from pythonization.config.models import AlarmConfig, AlarmDeliveryConfig  # noqa: E402
from pythonization.notify import ntfy                                     # noqa: E402
from pythonization.notify.alarm_manager import AlarmManager              # noqa: E402

TOKEN = "tk_testtoken"


class FakeNtfy:
    """POST / (JSON 발행), POST /<topic> (버튼·직접 발행), GET /<topic>/json (구독)."""

    def __init__(self, require_token=True):
        self.published = []           # JSON 발행 본문
        self.topics = {}              # topic → queue
        self.subscribe_requests = []  # (topic, query)
        self.close_after_one = False
        self.require_token = require_token
        self.stop = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _auth_ok(self):
                return (not outer.require_token
                        or self.headers.get("Authorization") == f"Bearer {TOKEN}")

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if not self._auth_ok():
                    self.send_response(403)
                    self.end_headers()
                    self.wfile.write(b'{"code":40301,"error":"forbidden"}')
                    return
                path = self.path.strip("/")
                if path == "":
                    msg = json.loads(body)
                    outer.published.append(msg)
                    outer.queue(msg["topic"]).put(msg["message"])
                else:
                    outer.queue(path).put(body.decode("utf-8"))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"id":"x"}')

            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                topic = parsed.path.strip("/").split("/")[0]
                outer.subscribe_requests.append((topic, urllib.parse.parse_qs(parsed.query)))
                if not self._auth_ok():
                    self.send_response(403)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                self.wfile.write(json.dumps({"id": "o1", "event": "open"}).encode() + b"\n")
                self.wfile.flush()
                q = outer.queue(topic)
                n = 0
                while not outer.stop.is_set():
                    try:
                        text = q.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    n += 1
                    line = {"id": f"m{len(outer.subscribe_requests)}-{n}", "event": "message",
                            "time": int(time.time()), "topic": topic, "message": text}
                    self.wfile.write(json.dumps(line).encode() + b"\n")
                    self.wfile.write(json.dumps({"event": "keepalive"}).encode() + b"\n")
                    self.wfile.flush()
                    if outer.close_after_one:
                        return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"

    def queue(self, topic):
        return self.topics.setdefault(topic, queue.Queue())

    def close(self):
        self.stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()


def wait_for(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


class NtfyCase(unittest.TestCase):
    def setUp(self):
        self.srv = FakeNtfy()
        self.addCleanup(self.srv.close)

    def delivery(self, **kw):
        base = dict(use_sound=False, use_ntfy=True, ntfy_server=self.srv.url,
                    ntfy_public_url="http://192.168.0.20:2586", ntfy_topic="lab-report",
                    ntfy_command_topic="lab-cmd", ntfy_token=TOKEN,
                    ntfy_commands_enabled=True)
        base.update(kw)
        return AlarmDeliveryConfig(**base)


class TestPublish(NtfyCase):
    def test_json_publish_with_title_priority_and_buttons(self):
        actions = [ntfy.http_action("승인", "http://192.168.0.20:2586", "lab-cmd",
                                    "/approve 1234", TOKEN)]
        err = ntfy.publish(self.srv.url, "lab-report", "게이트 누설 감지", title="AI 승인 요청",
                           priority=4, tags=["raised_hand"], actions=actions, token=TOKEN)
        self.assertIsNone(err)
        msg = self.srv.published[0]
        self.assertEqual(("lab-report", "게이트 누설 감지", "AI 승인 요청", 4),
                         (msg["topic"], msg["message"], msg["title"], msg["priority"]))
        button = msg["actions"][0]
        self.assertEqual("http://192.168.0.20:2586/lab-cmd", button["url"])
        self.assertEqual("/approve 1234", button["body"])
        self.assertEqual(f"Bearer {TOKEN}", button["headers"]["Authorization"])

    def test_wrong_token_reports_permission_hint(self):
        err = ntfy.publish(self.srv.url, "lab-report", "x", token="tk_wrong")
        self.assertIn("403", err)
        self.assertIn("토큰", err)

    def test_unreachable_server_returns_reason(self):
        err = ntfy.publish("http://127.0.0.1:9", "t", "x", timeout=2)
        self.assertTrue(err)

    def test_long_korean_message_clipped_under_limit(self):
        ntfy.publish(self.srv.url, "lab-report", "가" * 5000, token=TOKEN)
        self.assertLessEqual(len(self.srv.published[0]["message"].encode("utf-8")), 3800)

    def test_random_topic_is_valid(self):
        t = ntfy.random_topic("pyz-cmd")
        self.assertTrue(t.startswith("pyz-cmd-"))
        self.assertLessEqual(len(t), 64)
        self.assertTrue(all(c.isalnum() or c in "-_" for c in t))
        self.assertNotEqual(t, ntfy.random_topic("pyz-cmd"))


class TestSubscribe(NtfyCase):
    def test_receives_new_messages_and_resumes_from_last_id(self):
        got = []
        self.srv.close_after_one = True      # 메시지 하나 보내고 스트림을 끊는다
        sub = ntfy.NtfySubscriber(self.srv.url, "lab-cmd", lambda ev: got.append(ev),
                                  token=TOKEN)
        with mock.patch.object(ntfy, "_RECONNECT_MIN_S", 0.05):
            sub.start()
            self.addCleanup(sub.stop)
            self.assertTrue(wait_for(lambda: self.srv.subscribe_requests))
            first_since = self.srv.subscribe_requests[0][1]["since"][0]
            self.assertTrue(first_since.isdigit(), "처음에는 '지금 이후'만 받는다")
            self.srv.queue("lab-cmd").put("게이트를 0 V 로")
            self.assertTrue(wait_for(lambda: len(got) == 1))
            self.assertTrue(wait_for(lambda: len(self.srv.subscribe_requests) >= 2))
            self.assertEqual(got[0]["id"], self.srv.subscribe_requests[1][1]["since"][0],
                             "다시 붙을 때 마지막 메시지 id 부터")
            self.srv.queue("lab-cmd").put("상태")
            self.assertTrue(wait_for(lambda: len(got) == 2))
        self.assertEqual(["게이트를 0 V 로", "상태"], [e["message"] for e in got])
        self.assertTrue(all(e["event"] == "message" for e in got), "keepalive 는 건너뛴다")


class TestRemoteParsing(unittest.TestCase):
    def test_commands(self):
        cases = {
            "중지": ("stop", "", ""), "/stop": ("stop", "", ""), "상태": ("status", "", ""),
            "도움말": ("help", "", ""), "승인 1234": ("approve", "1234", ""),
            "/approve 1234": ("approve", "1234", ""),
            "거부 1234 속도가 너무 빠름": ("reject", "1234", "속도가 너무 빠름"),
            "/reject 99": ("reject", "99", ""),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                c = parse_remote(text)
                self.assertEqual(expected, (c.kind, c.arg, c.text))

    def test_sentences_are_instructions(self):
        for text in ("측정 중지하고 게이트를 0 V 로", "승인 해줘 빨리 끝내자", "게이트 -20→20 V 스윕"):
            with self.subTest(text=text):
                self.assertEqual("instruction", parse_remote(text).kind)
        self.assertEqual("empty", parse_remote("   ").kind)


class TestAlarmsGoToNtfy(NtfyCase):
    def test_ai_report_and_urgent_priority(self):
        with mock.patch("pythonization.config.app_config.load_alarm_delivery",
                        return_value=self.delivery()):
            self.assertEqual([], AlarmManager().send_message("sweep 시작", urgent=False))
            self.assertEqual([], AlarmManager().send_message("누설!", urgent=True))
        normal, urgent = self.srv.published
        self.assertEqual(("sweep 시작", "Pythonization AI"), (normal["message"], normal["title"]))
        self.assertNotIn("priority", normal)
        self.assertEqual(5, urgent["priority"])

    def test_existing_alarm_fire_goes_to_ntfy(self):
        with mock.patch("pythonization.config.app_config.load_alarm_delivery",
                        return_value=self.delivery()):
            AlarmManager()._fire_async(AlarmConfig(enabled=True, use_sound=False), "통신 오류")
        self.assertEqual("통신 오류", self.srv.published[0]["message"])
        self.assertEqual(5, self.srv.published[0]["priority"])

    def test_nothing_configured_is_reported(self):
        with mock.patch("pythonization.config.app_config.load_alarm_delivery",
                        return_value=AlarmDeliveryConfig(use_sound=False)):
            errors = AlarmManager().send_message("x")
        self.assertTrue(errors and "ntfy" in errors[0])

    def test_alarm_dialog_save_keeps_ntfy_settings(self):
        from PySide6.QtWidgets import QApplication
        from pythonization.ui.dialogs import alarm_config
        QApplication.instance() or QApplication([])
        saved = []
        with mock.patch.object(alarm_config, "load_alarm_delivery",
                               return_value=self.delivery()), \
                mock.patch.object(alarm_config, "save_alarm_delivery", saved.append):
            dlg = alarm_config.AlarmConfigWindow(AlarmConfig(), AlarmManager())
            dlg._save_delivery_global(dlg.get_config())
        self.assertEqual("lab-cmd", saved[0].ntfy_command_topic)
        self.assertEqual(TOKEN, saved[0].ntfy_token)
        self.assertTrue(saved[0].use_ntfy)


class TestNtfySettingsDialog(NtfyCase):
    def test_validation_and_save_only_touches_ntfy(self):
        from PySide6.QtWidgets import QApplication, QMessageBox
        from pythonization.ui.dialogs import ntfy_settings
        QApplication.instance() or QApplication([])
        existing = self.delivery(smtp_user="me@example.com", use_ntfy=False)
        saved = []
        with mock.patch.object(ntfy_settings, "load_alarm_delivery", return_value=existing), \
                mock.patch.object(ntfy_settings, "save_alarm_delivery", saved.append), \
                mock.patch.object(QMessageBox, "warning") as warn:
            dlg = ntfy_settings.NtfySettingsDialog()
            dlg._le_cmd.setText("lab-report")          # 보고 주제와 같으면 거부
            dlg._on_save()
            self.assertTrue(warn.called)
            self.assertEqual([], saved)
            dlg._le_cmd.setText("lab-cmd")
            dlg._cb_use.setChecked(True)
            dlg._on_save()
        self.assertTrue(saved[0].use_ntfy)
        self.assertEqual("me@example.com", saved[0].smtp_user, "다른 전송 설정은 그대로")


if __name__ == "__main__":
    unittest.main()
