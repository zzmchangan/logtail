"""TUI 滚轮/翻页键绑定测试(无 curses).

回归背景: 新增 vim 习惯的 Ctrl-F/Ctrl-B 翻页 (Ctrl-F=下翻一页同 PgDn,
Ctrl-B=上翻一页同 PgUp)。用假屏幕给出窗口高度, 验证这两个键把内容锚点
推到与 PgUp/PgDn一致的语义, 且到底后恢复自动跟随 (view_anchor=None)。
滚动模型自 20000 行缓冲改版后以"内容行"为锚点, 不再用绝对行号。
"""

import unittest
import curses
from typing import Tuple

from logtail.config import Config
from logtail.models import LogLine
from logtail.timeline import Timeline
from logtail.tui import Tui


class _FakeScreen:
    def getmaxyx(self):
        return (24, 80)          # 窗口高 24 -> log_h = 22


def _seed(tui: Tui, n: int) -> None:
    lines = []
    for i in range(n):
        lines.append(LogLine("scene", f"line{i:07d} " + ("xx" * 6),
                            (1_000_000.0 + i, 0), i, "", "INFO"))
    tui.timeline.feed(lines)


def make_tui() -> Tuple[Tui, tuple[int, int]]:
    tui = Tui(Config())
    tui.screen = _FakeScreen()
    tui.timeline = Timeline(tui.ruleset, maxlen=200)
    _seed(tui, 60)               # 60 行, 每行 1 显示行 -> 共 60 显示行
    return tui, (22, 38)         # (log_h, max_top=60-22)


class TestScrollKeys(unittest.TestCase):
    def test_ctrl_f_pages_down(self):
        """Ctrl-F (key=6) 下翻一页, 如同 PgDn."""
        tui, (log_h, _) = make_tui()
        # 冻结在顶部附近, 下翻一页锚点行号应增加 log_h, 未到底
        tui._handle_key(curses.KEY_HOME, "")
        top_before = tui._visible_idx_of(tui.timeline.visible(), tui.view_anchor)
        tui._handle_key(6, "")
        top_after = tui._visible_idx_of(tui.timeline.visible(), tui.view_anchor)
        self.assertEqual(top_after, top_before + log_h)

    def test_ctrl_f_at_bottom_follows(self):
        """Ctrl-F 已贴底时保持自动跟随 (view_anchor=None), 如同 PgDn."""
        tui, (_, _) = make_tui()
        tui.view_anchor, tui.view_offset = None, 0      # 跟随底部
        tui._handle_key(6, "")
        self.assertIsNone(tui.view_anchor)

    def test_ctrl_b_pages_up(self):
        """Ctrl-B (key=2) 上翻一页, 如同 PgUp; 不越过最顶."""
        tui, (log_h, _) = make_tui()
        # 从底部向上翻一页 -> 锚点行号 = (60-log_h) - log_h (底窗口顶端再上翻一页), 再翻夹到顶
        tui.view_anchor, tui.view_offset = None, 0
        tui._handle_key(2, "")
        top = tui._visible_idx_of(tui.timeline.visible(), tui.view_anchor)
        self.assertEqual(top, 60 - 2 * log_h)       # 60-44 = 16
        tui._handle_key(curses.KEY_HOME, "")
        self.assertEqual(tui._visible_idx_of(tui.timeline.visible(), tui.view_anchor), 0)
        tui._handle_key(2, "")
        self.assertEqual(tui._visible_idx_of(tui.timeline.visible(), tui.view_anchor), 0)

    def test_ctrl_keys_not_sniffed_as_stdin(self):
        """Ctrl-F/Ctrl-B 是控制码 (<32), 不能被当成可打印字符塞进输入框."""
        tui, _ = make_tui()
        tui._handle_key(6, "abc")
        tui._handle_key(2, "abc")
        # buf 不变 (滚动键不清输入), 且不是被当字符追加
        self.assertEqual(tui._handle_key(6, "abc")[1], "abc")


if __name__ == "__main__":
    unittest.main()
