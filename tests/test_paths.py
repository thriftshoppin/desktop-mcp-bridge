"""The glide must stay on monitors: the TV (-4096,-1450 4096x2160) and the main monitor (0,0 1920x1080) only touch along x=0, y 0..709."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from desktop_bridge import coords as c  # noqa: E402

PRIMARY = (0, 0, 1920, 1080)
TV = (-4096, -1450, 4096, 2160)
RECTS = [PRIMARY, TV]


def on_monitor(x, y):
    return any(r[0] <= x < r[0] + r[2] and r[1] <= y < r[1] + r[3] for r in RECTS)


class Paths(unittest.TestCase):
    def check_stays_on_monitors(self, a, b):
        path = c.plan_path(a, b, RECTS)
        self.assertEqual(path[0], a)
        self.assertEqual(path[-1], b)
        for k in range(0, 201):
            x, y = c.point_along(path, k / 200)
            self.assertTrue(on_monitor(x, y), (a, b, k, x, y, path))

    def test_tv_to_primary_that_used_to_hit_the_dead_corner(self):
        self.check_stays_on_monitors((-3000, -1000), (1338, 551))   # the failing selftest case
        self.check_stays_on_monitors((-3000, -900), (1338, 551))

    def test_primary_to_tv_and_extremes(self):
        self.check_stays_on_monitors((1338, 551), (-3000, -1000))
        self.check_stays_on_monitors((1919, 1079), (-4096, -1450))
        self.check_stays_on_monitors((0, 0), (-1, 709))
        self.check_stays_on_monitors((-1, 709), (1919, 0))

    def test_same_monitor_is_straight(self):
        self.assertEqual(c.plan_path((10, 10), (500, 500), RECTS), [(10, 10), (500, 500)])
        self.assertEqual(c.plan_path((-3000, -1000), (-100, 600), RECTS), [(-3000, -1000), (-100, 600)])

    def test_crossing_is_on_the_shared_edge(self):
        path = c.plan_path((-3000, -1000), (1338, 551), RECTS)
        self.assertEqual(len(path), 4)
        (xa, ya), (xb, yb) = path[1], path[2]
        self.assertEqual((xa, xb), (-1, 0))
        self.assertEqual(ya, yb)
        self.assertTrue(0 < ya < 709)

    def test_shared_edge(self):
        self.assertEqual(c.shared_edge(PRIMARY, TV), ("v", 0, -1, 0, 709))
        self.assertEqual(c.shared_edge(TV, PRIMARY), ("v", -1, 0, 0, 709))
        self.assertIsNone(c.shared_edge(PRIMARY, (3000, 0, 100, 100)))

    def test_no_route_falls_back_to_straight(self):
        far = [(0, 0, 100, 100), (500, 500, 100, 100)]
        self.assertEqual(c.plan_path((10, 10), (550, 550), far), [(10, 10), (550, 550)])

    def test_three_monitors_chain(self):
        rects = [(0, 0, 100, 100), (100, 0, 100, 100), (200, 0, 100, 100)]
        path = c.plan_path((10, 50), (290, 50), rects)
        self.assertEqual(len(path), 6)
        self.assertEqual(path[-1], (290, 50))


if __name__ == "__main__":
    unittest.main()
