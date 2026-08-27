"""timeline 单元测试: 排序/淘汰/上下文(行数+时间)/trace/搜索/重着色."""

import unittest

from logtail.models import LogLine
from logtail.rules import RuleSet
from logtail.timeline import MODE_ALL, MODE_CONTEXT, MODE_TRACE, Timeline


def line(text, ts, seq, source="s") -> LogLine:
    return LogLine(source=source, text=text, ts_key=(float(ts), 0), seq=seq)


def make(n=10, rules=("err",)) -> Timeline:
    t = Timeline(RuleSet(keywords=list(rules)), maxlen=100)
    return t


class TestFeed(unittest.TestCase):
    def test_sorted_by_ts_then_seq(self):
        t = make()
        # 乱序喂入: 按 (ts, seq) 排
        t.feed([line("a", 3, 1), line("b", 1, 2), line("c", 1, 1), line("d", 2, 9)])
        got = [ln.text for ln, _ in t.ring]
        self.assertEqual(got, ["c", "b", "d", "a"])                  # ts 相同按 seq

    def test_empty_feed(self):
        t = make()
        t.feed([])
        self.assertEqual(len(t.ring), 0)

    def test_ring_eviction(self):
        t = Timeline(RuleSet(), maxlen=3)
        t.feed([line(str(i), i, i) for i in range(10)])
        self.assertEqual(len(t.ring), 3)
        self.assertEqual([ln.text for ln, _ in t.ring], ["7", "8", "9"])


class TestVisibleAll(unittest.TestCase):
    def test_all_mode_returns_everything(self):
        t = make()
        t.feed([line("x", 1, 1), line("err", 2, 2)])
        vis = t.visible()
        self.assertEqual(len(vis), 2)
        self.assertTrue(all(not dim for _, _, dim in vis))


class TestVisibleContext(unittest.TestCase):
    def test_line_context(self):
        t = make(rules=("hit",))
        texts = [f"row{i}" if i != 5 else "hit5" for i in range(10)]
        t.feed([line(texts[i], i, i) for i in range(10)])
        t.set_mode(MODE_CONTEXT)
        t.set_context_n(1)
        # 只有 "hit5" 命中 -> 前后各 1 行
        vis = t.visible()
        self.assertEqual([ln.text for ln, _, _ in vis], ["row4", "hit5", "row6"])

    def test_dim_marks_non_highlight(self):
        t = make(rules=("hit",))
        t.feed([line("a", 1, 1), line("hit", 2, 2), line("b", 3, 3)])
        t.set_mode(MODE_CONTEXT)
        t.set_context_n(1)
        dims = {ln.text: dim for ln, _, dim in t.visible()}
        self.assertEqual(dims, {"a": True, "hit": False, "b": True})

    def test_time_context(self):
        t = make(rules=("hit",))
        # 时间不均匀: 0,1,2 秒各一行, 100,101 秒两行 (其中 100 命中)
        rows = [("a", 0), ("b", 1), ("c", 2), ("hit100", 100), ("d", 101)]
        t.feed([line(txt, ts, i) for i, (txt, ts) in enumerate(rows)])
        t.set_mode(MODE_CONTEXT)
        self.assertTrue(t.set_context("10s"))                        # 命中行 (100) 前后 10s
        vis = [ln.text for ln, _, _ in t.visible()]
        self.assertEqual(vis, ["hit100", "d"])                       # 0/1/2 距离太远

    def test_set_context_specs(self):
        t = make()
        self.assertTrue(t.set_context("5"))
        self.assertEqual((t.context_n, t.context_seconds), (5, 0.0))
        self.assertTrue(t.set_context("3s"))
        self.assertEqual((t.context_n, t.context_seconds), (0, 3.0))
        self.assertTrue(t.set_context("2m"))
        self.assertEqual(t.context_seconds, 120.0)
        self.assertFalse(t.set_context("abc"))
        self.assertFalse(t.set_context(""))
        self.assertFalse(t.set_context("-1"))

    def test_context_zero_shows_only_hits(self):
        t = make(rules=("hit",))
        t.feed([line("a", 1, 1), line("hit", 2, 2), line("b", 3, 3)])
        t.set_mode(MODE_CONTEXT)
        t.set_context_n(0)
        self.assertEqual([ln.text for ln, _, _ in t.visible()], ["hit"])


class TestVisibleTrace(unittest.TestCase):
    def test_trace_pure_no_neighbors(self):
        t = make(rules=("hit",))
        t.feed([line("player enter", 1, 1), line("player leave", 2, 2), line("other", 3, 3)])
        t.set_mode(MODE_TRACE)
        t.trace_term = "player"
        vis = [ln.text for ln, _, _ in t.visible()]
        self.assertEqual(vis, ["player enter", "player leave"])

    def test_trace_case_insensitive(self):
        t = make()
        t.set_mode(MODE_TRACE)
        t.trace_term = "ABC"
        t.feed([line("x abc y", 1, 1)])
        self.assertEqual(len(t.visible()), 1)


class TestSearch(unittest.TestCase):
    def _setup(self):
        t = make()
        t.feed([line("alpha", 1, 1), line("beta", 2, 2),
                line("alpha", 3, 3), line("gamma", 4, 4)])
        return t

    def test_forward_wraparound(self):
        t = self._setup()
        rs = RuleSet(keywords=["alpha"])
        rule = rs.list_highlights()[0]
        self.assertEqual(t.search(rule, -1, +1), 0)                  # 第一条
        self.assertEqual(t.search(rule, 0, +1), 2)                   # 下一条
        self.assertEqual(t.search(rule, 2, +1), 0)                   # 回绕

    def test_backward(self):
        t = self._setup()
        rs = RuleSet(keywords=["alpha"])
        rule = rs.list_highlights()[0]
        self.assertEqual(t.search(rule, 2, -1), 0)
        self.assertEqual(t.search(rule, 0, -1), 2)                   # 回绕向上

    def test_no_match(self):
        t = self._setup()
        rs = RuleSet(keywords=["zzz"])
        self.assertEqual(t.search(rs.list_highlights()[0], -1, +1), -1)

    def test_callable_matcher(self):
        t = self._setup()
        self.assertEqual(t.search(lambda s: s == "beta", -1, +1), 1)

    def test_empty_buffer(self):
        t = make()
        self.assertEqual(t.search(lambda s: True, -1, +1), -1)


class TestRehash(unittest.TestCase):
    def test_rules_change_recolors_existing(self):
        rs = RuleSet()
        t = Timeline(rs)
        t.feed([line("err boom", 1, 1)])
        self.assertEqual(len(t.ring._items[0][1]), 0)                # 无规则: 无高亮
        rs.add("err", "highlight")
        t.rehash()
        self.assertEqual(len(t.ring._items[0][1]), 1)                # 重算后着色


class TestModeValidation(unittest.TestCase):
    def test_unknown_mode(self):
        with self.assertRaises(ValueError):
            make().set_mode("bogus")


if __name__ == "__main__":
    unittest.main()
