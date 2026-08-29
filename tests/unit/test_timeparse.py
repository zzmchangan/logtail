"""timeparse 单元测试: 四种时间戳格式、占位日期、坏输入不炸."""

import unittest
from datetime import datetime

from logtail.timeparse import extract_timestamp, parse_timestamp

NOW = datetime(2026, 8, 27, 12, 0, 0)


class TestInvalidFullDate(unittest.TestCase):
    def test_invalid_values_return_none_not_throw(self):
        """形似完整日期但数值越界(99点/2月30/闰秒:60): 当"无时间戳"返回 None, 不抛.

        抛 ValueError 会被 reader 线程吞掉, 导致本批后续行静默丢失。
        """
        self.assertIsNone(extract_timestamp("[2026-08-27 99:00:00] x"))
        self.assertIsNone(extract_timestamp("[2026-02-30 10:00:00] x"))
        self.assertIsNone(extract_timestamp("[2026-08-27 10:00:60] x"))     # 闰秒
        self.assertIsNone(extract_timestamp("2026-08-27 10:00:60 x"))

    def test_valid_full_date_still_parses(self):
        hit = extract_timestamp("[2026-08-27 10:00:01.000000] x", NOW)
        self.assertIsNotNone(hit)


class TestBracketTime(unittest.TestCase):
    def test_bracket_hhmmss(self):
        hit = extract_timestamp("[10:20:30] hello", NOW)
        self.assertIsNotNone(hit)
        (sec, us), s, e = hit
        # 当天 10:20:30 与 NOW 同日: 相差 1h39m30s
        self.assertAlmostEqual(sec - datetime(2026, 8, 27).timestamp(), 10 * 3600 + 20 * 60 + 30)
        self.assertEqual(us, 0)
        self.assertEqual("[10:20:30]", "[10:20:30] hello"[s:e])

    def test_bracket_fraction_dot(self):
        hit = extract_timestamp("[10:20:30.123456] x", NOW)
        self.assertEqual(hit[0][1], 123456)

    def test_bracket_fraction_comma(self):
        hit = extract_timestamp("[10:20:30,5] x", NOW)
        # 1 位小数补足成 500000 微秒
        self.assertEqual(hit[0][1], 500000)


class TestBracketFullDate(unittest.TestCase):
    def test_bracket_full(self):
        hit = extract_timestamp("[2026-08-26 23:59:59.5] x", NOW)
        sec, us = hit[0]
        self.assertEqual(sec, datetime(2026, 8, 26, 23, 59, 59).timestamp())
        self.assertEqual(us, 500000)

    def test_bracket_full_t_separator(self):
        hit = extract_timestamp("[2026-01-02T03:04:05] x", NOW)
        self.assertEqual(hit[0][0], datetime(2026, 1, 2, 3, 4, 5).timestamp())


class TestBareForms(unittest.TestCase):
    def test_bare_fulldate(self):
        hit = extract_timestamp("2026-08-27 08:00:00 boom", NOW)
        self.assertEqual(hit[0][0], datetime(2026, 8, 27, 8, 0, 0).timestamp())
        # 剥离区间应覆盖时间戳本身
        self.assertEqual("2026-08-27 08:00:00", "2026-08-27 08:00:00 boom"[hit[1]:hit[2]])

    def test_bare_time_uses_today(self):
        hit = extract_timestamp("  01:02:03 t", NOW)
        self.assertAlmostEqual(hit[0][0] - datetime(2026, 8, 27).timestamp(), 3723)

    def test_none_on_no_timestamp(self):
        self.assertIsNone(extract_timestamp("plain text no ts", NOW))
        self.assertIsNone(extract_timestamp("", NOW))

    def test_parse_timestamp_wrapper(self):
        self.assertIsNone(parse_timestamp("nothing", NOW))
        self.assertIsNotNone(parse_timestamp("[01:02:03] x", NOW))


class TestEdgeCases(unittest.TestCase):
    def test_midnight_boundary(self):
        # 00:00:00 应为当天 0 秒
        hit = extract_timestamp("[00:00:00] x", NOW)
        self.assertAlmostEqual(hit[0][0] - datetime(2026, 8, 27).timestamp(), 0)

    def test_bracket_time_priority_over_fulldate(self):
        # [hh:mm:ss] 优先于完整日期: [10:20:30] 不应误吞日期形式
        hit = extract_timestamp("[10:20:30] later 2026-08-26 01:02:03", NOW)
        self.assertEqual(hit[2], len("[10:20:30]"))


if __name__ == "__main__":
    unittest.main()
