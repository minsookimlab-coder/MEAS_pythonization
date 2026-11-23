"""AI 자동 측정 — 메인 창 sweep 경로를 실제로 돌리는 통합 테스트 (헤드리스).

실제 MainWindow · SweepWorker(QThread) · DataSaver 가 가짜 세션 위에서 돈다.
에이전트 스레드처럼 별도 스레드에서 bridge.run_main_sweep 을 부르고, 테스트는
Qt 이벤트를 돌리며 끝나길 기다린다. 사용자 설정 폴더에는 쓰지 않는다 (임시 폴더).

확인하는 것:
  - .dat 가 Main Folder / sub_folder 아래 file_prefix 로 저장되고 값이 맞다
  - 즉시 중단 조건(|누설전류| 상한)이 걸리면 그 스텝에서 바로 멈춘다
  - Time 채널 + 안정화 조건이면 안정되는 즉시 끝난다
  - 중지(stop_event)가 sweep 을 끊는다
  - 끝나면 메인 창 설정이 원래대로 돌아온다
  - AI 실행 중에는 사람이 메인 Start 를 눌러도 시작되지 않는다
  - AI 창에서 요청 → (가짜) LLM → 메인 창 sweep → 최종 보고까지 한 번에
"""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from pythonization.automation.executor import TIME_CHANNEL_ID, MainSweepSpec  # noqa: E402
from pythonization.measurement.resume_log import ResumeLog                    # noqa: E402
from pythonization.profiles.registry import ProfileRegistry                   # noqa: E402
from pythonization.ui.main_window import MainWindow                           # noqa: E402
from pythonization.ui.modules.auto_measure.bridge import MainWindowBridge     # noqa: E402
from tests.automation_fakes import FakeLab, make_profile                       # noqa: E402

_app = QApplication.instance() or QApplication([])


