"""More pure/offline unit tests: denylist matching, key parsing, path protection, log text cleaning, locked-session capture refusal."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from desktop_bridge import safety, winapi  # noqa: E402

DENY = ["KeePass*", "Bitwarden*", "1Password*", "Dashlane*", "LastPass*"]


class Denylist(unittest.TestCase):
    def test_matches(self):
        for n in ("KeePass.exe", "KeePassXC.exe", "keepass.exe", "Bitwarden.exe", "1Password.exe", "Dashlane.exe", "LastPass.exe"):
            self.assertTrue(winapi.is_denylisted(n, DENY), n)

    def test_non_matches(self):
        for n in ("notepad.exe", "firefox.exe", "Code.exe", "", "keep.exe"):
            self.assertFalse(winapi.is_denylisted(n, DENY), n)


class Keys(unittest.TestCase):
    def test_combos(self):
        self.assertEqual(winapi.parse_combo("ctrl+shift+esc"), [0x11, 0x10, 0x1B])
        self.assertEqual(winapi.parse_combo("Win+R"), [0x5B, 0x52])
        self.assertEqual(winapi.parse_combo("f5"), [0x74])
        with self.assertRaises(ValueError):
            winapi.parse_combo("ctrl+banana")


class Paths(unittest.TestCase):
    def test_protected(self):
        prot = [r"C:\Users\SysAdmin\Claude\claude-actions-log.md", r"C:\Users\SysAdmin\Claude\log-tools"]
        self.assertTrue(safety.is_protected_path(r"C:\Users\SysAdmin\Claude\claude-actions-log.md", prot))
        self.assertTrue(safety.is_protected_path(r"c:\users\sysadmin\claude\LOG-TOOLS\Add-ClaudeLog.ps1", prot))
        self.assertFalse(safety.is_protected_path(r"C:\Users\SysAdmin\Claude\shared\x.md", prot))
        self.assertFalse(safety.is_protected_path(r"C:\Users\SysAdmin\Claude\log-tools-old\x", prot))


class LogText(unittest.TestCase):
    def test_clean(self):
        s = safety.clean_log_text("a\r\nb\tc \u00e9", 100)
        self.assertNotIn("\n", s)
        self.assertTrue(s.isascii())
        self.assertEqual(len(safety.clean_log_text("x" * 5000, 100)), 100)


class Locked(unittest.TestCase):
    def test_locked_session_refuses_capture(self):
        from desktop_bridge import capture
        from desktop_bridge.coords import Monitor
        cap = capture.Capturer()
        mons = [Monitor(0, "m", 0, 0, 100, 100, 96, True)]
        with self.assertRaises(safety.ToolRefused) as cm:
            cap.grab((0, 0, 100, 100), mons, {"locked": True, "input_desktop": "Winlogon", "type": "console"})
        self.assertEqual(cm.exception.code, "SESSION_LOCKED")
        with self.assertRaises(safety.ToolRefused) as cm:
            cap.grab((0, 0, 100, 100), mons, {"locked": False, "type": "rdp", "wts_state": "disconnected"})
        self.assertEqual(cm.exception.code, "SESSION_DISCONNECTED")


if __name__ == "__main__":
    unittest.main()
