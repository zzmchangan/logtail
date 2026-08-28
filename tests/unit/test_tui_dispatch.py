"""TUI 命令分发测试(无 curses): 已知命令集合与 _dispatch 裁决的一致性.

回归背景: /less 实现时只接了 _run_command 忘了登记 _KNOWN_COMMANDS,
输入被搜索分支截胡("输入这些都进搜索命令了")。
"""

import unittest

from logtail.config import Config
from logtail.tui import _KNOWN_COMMANDS, Tui


def make_tui() -> Tui:
    return Tui(Config())            # __init__ 不触碰 curses


class TestDispatch(unittest.TestCase):
    def test_new_commands_registered(self):
        self.assertIn("/less", _KNOWN_COMMANDS, "/less 未登记, 会被搜索分支截胡")

    def test_dispatch_command_not_search(self):
        """命令走命令分支(空缓冲给提示), 而不是被当搜索词."""
        tui = make_tui()
        msg = tui._dispatch("/less", "")
        self.assertNotIn("搜索", msg)
        self.assertIn("缓冲区为空", msg)

    def test_dispatch_non_command_is_search(self):
        tui = make_tui()
        msg = tui._dispatch("/somekeyword", "")
        self.assertIn("搜索", msg)                          # 非命令仍走搜索

    def test_quit_aliases_registered(self):
        """/quit 与 /q 都应登记, 走命令分支而非搜索分支."""
        self.assertIn("/quit", _KNOWN_COMMANDS)
        self.assertIn("/q", _KNOWN_COMMANDS)

    def test_q_quits(self):
        """/q 作为 /quit 别名直接退出 (SystemExit)."""
        tui = make_tui()
        with self.assertRaises(SystemExit):
            tui._dispatch("/q", "")

    def test_quit_quits(self):
        tui = make_tui()
        with self.assertRaises(SystemExit):
            tui._dispatch("/quit", "")


if __name__ == "__main__":
    unittest.main()
