"""No headless mode: the guardian needs a visible feed window."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from desktop_bridge import worker  # noqa: E402


class VisibleOnly(unittest.TestCase):
    def test_refuses_without_a_console_window(self):
        self.assertFalse(worker.require_visible_console(lambda: 0, lambda h: True))

    def test_refuses_when_the_window_is_hidden(self):
        self.assertFalse(worker.require_visible_console(lambda: 123, lambda h: False))

    def test_runs_when_the_window_is_visible(self):
        self.assertTrue(worker.require_visible_console(lambda: 123, lambda h: True))


if __name__ == "__main__":
    unittest.main()
