"""Offline tests for the shared guardian pieces: two-stage Esc, call cancellation, client identity/allow-list, approval migration, pipe names."""
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from desktop_bridge import clients, esc, policy, safety  # noqa: E402


class EscDecisions(unittest.TestCase):
    def test_first_esc_with_calls_running_cancels(self):
        self.assertEqual(esc.decide(100.0, 2, None, 10), "cancel")

    def test_second_esc_within_window_stops_the_bridge(self):
        self.assertEqual(esc.decide(105.0, 0, 100.0, 10), "stop")
        self.assertEqual(esc.decide(105.0, 3, 100.0, 10), "stop")  # even if calls are still running: the user pressed twice

    def test_window_expires_back_to_stage_one(self):
        self.assertEqual(esc.decide(111.0, 1, 100.0, 10), "cancel")

    def test_esc_with_nothing_to_cancel_stops_immediately(self):
        self.assertEqual(esc.decide(100.0, 0, None, 10), "stop")

    def test_stage_numbers(self):
        self.assertEqual(esc.stage(100, None, 10, False), 0)
        self.assertEqual(esc.stage(105, 100, 10, False), 1)
        self.assertEqual(esc.stage(111, 100, 10, False), 0)
        self.assertEqual(esc.stage(105, 100, 10, True), 2)


class CancelMechanism(unittest.TestCase):
    def mk(self, inflight=1):
        return safety.KillSwitch([], esc_enabled=False, inflight=lambda: inflight, second_press_s=10)

    def test_cancel_stops_calls_admitted_before_it_but_not_later_ones(self):
        ks = self.mk()
        ks.begin_call(ks.epoch)
        ks.check()  # fine
        ks.cancel("test")
        with self.assertRaises(safety.CancelledByUser) as cm:
            ks.check()
        self.assertEqual(cm.exception.code, "CANCELLED_BY_USER")
        self.assertFalse(ks.active)  # stage 1 does not stop the bridge
        ks.begin_call(ks.epoch)  # a call admitted after the cancel is fine
        ks.check()
        ks.close()

    def test_a_queued_call_admitted_before_the_cancel_is_cancelled_when_it_finally_starts(self):
        ks = self.mk()
        admitted = ks.epoch
        ks.cancel("test")
        ks.begin_call(admitted)
        with self.assertRaises(safety.CancelledByUser):
            ks.check()
        ks.close()

    def test_epoch_is_per_thread(self):
        ks = self.mk()
        out = {}
        ks.begin_call(ks.epoch)
        ks.cancel("t")

        def other():
            ks.begin_call(ks.epoch)  # new epoch in another thread
            try:
                ks.check()
                out["other"] = "ok"
            except Exception as e:  # noqa
                out["other"] = type(e).__name__

        th = threading.Thread(target=other)
        th.start()
        th.join()
        self.assertEqual(out["other"], "ok")
        with self.assertRaises(safety.CancelledByUser):
            ks.check()  # this thread still holds the old epoch
        ks.close()

    def test_cancel_runs_the_registered_cancel_hooks(self):
        ks = self.mk()
        hit = []
        ks.add_cancel(lambda: hit.append(1))
        ks.cancel("t")
        self.assertEqual(hit, [1])
        ks.close()

    def test_two_presses_cancel_then_stop(self):
        ks = self.mk(inflight=1)
        self.assertEqual(ks.esc_pressed(), "cancel")
        self.assertFalse(ks.active)
        self.assertEqual(ks.esc_pressed(), "stop")
        self.assertTrue(ks.active)
        self.assertEqual(ks.source, "esc")
        self.assertTrue(ks.resume())
        ks.close()

    def test_press_with_nothing_running_stops_at_once(self):
        ks = self.mk(inflight=0)
        self.assertEqual(ks.esc_pressed(), "stop")
        self.assertTrue(ks.active)
        ks.close()

    def test_status_reports_the_stage(self):
        ks = self.mk(inflight=1)
        self.assertEqual(ks.status()["esc"]["stage"], 0)
        ks.cancel("t")
        self.assertEqual(ks.status()["esc"]["stage"], 1)
        ks.close()


