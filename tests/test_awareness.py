"""Offline tests for the 'awareness' helpers: which user keys count as interference (media/volume keys must not)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from desktop_bridge import winapi  # noqa: E402


class UserKeys(unittest.TestCase):
    def test_media_volume_browser_keys_and_packet_are_ignored_everything_else_counts(self):
        with mock.patch.object(winapi.user32, "GetAsyncKeyState", create=True, return_value=-32768):
            keys = winapi.user_keys_down(own=(0x0D, 0x09))
        self.assertNotIn(0xB3, keys)  # play/pause
        self.assertNotIn(0xAF, keys)  # volume up
        self.assertNotIn(0xA6, keys)  # browser back
        self.assertNotIn(0xE7, keys)  # VK_PACKET (our own unicode typing)
        self.assertNotIn(0x0D, keys)  # own: Enter
        self.assertIn(0x41, keys)     # 'A'
        self.assertIn(0x01, keys)     # left mouse button
        self.assertIn(0x11, keys)     # Ctrl

    def test_nothing_down(self):
        with mock.patch.object(winapi.user32, "GetAsyncKeyState", create=True, return_value=0):
            self.assertEqual(winapi.user_keys_down(), [])


if __name__ == "__main__":
    unittest.main()
