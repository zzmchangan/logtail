"""TUI 滚轮/翻页键绑定测试(无 curses).

回归背景: 新增 vim 习惯的 Ctrl-F/Ctrl-B 翻页 (Ctrl-F=下翻一页同 PgDn,
Ctrl-B=上翻一页同 PgUp)。用假屏幕给出窗口高度, 验证这两个键把 view_top
推到与 PgUp/PgDn一致的语义, 且到底后恢复自动跟随 (view_top=None)。
"""

import unittest
from typing import Tuple

from logtail.config import Config
from logtail.tui import Tui


class _FakeScreen:
    def getmaxyx(self):
        return (24, 80)


def make_tui() -> Tuple[Tui, tuple[int, int]]:
    tui = Tui(Config())
    tui.screen = _FakeScreen()      # 窗口高 24 -> log_h = 22
    tui._row_total = 60             # 60 显示行 -> max_top = 60 - 22 = 38
    return tui, (22, 38)


class TestScrollKeys(unittest.TestCase):
    def test_ctrl_f_pages_down(self):
        """Ctrl-F (key=6) 下翻一页, 如同 PgDn."""
        tui, (log_h, max_top) = make_tui()
        tui.view_top = 5
        tui._handle_key(6, "")
        self.assertEqual(tui.view_top, 5 + log_h)      # 27, 未到底

    def test_ctrl_f_at_bottom_follows(self):
        """Ctrl-F 已贴底时保持自动跟随 (view_top=None), 如同 PgDn."""
        tui, (log_h, max_top) = make_tui()
        tui.view_top = None                              # 跟随底部
        tui._handle_key(6, "")
        self.assertIsNone(tui.view_top)

    def test_ctrl_b_pages_up(self):
        """Ctrl-B (key=2) 上翻一页, 如同 PgUp; 不越过最顶 0."""
        tui, (log_h, max_top) = make_tui()
        tui.view_top = 30
        tui._handle_key(2, "")
        self.assertEqual(tui.view_top, 30 - log_h)       # 8
        tui.view_top = 5
        tui._handle_key(2, "")
        self.assertEqual(tui.view_top, 0)                 # 夹到顶部

    def test_ctrl_keys_not_sniffed_as_stdin(self):
        """Ctrl-F/Ctrl-B 是控制码 (<32), 不能被当成可打印字符塞进输入框."""
        tui, _ = make_tui()
        tui._handle_key(6, "abc")
        tui._handle_key(2, "abc")
        # buf 不变 (滚动键不清输入), 且不是被当字符追加
        self.assertEqual(tui._handle_key(6, "abc")[1], "abc")


if __name__ == "__main__":
    unittest.main()