class ClientIdentity(unittest.TestCase):
    def test_version_changes_do_not_change_the_identity(self):
        a = r"C:\Program Files\WindowsApps\Claude_4.4212.0.0_x64__pzs8sxrjxfjjc\app\Claude.exe"
        b = r"C:\Program Files\WindowsApps\Claude_4.5000.1.0_x64__pzs8sxrjxfjjc\app\Claude.exe"
        self.assertEqual(clients.identity_key(a), clients.identity_key(b))
        c = r"c:\users\x\.vscode\extensions\anthropic.claude-code-2.1.312-win32-x64\resources\native-binary\claude.exe"
        d = r"c:\users\x\.vscode\extensions\anthropic.claude-code-2.2.0-win32-x64\resources\native-binary\claude.exe"
        self.assertEqual(clients.identity_key(c), clients.identity_key(d))
        self.assertNotEqual(clients.identity_key(a), clients.identity_key(c))

    def test_labels(self):
        self.assertEqual(clients.label_for(r"C:\Program Files\WindowsApps\Claude_4.1.0.0_x64__x\app\Claude.exe"), "Claude desktop")
        self.assertEqual(clients.label_for(r"C:\u\.vscode\extensions\anthropic.claude-code-2.1.3-win32-x64\resources\native-binary\claude.exe"), "Claude Code (VS Code)")
        self.assertEqual(clients.label_for(r"C:\Python\python.exe", r"C:\x\selftest.py"), "python.exe selftest.py")

    def test_scripts_are_part_of_the_identity_for_interpreters_only(self):
        py = r"C:\Python\python.exe"
        self.assertNotEqual(clients.identity_key(py, "a.py"), clients.identity_key(py, "b.py"))
        exe = r"C:\Apps\thing.exe"
        self.assertEqual(clients.identity_key(exe, "a.py"), clients.identity_key(exe, "b.py"))

    def test_allow_list_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            al = clients.AllowList(d)
            k = clients.identity_key(r"C:\Apps\x.exe")
            self.assertFalse(al.is_allowed(k))
            al.add(k, "X", r"C:\Apps\x.exe")
            self.assertTrue(al.is_allowed(k))
            self.assertTrue(clients.AllowList(d).is_allowed(k))  # persisted
            self.assertTrue(al.remove(k))
            self.assertFalse(al.is_allowed(k))
            self.assertFalse(al.remove(k))


class LauncherDetection(unittest.TestCase):
    def test_only_the_script_being_run_counts(self):
        self.assertTrue(clients.is_launcher_cmd(["python.exe", "C:/x/launcher.py"]))
        self.assertTrue(clients.is_launcher_cmd([r"C:\v\python.exe", r"C:\x\LAUNCHER.PY"]))
        # a different script that merely receives launcher.py as an argument is a CLIENT, not the launcher chain
        self.assertFalse(clients.is_launcher_cmd(["python.exe", "probe_client.py", "C:/x/launcher.py", "{}"]))
        self.assertFalse(clients.is_launcher_cmd(["claude.exe", "--mcp", "launcher.py"]))
        self.assertFalse(clients.is_launcher_cmd(["python.exe"]))


class ApprovalMigration(unittest.TestCase):
    def test_imports_the_legacy_record_only_for_the_same_config(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as local, tempfile.TemporaryDirectory() as store:
            json.dump({"a": 1}, open(os.path.join(root, "config.json"), "w"))
            legacy = os.path.join(local, "Packages", "Claude_x", "LocalCache", "Local", "desktop-bridge")
            os.makedirs(legacy)
            digest, merged = policy.config_digest(root)
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": local}):
                ap = policy.Approval(root, store)
                self.assertIsNone(ap.import_legacy())  # nothing recorded yet
                json.dump({"sha256": "0" * 64, "config": {}}, open(os.path.join(legacy, "config.approved.json"), "w"))
                self.assertIsNone(ap.import_legacy())  # approval of a DIFFERENT config is never adopted
                self.assertFalse(ap.state()[0])
                json.dump({"sha256": digest, "config": merged}, open(os.path.join(legacy, "config.approved.json"), "w"))
                self.assertEqual(ap.import_legacy(), legacy)
                self.assertTrue(ap.state()[0])

    def test_default_store_is_programdata_not_localappdata(self):
        with mock.patch.dict(os.environ, {"ProgramData": r"C:\PD", "LOCALAPPDATA": r"C:\LA"}):
            self.assertTrue(policy.Approval(".").dir.startswith(r"C:\PD"))


class PipeNames(unittest.TestCase):
    def test_control_pipe_name_has_instance_and_user(self):
        from desktop_bridge import worker
        with mock.patch.dict(os.environ, {"USERNAME": "Someone"}):
            self.assertEqual(worker.control_pipe_name("main"), "desktop-bridge-ctl-main-someone")
            self.assertNotEqual(worker.control_pipe_name("main"), worker.control_pipe_name("t1"))


if __name__ == "__main__":
    unittest.main()
