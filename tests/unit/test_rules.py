"""rules 单元测试: 裸词/re: 匹配、增删清、级别过滤、坏正则报错."""

import unittest

from logtail.rules import Rule, RulePatternError, RuleSet


def rs(**kw) -> RuleSet:
    return RuleSet(keywords=kw.get("k"), blacklist=kw.get("b"))


class TestRule(unittest.TestCase):
    def test_bare_word_substring(self):
        r = Rule(1, "highlight", "timeout")
        self.assertTrue(r.matches("request TIMEOUT after 5s"))     # 大小写不敏感
        self.assertTrue(r.matches("my timeout handler"))
        self.assertFalse(r.matches("timed out"))                    # 非子串

    def test_regex_prefix(self):
        r = Rule(1, "highlight", r"re:player=\d+")
        self.assertTrue(r.matches("player=123 enter"))
        self.assertTrue(r.matches("PLAYER=999 leave"))              # IGNORECASE
        self.assertFalse(r.matches("player=abc"))

    def test_bad_regex_raises(self):
        with self.assertRaises(RulePatternError):
            Rule(1, "highlight", "re:[invalid")

    def test_repr(self):
        self.assertIn("timeout", repr(Rule(1, "highlight", "timeout")))


class TestRuleSetCrud(unittest.TestCase):
    def test_add_dedupe(self):
        r = rs(k=["a"])
        first = r.add("a", "highlight")
        self.assertEqual(len(r.list_highlights()), 1)

    def test_remove(self):
        r = rs(k=["a", "b"])
        self.assertTrue(r.remove("a", "highlight"))
        self.assertFalse(r.remove("a", "highlight"))                # 已不在
        self.assertEqual([x.pattern for x in r.list_highlights()], ["b"])

    def test_clear_kind(self):
        r = rs(k=["a"], b=["x"])
        self.assertEqual(r.clear("highlight"), 1)
        self.assertEqual(len(r.list_blacklist()), 1)                # 黑名单不受影响

    def test_reset(self):
        r = rs(k=["a"], b=["x"])
        r.add("tmp", "highlight")
        r.reset(["p"], ["q"])
        self.assertEqual([x.pattern for x in r.list_highlights()], ["p"])
        self.assertEqual([x.pattern for x in r.list_blacklist()], ["q"])

    def test_blocked(self):
        r = rs(b=["heartbeat", r"re:tick\d+"])
        self.assertTrue(r.blocked("HEARTBEAT ping"))
        self.assertTrue(r.blocked("tick42 fire"))
        self.assertFalse(r.blocked("normal line"))

    def test_highlights(self):
        r = rs(k=["err", "re:fatal\w*"])
        hits = r.highlights("FATAL error occurred")
        self.assertEqual(len(hits), 2)
        self.assertEqual(r.highlights("all good"), [])


class TestLevelFilter(unittest.TestCase):
    def test_set_filter(self):
        r = rs()
        r.set_level_filter("warn")                                   # 小写自动转大写
        self.assertTrue(r.level_ok("ERROR"))
        self.assertFalse(r.level_ok("INFO"))

    def test_set_filter_invalid(self):
        r = rs()
        with self.assertRaises(ValueError):
            r.set_level_filter("VERBOSE")

    def test_exclude_levels(self):
        r = rs()
        r.set_level_filter("TRACE")
        r.add_exclude_level("DEBUG")
        self.assertFalse(r.level_ok("DEBUG"))
        self.assertTrue(r.level_ok("INFO"))

    def test_reset_clears_level_state(self):
        r = rs()
        r.set_level_filter("ERROR")
        r.add_exclude_level("WARN")
        r.reset([], [])
        self.assertTrue(r.level_ok("DEBUG"))
        self.assertEqual(r.min_level, "")


if __name__ == "__main__":
    unittest.main()
