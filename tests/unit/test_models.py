"""models 单元测试: LogLine/SourceConfig/ColorPool/fmt_hhmmss."""

import time
import unittest

from logtail.models import (
    PALETTE_FG_COLORS, ColorPool, LogLine, SourceConfig, fmt_hhmmss,
)


def mkline(**kw) -> LogLine:
    base = dict(source="s", text="t", ts_key=(1000.0, 500000), seq=1)
    base.update(kw)
    return LogLine(**base)


class TestLogLine(unittest.TestCase):
    def test_ts_seconds_combines_us(self):
        self.assertAlmostEqual(mkline().ts_seconds, 1000.5)

    def test_defaults(self):
        ln = mkline()
        self.assertEqual(ln.time_str, "")
        self.assertEqual(ln.level, "")


class TestSourceConfig(unittest.TestCase):
    def test_frozen(self):
        sc = SourceConfig("n", "/p", "*.log")
        with self.assertRaises(Exception):
            sc.name = "other"


class TestColorPool(unittest.TestCase):
    def test_stable_assignment(self):
        pool = ColorPool()
        a1 = pool.color_for(1)
        self.assertEqual(pool.color_for(1), a1)                     # 稳定
        self.assertEqual(pool.color_name(1), PALETTE_FG_COLORS[a1])

    def test_different_ids_cycle(self):
        pool = ColorPool()
        seen = {pool.color_for(i) for i in range(1, len(PALETTE_FG_COLORS) + 1)}
        self.assertEqual(len(seen), len(PALETTE_FG_COLORS))         # 满轮不重

    def test_wraparound(self):
        pool = ColorPool()
        first = pool.color_for(1)
        # 用完整个调色板后, 下一个回到起始色
        for i in range(2, len(PALETTE_FG_COLORS) + 2):
            pool.color_for(i)
        self.assertEqual(pool.color_for(len(PALETTE_FG_COLORS) + 1), first)


class TestFmtHhmmss(unittest.TestCase):
    def test_format(self):
        lt = time.localtime(0)
        expect = f"[{lt.tm_hour:02d}:00:00.000]"
        self.assertEqual(fmt_hhmmss(0.0), expect)

    def test_ms_rounding(self):
        s = time.mktime(time.localtime(3661))
        out = fmt_hhmmss(s + 0.9999)
        self.assertTrue(out.endswith(".999]"))


if __name__ == "__main__":
    unittest.main()
