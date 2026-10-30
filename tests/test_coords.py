"""Unit tests for desktop_bridge.coords (pure, no Windows calls). Run: .venv\\Scripts\\python.exe -m unittest discover tests"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from desktop_bridge import coords as c  # noqa: E402

PRIMARY = c.Monitor(0, r"\\.\DISPLAY20", 0, 0, 1920, 1080, 96, True, 240)
TV = c.Monitor(1, r"\\.\DISPLAY21", -4096, -1450, 4096, 2160, 192, False, 0)
MONS = [PRIMARY, TV]


class Plan(unittest.TestCase):
    def test_primary_long_edge_capped(self):
        ctx = c.plan_shot(PRIMARY.rect, 0)
        self.assertLessEqual(max(ctx.iw, ctx.ih), 1568)
        self.assertAlmostEqual(ctx.scale, 1568 / 1920)

    def test_tv_long_edge_capped(self):
        ctx = c.plan_shot(TV.rect, 1)
        self.assertEqual(ctx.iw, 1568)
        self.assertEqual(ctx.ih, round(2160 * 1568 / 4096))
        self.assertEqual(ctx.origin, (-4096, -1450))

    def test_small_region_not_upscaled(self):
        ctx = c.plan_shot((100, 100, 400, 300), 0)
        self.assertEqual(ctx.scale, 1.0)
        self.assertEqual((ctx.iw, ctx.ih), (400, 300))

    def test_zoom_upscale(self):
        ctx = c.plan_shot((100, 100, 400, 300), 0, upscale=2.0)
        self.assertEqual((ctx.iw, ctx.ih), (800, 600))

    def test_empty_region(self):
        with self.assertRaises(ValueError):
            c.plan_shot((0, 0, 0, 10), 0)


class Mapping(unittest.TestCase):
    def check_round_trip(self, mon):
        ctx = c.plan_shot(mon.rect, mon.id)
        pts = [(0, 0), (ctx.iw - 1, ctx.ih - 1), (ctx.iw // 2, ctx.ih // 2), (17, 301), (ctx.iw - 3, 5), (700, ctx.ih - 2)]
        for x, y in pts:
            px, py = c.shot_to_screen(ctx, x, y)
            self.assertTrue(mon.contains(px, py), (mon.id, x, y, px, py))
            bx, by = c.screen_to_shot(ctx, px, py)
            self.assertLessEqual(abs(bx - x), 1, (mon.id, x, y, px, py, bx, by))
            self.assertLessEqual(abs(by - y), 1, (mon.id, x, y, px, py, bx, by))

    def test_round_trip_primary(self):
        self.check_round_trip(PRIMARY)

    def test_round_trip_tv(self):
        self.check_round_trip(TV)

    def test_real_to_shot_to_real_within_scale(self):
        # real -> shot -> real may lose up to 1/scale real pixels (downscaled image), never more.
        for mon in MONS:
            ctx = c.plan_shot(mon.rect, mon.id)
            for dx, dy in [(0, 0), (123, 456), (mon.w - 1, mon.h - 1), (mon.w // 2, mon.h // 2)]:
                px, py = mon.x + dx, mon.y + dy
                sx, sy = c.screen_to_shot(ctx, px, py)
                qx, qy = c.shot_to_screen(ctx, sx, sy)
                tol = int(1 / ctx.scale) + 1
                self.assertLessEqual(abs(qx - px), tol)
                self.assertLessEqual(abs(qy - py), tol)

    def test_tv_negative_origin_known_values(self):
        ctx = c.plan_shot(TV.rect, 1)
        x, y = c.shot_to_screen(ctx, 0, 0)
        self.assertTrue(-4096 <= x <= -4090 and -1450 <= y <= -1444)
        x, y = c.shot_to_screen(ctx, ctx.iw - 1, ctx.ih - 1)
        self.assertTrue(-5 <= x <= -1 and 705 <= y <= 709, (x, y))

    def test_centre_of_tv_image(self):
        ctx = c.plan_shot(TV.rect, 1)
        x, y = c.shot_to_screen(ctx, 784, ctx.ih // 2)
        self.assertLessEqual(abs(x - (-4096 + 2048)), 3)
        self.assertLessEqual(abs(y - (-1450 + 1080)), 3)

    def test_clamp(self):
        ctx = c.plan_shot(PRIMARY.rect, 0)
        x, y = c.shot_to_screen(ctx, -50, 99999)
        self.assertTrue(PRIMARY.contains(x, y))
        x2, y2 = c.shot_to_screen(ctx, 5000, -5)
        self.assertTrue(PRIMARY.contains(x2, y2))

    def test_region_shot_to_screen(self):
        ctx = c.plan_shot(TV.rect, 1)
        r = c.region_shot_to_screen(ctx, (0, 0, ctx.iw, ctx.ih))
        self.assertEqual(r, (-4096, -1450, 4096, 2160))
        r = c.region_shot_to_screen(ctx, (100, 100, 5000, 5000))
        self.assertEqual(r[0] + r[2], -4096 + 4096)  # clamped to region edge

    def test_monitor_at_and_edges(self):
        self.assertEqual(c.monitor_at(MONS, 0, 0).id, 0)
        self.assertEqual(c.monitor_at(MONS, -4096, -1450).id, 1)
        self.assertIsNone(c.monitor_at(MONS, 1920, 0))  # right of primary: nothing
        self.assertIsNone(c.monitor_at(MONS, 0, 1080))

    def test_secondary_region_inside_tv(self):
        # sub-region screenshot of the TV (e.g. zoom): origin is the region, not the monitor
        ctx = c.plan_shot((-3000, -1000, 800, 600), 1)
        self.assertEqual(ctx.scale, 1.0)
        self.assertEqual(c.shot_to_screen(ctx, 10, 20), (-2990, -980))
        self.assertEqual(c.screen_to_shot(ctx, -2990, -980), (10, 20))

    def test_intersect(self):
        self.assertEqual(c.intersect((0, 0, 10, 10), (5, 5, 10, 10)), (5, 5, 5, 5))
        self.assertIsNone(c.intersect((0, 0, 10, 10), (10, 0, 10, 10)))

    def test_scale_pct(self):
        self.assertEqual(PRIMARY.scale_pct, 100)
        self.assertEqual(TV.scale_pct, 200)


if __name__ == "__main__":
    unittest.main()
