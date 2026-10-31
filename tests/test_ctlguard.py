"""Offline tests for the control-pipe hardening (audit DB-1: the launcher authenticates the guardian; DB-2: control connections time out and are capped).
No guardian is started and no window opens: the 'squatter' is a plain named pipe created inside this test process."""
import ctypes
import os
import sys
import threading
import time
import unittest
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import launcher  # noqa: E402
from desktop_bridge import ctlguard  # noqa: E402


class CheckGuardian(unittest.TestCase):
    ok = {os.path.normcase(r"c:\py\python.exe")}

    def test_accepts_the_expected_image_at_high_integrity(self):
        self.assertIsNone(launcher.check_guardian(r"C:\PY\python.exe", 0x3000, self.ok))
        self.assertIsNone(launcher.check_guardian(r"C:\py\python.exe", 0x4000, self.ok))

    def test_refuses_other_programs(self):
        self.assertIn("not this bridge", launcher.check_guardian(r"C:\evil\python.exe", 0x3000, self.ok))
        self.assertIn("could not be identified", launcher.check_guardian("", 0x3000, self.ok))

    def test_refuses_medium_integrity_even_with_the_right_image(self):
        self.assertIn("not elevated", launcher.check_guardian(r"C:\py\python.exe", 0x2000, self.ok))

    def test_refuses_when_integrity_is_unknown(self):
        self.assertIn("could not be read", launcher.check_guardian(r"C:\py\python.exe", None, self.ok))

    def test_expected_images_cover_the_venv_shim_and_the_base_interpreter(self):
        e = launcher.expected_images()
        self.assertIn(os.path.normcase(os.path.abspath(sys.executable)), e)


@unittest.skipUnless(sys.platform == "win32", "Windows only")
class SquatterIsRefused(unittest.TestCase):
    """A same-user program that creates the predictable control pipe first must not receive anything from the launcher."""

    def setUp(self):
        import win32pipe
        self.inst = "t-sq-" + uuid.uuid4().hex[:8]
        self.old_inst = launcher.INSTANCE
        launcher.INSTANCE = self.inst
        name = launcher.control_path()
        self.h = win32pipe.CreateNamedPipe(name, win32pipe.PIPE_ACCESS_DUPLEX, win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_WAIT, 1, 4096, 4096, 0, None)
        self.got = []
        self.t = threading.Thread(target=self._serve, daemon=True)
        self.t.start()

    def _serve(self):
        import win32file
        import win32pipe
        try:
            win32pipe.ConnectNamedPipe(self.h, None)
            self.got.append(win32file.ReadFile(self.h, 4096)[1])   # blocks until the client sends something or closes
        except Exception as e:  # noqa
            self.got.append(b"")

    def tearDown(self):
        import win32api
        launcher.INSTANCE = self.old_inst
        try:
            win32api.CloseHandle(self.h)
        except Exception:
            pass

    def test_launcher_refuses_a_pipe_owned_by_a_medium_process(self):
        if launcher.is_admin():
            self.skipTest("this test process is elevated, so the squatter would be High integrity too; the image check covers that case below")
        with self.assertRaises(launcher.UntrustedGuardian) as cm:
            launcher.try_open_control()
        self.assertIn("not elevated", str(cm.exception))
        self.t.join(3)
        self.assertEqual(self.got, [b""], "the squatter must not receive a single byte")

    def test_launcher_refuses_a_pipe_owned_by_another_program(self):
        old = launcher.expected_images
        launcher.expected_images = lambda: {os.path.normcase(r"c:\somewhere\else\python.exe")}
        try:
            with self.assertRaises(launcher.UntrustedGuardian) as cm:
                launcher.try_open_control()
            self.assertIn("not this bridge", str(cm.exception))
        finally:
            launcher.expected_images = old
        self.t.join(3)
        self.assertEqual(self.got, [b""])

    def test_the_pipe_server_pid_is_this_process(self):
        f = open(launcher.control_path(), "r+b", buffering=0)
        try:
            self.assertEqual(launcher.server_pid_of(f), os.getpid())
            self.assertTrue(launcher.process_image(os.getpid()).lower().endswith("python.exe"))
            self.assertIn(launcher.process_integrity(os.getpid()), (0x2000, 0x3000, 0x4000))
        finally:
            f.close()


@unittest.skipUnless(sys.platform == "win32", "Windows only")
class ReadTimeout(unittest.TestCase):
    def test_a_silent_client_is_dropped_after_the_timeout(self):
        import win32file
        import win32pipe
        name = r"\\.\pipe\t-rt-" + uuid.uuid4().hex[:8]
        h = win32pipe.CreateNamedPipe(name, win32pipe.PIPE_ACCESS_DUPLEX, win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_WAIT, 1, 4096, 4096, 0, None)
        client = win32file.CreateFile(name, 0xC0000000, 0, None, 3, 0, None)   # connects, then says nothing
        win32pipe.ConnectNamedPipe(h, None)
        t0 = time.monotonic()
        with self.assertRaises(TimeoutError):
            ctlguard.read_with_timeout(h, 4096, 0.6)
        dt = time.monotonic() - t0
        self.assertTrue(0.5 < dt < 3, dt)
        win32file.CloseHandle(client)
        win32file.CloseHandle(h)

    def test_a_prompt_client_is_read_normally(self):
        import win32file
        import win32pipe
        name = r"\\.\pipe\t-rt2-" + uuid.uuid4().hex[:8]
        h = win32pipe.CreateNamedPipe(name, win32pipe.PIPE_ACCESS_DUPLEX, win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_WAIT, 1, 4096, 4096, 0, None)
        client = win32file.CreateFile(name, 0xC0000000, 0, None, 3, 0, None)
        win32pipe.ConnectNamedPipe(h, None)
        win32file.WriteFile(client, b'{"op":"connect"}\n')
        self.assertEqual(ctlguard.read_with_timeout(h, 4096, 2), b'{"op":"connect"}\n')
        win32file.CloseHandle(client)
        win32file.CloseHandle(h)


class Limiter(unittest.TestCase):
    def test_per_pid_cap(self):
        lim = ctlguard.ConnLimiter(per_pid=2, total_unread=10)
        a, b = lim.admit(100), lim.admit(100)
        self.assertTrue(a and b)
        self.assertIsNone(lim.admit(100))      # third from the same process
        self.assertIsNotNone(lim.admit(200))   # another process is not affected
        lim.release(a)
        self.assertIsNotNone(lim.admit(100))   # a freed slot is reusable

    def test_total_cap_counts_only_connections_that_have_not_sent_their_request(self):
        lim = ctlguard.ConnLimiter(per_pid=100, total_unread=3)
        ids = [lim.admit(p) for p in (1, 2, 3)]
        self.assertIsNone(lim.admit(4))        # a flood of silent connections is turned away ...
        lim.request_read(ids[0])
        self.assertIsNotNone(lim.admit(4))     # ... but one that already asked (and may be waiting for the user's answer) frees its place
        self.assertEqual(lim.counts(), (4, 3))

    def test_a_flood_cannot_take_every_slot(self):
        lim = ctlguard.ConnLimiter(per_pid=4, total_unread=32)
        got = [lim.admit(999) for _ in range(500)]
        self.assertEqual(sum(1 for g in got if g), 4)   # one flooding process holds 4 of the 255 pipe instances, not all of them


if __name__ == "__main__":
    unittest.main()
