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

    def test_remove_case_insensitive(self):
        """/rm 移除应与默认匹配一样大小写不敏感: remove("error") 应删掉 "ERROR"."""
        r = rs(k=["ERROR", "timeout"])
        n = r.remove("error", "highlight")          # 大小写不同也能删
        self.assertEqual(n, 1)
        self.assertNotIn("ERROR", [x.pattern for x in r.list_highlights()])
        self.assertIn("timeout", [x.pattern for x in r.list_highlights()])

    def test_remove_all_case_variants(self):
        """同一关键词的大小写变体应一起删掉, 不留冗余规则."""
        r = rs(k=["ERROR", "error", "ok"])
        n = r.remove("eRrOr", "highlight")
        self.assertEqual(n, 2)
        self.assertEqual([x.pattern for x in r.list_highlights()], ["ok"])

    def test_remove_respects_case_sensitive(self):
        """case_sensitive 配置下移除按精确匹配, 不误删大小写不同的规则."""
        r = RuleSet(keywords=["Dragon", "dragon2"], case_sensitive=True)
        self.assertEqual(r.remove("dragon", "highlight"), 0)        # 精确才删
        self.assertEqual(r.remove("Dragon", "highlight"), 1)
        self.assertEqual([x.pattern for x in r.list_highlights()], ["dragon2"])

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


class TestCaseSensitive(unittest.TestCase):
    def test_bare_word(self):
        r = Rule(1, "highlight", "Dragon", case_sensitive=True)
        self.assertTrue(r.matches("Dragon island"))
        self.assertFalse(r.matches("dragon2 account"))             # 精确: 不撞小写
        self.assertFalse(r.matches("DRAGON"))

    def test_bare_word_default_insensitive(self):
        r = Rule(1, "highlight", "Dragon")
        self.assertTrue(r.matches("dragon2"))                      # 默认: 撞

    def test_regex(self):
        r = Rule(1, "highlight", "re:Error", case_sensitive=True)
        self.assertTrue(r.matches("[Error] x"))
        self.assertFalse(r.matches("[ERROR] x"))
        r2 = Rule(1, "highlight", "re:Error")
        self.assertTrue(r2.matches("[ERROR] x"))                   # 默认 IGNORECASE

    def test_ruleset_inherits(self):
        rs = RuleSet(blacklist=["DEBUG"], case_sensitive=True)
        self.assertTrue(rs.blocked("DEBUG level"))
        self.assertFalse(rs.blocked("[Debug] level"))
        rs2 = RuleSet(blacklist=["DEBUG"])
        self.assertTrue(rs2.blocked("[Debug] level"))              # 默认滤掉

    def test_reset_inherits(self):
        rs = RuleSet(case_sensitive=True)
        rs.reset([], ["X"])
        self.assertFalse(rs.blocked("x"))


if __name__ == "__main__":
    unittest.main()
