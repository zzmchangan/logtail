"""TUI 渲染层对 NUL 字节的防御测试(无真实终端).

回归背景: 日志字节里可能含 NUL (\x00) —— 二进制垃圾/结构数据。NUL 是合法 UTF-8
字符, 会在 reader 的 decode(errors="replace") 中原样穿透进 LogLine.text;
curses 的 addnstr 遇到内嵌 NUL 会抛 ValueError: embedded null character,
让 TUI 整个崩溃。

修复只在渲染边界剥掉 NUL, 不影响数据源, 因此 agent/--json/stdout 契约不受影响。
本测试用假的 stdscr 复刻 curses 对 NUL 的拒绝, 确保渲染路径绝不把 NUL 交给 addnstr。
"""

import unittest

from logtail.config import Config
from logtail.tui import Tui


class _FakeStdscr:
    """模拟 curses 屏幕的极小实现; addnstr 遇到 NUL 抛 ValueError (与真实 curses 一致)."""

    def __init__(self) -> None:
        self.drawn: list = []          # (row, col, text, n)

    def addnstr(self, row, col, text, n, attr=0):
        if "\x00" in text:
            raise ValueError("embedded null character")
        self.drawn.append((row, col, text, n))

    def move(self, row, col):
        pass

    def refresh(self):
        pass


def make_tui() -> Tui:
    return Tui(Config())            # __init__ 不触碰 curses


class TestNulGuard(unittest.TestCase):
    def test_plain_seg_with_nul_does_not_crash(self):
        """报告的就是这条: 无高亮(hl_rules 空)分支直接把 seg 交给 addnstr.

        含 NUL 的日志行在修复前会让 addnstr 抛 ValueError: embedded null character。
        """
        tui = make_tui()
        fake = _FakeStdscr()
        # 普通行正常渲染
        tui._render_highlighted(fake, 0, 0, "hello world", [], False, 40)
        self.assertIn("hello world", [d[2] for d in fake.drawn])
        # 含 NUL 的行: 送入 addnstr 的文本里不得再有 NUL
        fake.drawn.clear()
        tui._render_highlighted(fake, 0, 0, "a\x00b", [], False, 40)
        texts = [d[2] for d in fake.drawn]
        self.assertTrue(texts, "应至少渲染出内容")
        self.assertNotIn("\x00", "".join(texts))
        self.assertIn("ab", "".join(texts))

    def test_prefix_with_nul_does_not_crash(self):
        """前缀(时间戳+来源)若带 NUL, 渲染时同样不得崩; NUL 被剥掉."""
        tui = make_tui()
        fake = _FakeStdscr()
        tui._render_display_row(fake, 0, "[12:00:00] scene\x00      ",
                                "body", [], False, 40)
        texts = [d[2] for d in fake.drawn]
        self.assertNotIn("\x00", "".join(texts))

    def test_highlighted_span_tail_with_nul_does_not_crash(self):
        """高亮分支: 命中词之后/之间的切片若夹带 NUL, 也不得抛错."""
        tui = make_tui()
        tui.has_color = True          # 走高亮分支
        rule = tui.ruleset.add("KEY", "highlight")     # 真实 Rule, 含 pattern/rule_id
        fake = _FakeStdscr()
        # 命中片段后跟 NUL: seg = "KEY\x00tail"
        tui._render_highlighted(fake, 0, 0, "KEY\x00tail", [rule], False, 40)
        texts = "".join(d[2] for d in fake.drawn)
        self.assertNotIn("\x00", texts)


if __name__ == "__main__":
    unittest.main()
