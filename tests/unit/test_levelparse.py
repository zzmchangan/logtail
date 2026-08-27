"""levelparse 单元测试: 级别识别、别名归一、词边界、权重."""

import unittest

from logtail.levelparse import (
    LEVEL_ORDER, filter_by_level, level_weight, parse_level,
)


class TestParseLevel(unittest.TestCase):
    def test_plain_tokens(self):
        for lv in LEVEL_ORDER:
            self.assertEqual(parse_level(f"[{lv}] msg"), lv)
            self.assertEqual(parse_level(f"xx {lv.lower()} yy"), lv)

    def test_aliases_normalize(self):
        self.assertEqual(parse_level("WARNING!"), "WARN")
        self.assertEqual(parse_level("err again"), "ERROR")
        self.assertEqual(parse_level("CRIT fail"), "FATAL")
        self.assertEqual(parse_level("CRITICAL"), "FATAL")

    def test_word_boundary(self):
        # 词边界: 不能把 DEBUGGER 识别成 DEBUG
        self.assertEqual(parse_level("DEBUGGER attached"), "")
        self.assertEqual(parse_level("information"), "")

    def test_wrapped_forms(self):
        self.assertEqual(parse_level("[Warn] low mem"), "WARN")
        self.assertEqual(parse_level("(INFO) got it"), "INFO")
        self.assertEqual(parse_level("level=ERROR boom"), "ERROR")

    def test_empty_and_none(self):
        self.assertEqual(parse_level(""), "")
        self.assertEqual(parse_level("nothing here"), "")

    def test_first_hit_wins(self):
        # ERROR 出现在 INFO 之前 -> 取先出现的
        self.assertEqual(parse_level("ERROR then INFO"), "ERROR")


class TestWeights(unittest.TestCase):
    def test_order(self):
        self.assertLess(level_weight("TRACE"), level_weight("DEBUG"))
        self.assertLess(level_weight("INFO"), level_weight("WARN"))
        self.assertLess(level_weight("WARN"), level_weight("ERROR"))
        self.assertLess(level_weight("ERROR"), level_weight("FATAL"))

    def test_unknown(self):
        self.assertEqual(level_weight(""), -1)
        self.assertEqual(level_weight("BOGUS"), -1)


class TestFilterByLevel(unittest.TestCase):
    def test_ge(self):
        self.assertTrue(filter_by_level("ERROR", "WARN"))
        self.assertTrue(filter_by_level("WARN", "WARN"))
        self.assertFalse(filter_by_level("INFO", "WARN"))

    def test_no_min_passes_all(self):
        self.assertTrue(filter_by_level("", ""))
        self.assertTrue(filter_by_level("DEBUG", ""))

    def test_no_level_fails_min(self):
        # 无级别行不满足任何 min_level (权重 -1 最小)
        self.assertFalse(filter_by_level("", "INFO"))


if __name__ == "__main__":
    unittest.main()
