"""交互版滚动回溯 / 冻结内容锚定 回归测试(无 curses).

回归背景: 日志量一大 freeze 不住 —— 两条根因:
  1. 冻结锚的是"行号"而非"内容": 环形缓冲从头部淘汰旧行时, 绝对行号随之前移,
     冻住的那段被顶飞 (内容漂移)。
  2. 渲染每帧对整条环形缓冲折行 (wrap_text), 缓冲到 20000 行时单帧 300+ms,
     主循环直接卡死。

本组测试对准新模型: 冻结锚 LogLine 内容对象、渲染只折可见窗口 (window-bounded)。
"""

import unittest

import curses

from logtail.config import Config
from logtail.models import LogLine
from logtail.timeline import Timeline
from logtail.tui import Tui


class _FakeScreen:
    def getmaxyx(self):
        return (24, 80)          # log_h = 22


def _seed(tui: Tui, n: int, start: int = 0) -> None:
    lines = []
    for i in range(start, start + n):
        lines.append(LogLine("scene", f"line{i:07d} " + ("xx" * 10),
                            (1_000_000.0 + i, 0), i, "", "INFO"))
    tui.timeline.feed(lines)


def make_tui(maxlen: int = 2000) -> Tui:
    tui = Tui(Config())
    tui.screen = _FakeScreen()          # h=24 -> log_h=22
    tui.timeline = Timeline(tui.ruleset, maxlen=maxlen)
    return tui


class TestFreezeContentAnchor(unittest.TestCase):
    def test_anchor_survives_eviction(self):
        """冻结锚内容而非行号: 环淘汰旧行使锚点索引移动, 内容必须仍钉在顶部."""
        tui = make_tui(maxlen=2000)
        _seed(tui, 1000)
        visible = tui.timeline.visible()
        anchor = visible[500][0]
        tui.view_anchor = anchor
        w, log_h = 80, 22

        rows = tui._window(visible, log_h, w)
        self.assertEqual(len(rows), log_h)
        self.assertIs(rows[0][6], anchor, "冻结后顶部应是锚点行")

        # 灌新行, 环从头部淘汰旧行: 锚点行的索引前移, 但内容必须仍在视口顶部
        _seed(tui, 1500, start=1000)
        visible2 = tui.timeline.visible()
        self.assertEqual(len(visible2), 2000)     # 环被淘汰回 2000
        rows2 = tui._window(visible2, log_h, w)
        self.assertIs(rows2[0][6], anchor, "环淘汰后冻住的内容不能漂走")

    def test_anchor_evicted_unfreezes(self):
        """锚点行本身被淘汰(如 /clear、内容推出缓冲): 应解除冻结回到跟随."""
        tui = make_tui(maxlen=2000)
        _seed(tui, 1000)
        visible = tui.timeline.visible()
        anchor = visible[100][0]
        tui.view_anchor = anchor
        # 灌到把锚点整个挤出缓冲 (2500 行, 环只留最后 2000 -> 锚点(第100)/前面全淘汰)
        _seed(tui, 1500, start=1000)
        tui._window(tui.timeline.visible(), 22, 80)
        self.assertIsNone(tui.view_anchor, "锚点被淘汰应解除冻结")


class TestWindowBounded(unittest.TestCase):
    def test_window_bounded_follow_bottom(self):
        """跟随底部: 只折视口大小, 不把整个环折一遍."""
        tui = make_tui(maxlen=100000)
        _seed(tui, 50000)
        visible = tui.timeline.visible()
        self.assertEqual(len(visible), 50000)
        tui.view_anchor = None
        rows = tui._window(visible, 22, 80)
        self.assertLessEqual(len(rows), 22)

    def test_window_bounded_frozen(self):
        """冻结时同样有界, 且顶部是锚点行."""
        tui = make_tui(maxlen=100000)
        _seed(tui, 50000)
        visible = tui.timeline.visible()
        anchor = visible[100][0]
        tui.view_anchor = anchor
        rows = tui._window(visible, 22, 80)
        self.assertLessEqual(len(rows), 22)
        self.assertIs(rows[0][6], anchor)


class TestScrollMovesAnchor(unittest.TestCase):
    def test_up_enters_freeze_down_reaches_bottom_unfreezes(self):
        """向上滚进入冻结(锚底窗口顶端上一行), 逐行回溯, 向下滚到底解除冻结."""
        tui = make_tui(maxlen=1000)
        _seed(tui, 100)
        visible = tui.timeline.visible()
        tui.view_anchor = None
        tui._handle_key(curses.KEY_UP, "")           # 进入冻结: 底窗口顶端(78)上一行
        self.assertIs(tui.view_anchor, visible[77][0])
        tui._handle_key(curses.KEY_UP, "")
        self.assertIs(tui.view_anchor, visible[76][0])
        tui._handle_key(curses.KEY_DOWN, "")
        self.assertIs(tui.view_anchor, visible[77][0])
        tui._handle_key(curses.KEY_DOWN, "")         # 滚到底 -> 恢复跟随
        self.assertIsNone(tui.view_anchor)

    def test_home_top_end_bottom(self):
        """g/Home 跳最顶, End/G 回底部跟随."""
        tui = make_tui(maxlen=1000)
        _seed(tui, 100)
        visible = tui.timeline.visible()
        tui._handle_key(curses.KEY_HOME, "")
        self.assertIs(tui.view_anchor, visible[0][0])
        tui._handle_key(curses.KEY_END, "")
        self.assertIsNone(tui.view_anchor)


if __name__ == "__main__":
    unittest.main()