def pump_until(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


class BridgeCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        cls.mw = MainWindow(profile_registry=ProfileRegistry(settings_dir=cls.tmp / "settings"))
        cls.mw._resume_log = ResumeLog(cls.tmp / "resume_points.json")
        cls.mw._on_selection_applied(make_profile())

    @classmethod
    def tearDownClass(cls):
        cls.mw._worker_thread.quit()
        cls.mw._worker_thread.wait()
        cls.mw.deleteLater()
        cls._tmp.cleanup()

    def setUp(self):
        self.lab = FakeLab()
        self.mw._session = self.lab
        self.mw._worker.set_session(self.lab)
        self.data_root = self.tmp / f"data_{self._testMethodName}"
        self.data_root.mkdir()
        self.mw._le_main_folder.setText(str(self.data_root))
        self.mw._cb_save_date.setChecked(False)
        self.mw._cb_save_enable.setChecked(False)
        self.mw._le_custom_folder.setText("user_folder")
        self.mw._le_custom_word.setText("user_")
        self.bridge = MainWindowBridge(self.mw)
        self.stop = threading.Event()
        self.bridge.set_stop_event(self.stop)

    def tearDown(self):
        if self.mw._running:
            self.mw._on_stop()
        self.mw.sweep_step_recorded.disconnect(self.bridge._on_step_recorded)
        self.mw.sweep_stopped.disconnect(self.bridge._on_sweep_stopped)

    def run_in_thread(self, spec, timeout=20.0):
        box = {}
        t = threading.Thread(target=lambda: box.setdefault("out", self.bridge.run_main_sweep(spec)),
                             daemon=True)
        t.start()
        self.assertTrue(pump_until(lambda: "out" in box, timeout), "sweep 이 끝나지 않았다")
        return box["out"]

    def spec(self, **kw):
        base = dict(channel=0, sweep_to=1.0, rate=60.0, time_per_point=0.05,
                    measurements=[0, 1], sub_folder="ai/gate", file_prefix="Vg_",
                    channel_key=("SRC", "smua voltage"),
                    measurement_keys=[("LIA", "X"), ("SRC", "leakage")],
                    labels={0: "LIA X", 1: "SRC leakage"})
        base.update(kw)
        return MainSweepSpec(**base)


class TestMainSweepPath(BridgeCase):
    def test_sweep_writes_dat_with_ai_names(self):
        out = self.run_in_thread(self.spec())
        self.assertEqual("complete", out.reason, out.detail)
        path = Path(out.filepath)
        self.assertEqual(self.data_root / "ai" / "gate", path.parent)
        self.assertTrue(path.name.startswith("Vg_X"), path.name)
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(["Vg", "X", "Ileak"], lines[0].split("\t"))
        rows = [list(map(float, ln.split("\t"))) for ln in lines[2:]]
        self.assertEqual(0.0, rows[0][0], "첫 행 = 시작 위치 측정")
        self.assertAlmostEqual(1.0, rows[-1][0])
        self.assertEqual(len(rows), out.points)
        for x, meas, _leak in rows:
            self.assertAlmostEqual(1e-3 * x, meas, places=9)

    def test_main_ui_restored_after_run(self):
        self.run_in_thread(self.spec())
        self.assertEqual("ai/gate", self.mw._le_custom_folder.text())
        self.assertTrue(self.bridge.restore_snapshot())
        self.assertEqual("user_folder", self.mw._le_custom_folder.text())
        self.assertEqual("user_", self.mw._le_custom_word.text())
        self.assertFalse(self.mw._cb_save_enable.isChecked())

    def test_abort_limit_stops_immediately(self):
        self.lab.leak_per_volt = 1e-9          # 0.5 V 에서 5e-10, 1 V 에서 1e-9 …
        out = self.run_in_thread(self.spec(sweep_to=5.0, abort_limits={1: 2e-9}))
        self.assertEqual("abort", out.reason)
        self.assertIn("SRC leakage", out.detail)
        self.assertLessEqual(self.lab.source, 2.2, "한계를 넘은 스텝에서 바로 멈춘다")
        self.assertFalse(self.mw._running)

    def test_time_trace_stops_when_stable(self):
        out = self.run_in_thread(self.spec(
            channel=TIME_CHANNEL_ID, sweep_to=60.0, rate=60.0, measurements=[0],
            measurement_keys=[("LIA", "X")], channel_key=None,
            stable=(0, 4, 1e-6)))
        self.assertEqual("stable", out.reason)
        self.assertLess(out.points, 10, "60초를 다 채우지 않고 안정화 즉시 끝난다")
        header = Path(out.filepath).read_text(encoding="utf-8").splitlines()[0]
        self.assertTrue(header.startswith("time"))

    def test_stop_event_interrupts_sweep(self):
        threading.Timer(0.4, self.stop.set).start()
        out = self.run_in_thread(self.spec(sweep_to=10.0, rate=6.0, time_per_point=0.05))
        self.assertEqual("agent_stop", out.reason)
        self.assertLess(self.lab.source, 1.0)

    def test_profile_change_detected(self):
        out = self.run_in_thread(self.spec(channel_key=("SRC", "other channel")))
        self.assertEqual("start_failed", out.reason)
        self.assertEqual([], self.lab.writes)

    def test_missing_main_folder_refused(self):
        self.mw._le_main_folder.setText("")
        out = self.run_in_thread(self.spec())
        self.assertEqual("start_failed", out.reason)
        self.assertIn("Main Folder", out.detail)


class TestAutoMeasureWindow(BridgeCase):
    """창 → 가짜 LLM → 메인 창 sweep → 최종 보고."""

    def test_full_run_with_scripted_llm(self):
        from pythonization.ui.modules.auto_measure import window as win_mod

        responses = [
            NS(stop_reason="tool_use", usage=None, content=[
                NS(type="text", text="게이트(smua)를 0→1 V 로 스윕하며 락인 X 를 기록합니다."),
                NS(type="tool_use", id="t1", name="run_sweep", input={
                    "channel": 0, "start": None, "stop": 1.0, "rate": 60,
                    "time_per_point": 0.05, "measurements": [0, 1],
                    "sub_folder": "sampleA/gate", "file_prefix": "Vg_0_1_",
                    "purpose": "요청한 게이트 sweep"})]),
            NS(stop_reason="end_turn", usage=None,
               content=[NS(type="text", text="측정 완료. 파일 1개 저장.")]),
        ]
        calls = []

        def create(**kwargs):
            calls.append(kwargs)
            return responses.pop(0)

        fake_client = NS(beta=NS(messages=NS(create=create)))
        self.mw._auto_measure_window = None
        with mock.patch.object(win_mod, "make_client", return_value=fake_client), \
                mock.patch.object(win_mod, "sdk_available", return_value=True), \
                mock.patch.object(win_mod, "has_env_credentials", return_value=True), \
                mock.patch.object(QMessageBox, "question",
                                  return_value=QMessageBox.StandardButton.Yes):
            win = win_mod.AutoMeasureWindow(self.mw, self.mw._param_manager_reg, None)
            self.mw._auto_measure_window = win
            sent = []
            win._send_messenger = lambda text, urgent: sent.append(text) or []
            # 게이트 허용 + 범위
            t = win._tbl_sweep
            t.item(0, 0).setCheckState(win_mod.Qt.CheckState.Checked)
            t.item(0, 4).setText("-20")
            t.item(0, 5).setText("20")
            t.item(0, 6).setText("120")
            t.item(0, 7).setText("gate")
            win._sb_min_tpp.setValue(0.05)
            win._cb_approval.setCurrentIndex(win._cb_approval.findData("auto"))
            win._te_request.setPlainText("게이트를 0 에서 1 V 까지 스윕해")
            win._on_start_clicked()
            self.assertFalse(win.is_idle(), "실행이 시작돼야 한다")

            # AI 실행 중에는 사람이 Start 를 눌러도 시작되지 않는다
            with mock.patch.object(QMessageBox, "warning") as warn:
                self.mw._on_start()
                self.assertTrue(warn.called)

            self.assertTrue(pump_until(win.is_idle, 30), "AI 실행이 끝나지 않았다")
            _app.processEvents()

        # 첫 요청에 요청문 + 실험실 현황(역할 메모 포함)이 들어갔다
        first = calls[0]["messages"][0]["content"][0]["text"]
        self.assertIn("게이트를 0 에서 1 V", first)
        self.assertIn('"role": "gate"', first)
        # 메인 창 경로로 저장된 .dat
        files = list((self.data_root / "sampleA" / "gate").glob("Vg_0_1_X*.dat"))
        self.assertEqual(1, len(files))
        # 두 번째 호출에 데이터 요약이 tool_result 로 전달됐다
        # (messages 는 덧붙이기만 하는 같은 리스트 — [user, assistant, user(결과), ...])
        result = calls[1]["messages"][2]["content"][0]
        self.assertEqual("t1", result["tool_use_id"])
        self.assertIn('"status": "complete"', result["content"])
        self.assertIn('"points"', result["content"])
        # 최종 보고가 메신저로, 실행 기록이 남는다
        self.assertTrue(any("측정 완료" in s for s in sent))
        run_dirs = list((self.data_root / "ai_runs").iterdir())
        self.assertEqual(1, len(run_dirs))
        self.assertTrue((run_dirs[0] / "journal.jsonl").exists())
        self.assertIn("측정 완료", (run_dirs[0] / "report.md").read_text(encoding="utf-8"))
        # 끝나면 메인 창 설정 복원 + Start 다시 사용 가능
        self.assertEqual("user_folder", self.mw._le_custom_folder.text())
        self.assertTrue(self.mw._btn_start.isEnabled())
        # 설정은 임시 폴더에만
        self.assertTrue((self.tmp / "settings" / "profiles" / "automation").exists())
        win._bridge.forget_snapshot()
        self.mw.sweep_step_recorded.disconnect(win._bridge._on_step_recorded)
        self.mw.sweep_stopped.disconnect(win._bridge._on_sweep_stopped)
        self.mw._auto_measure_window = None


class TestRemoteControl(BridgeCase):
    """휴대폰(ntfy 지시 주제)에서 온 글이 알맞은 곳으로 가는지 — 네트워크 없이."""

    def setUp(self):
        super().setUp()
        from pythonization.ui.modules.auto_measure import window as win_mod
        self.win_mod = win_mod
        self.win = win_mod.AutoMeasureWindow(self.mw, self.mw._param_manager_reg, None)
        self.mw._auto_measure_window = self.win
        self.replies = []
        self.win._reply_remote = self.replies.append

    def tearDown(self):
        self.win.stop_mcp_server()
        self.mw.sweep_step_recorded.disconnect(self.win._bridge._on_step_recorded)
        self.mw.sweep_stopped.disconnect(self.win._bridge._on_sweep_stopped)
        self.mw._auto_measure_window = None
        super().tearDown()

    def pending(self):
        req = self.win_mod._ApprovalRequest("run_sweep", {}, "Sweep 측정: 게이트 -20→20 V")
        self.win._on_approval_requested(req)
        return req

    def test_status(self):
        self.win._on_remote_message("상태")
        self.assertIn("AI 대기 중", self.replies[-1])

    def test_approve_and_reject_by_number(self):
        req = self.pending()
        self.win._on_remote_message("승인 0000")
        self.assertFalse(req.done.is_set(), "번호가 다르면 아무것도 하지 않는다")
        self.assertIn("없습니다", self.replies[-1])
        self.win._on_remote_message(f"/approve {req.short_id}")
        self.assertTrue(req.decision.approved)
        self.assertFalse(self.win._approval_card.isVisible())
        req2 = self.pending()
        self.win._on_remote_message(f"거부 {req2.short_id} 속도가 빠름")
        self.assertEqual((False, "속도가 빠름"), (req2.decision.approved, req2.decision.comment))

    def test_approval_notification_has_phone_buttons(self):
        from pythonization.config.models import AlarmDeliveryConfig
        d = AlarmDeliveryConfig(use_ntfy=True, ntfy_server="http://127.0.0.1:2586",
                                ntfy_public_url="http://192.168.0.20:2586",
                                ntfy_topic="lab-report", ntfy_command_topic="lab-cmd",
                                ntfy_token="tk_x", ntfy_commands_enabled=True)
        sent = []
        req = self.win_mod._ApprovalRequest("run_sweep", {}, "Sweep 측정")
        with mock.patch.object(self.win_mod, "load_alarm_delivery", return_value=d), \
                mock.patch.object(self.win._alarm, "send_ntfy",
                                  side_effect=lambda *a, **k: sent.append((a, k)) or None):
            self.assertTrue(self.win._notify_approval_by_ntfy(req))
        actions = sent[0][1]["actions"]
        self.assertEqual("http://192.168.0.20:2586/lab-cmd", actions[0]["url"],
                         "버튼은 휴대폰이 닿는 주소로 보낸다")
        self.assertEqual(f"/approve {req.short_id}", actions[0]["body"])
        self.assertEqual(f"/reject {req.short_id}", actions[1]["body"])

    def test_instruction_goes_to_claude_code_when_it_is_working(self):
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.win._sb_port.setValue(port)
        self.assertTrue(self.win.start_mcp_server())
        self.win._mcp.last_activity = time.time()        # 방금 Claude Code 가 도구를 불렀다
        self.win._on_remote_message("게이트를 0 V 로 되돌려")
        self.assertIn("Claude Code 에 전달", self.replies[-1])
        reply = self.win._mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": "check_user_messages",
                                                 "arguments": {}}})
        texts = [c["text"] for c in reply["result"]["content"]]
        self.assertTrue(any("[사용자 원격 지시 (ntfy)]" in t and "게이트를 0 V 로" in t
                            for t in texts))
        self.assertEqual([], self.win.take_remote_messages(), "한 번 전달하면 비워진다")

    def test_instruction_starts_in_app_run_when_idle(self):
        responses = [NS(stop_reason="end_turn", usage=None,
                        content=[NS(type="text", text="현재값을 확인했습니다.")])]
        fake_client = NS(beta=NS(messages=NS(create=lambda **kw: responses.pop(0))))
        with mock.patch.object(self.win_mod, "make_client", return_value=fake_client), \
                mock.patch.object(self.win_mod, "sdk_available", return_value=True):
            self.win._on_remote_message("락인 값 한 번 읽어줘")
            self.assertIn("시작", self.replies[-1])
            self.assertTrue(pump_until(self.win.is_idle, 20))
            _app.processEvents()
        self.assertEqual("현재값을 확인했습니다.", self.win._agent.final_text)

    def test_stop_command(self):
        self.win._on_remote_message("중지")
        self.assertIn("멈출 작업이 없습니다", self.replies[-1])


if __name__ == "__main__":
    unittest.main()
