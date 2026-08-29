"""TUI 命令 /goto 与 /source 的单元测试(无 curses).

回归背景: 新增两条交互命令 —— /goto 按时间跳转缓冲内某时刻, /source 每源开关
(显示层过滤, 照读照存)。本组验证命令逻辑与过滤语义, 不依赖真实终端。
"""

import unittest

from logtail.config import Config
from logtail.models import LogLine, SourceConfig
from logtail.timeline import Timeline
from logtail.tui import Tui, _goto, _source_cmd


class _FakeScreen:
    def getmaxyx(self):
        return (30, 120)          # log_h = 28


def make():
    tui = Tui(Config())
    tui.screen = _FakeScreen()
    tui.cfg.sources = [SourceConfig("scene", "/tmp", "s*.log"),
                       SourceConfig("guild", "/tmp", "g*.log")]
    tui.timeline = Timeline(tui.ruleset, maxlen=500)
    lines = []
    for i in range(40):
        ts = float(1_000_000 + i)
        src = "scene" if i % 2 else "guild"
        lines.append(LogLine(src, f"msg{i}", (ts, 0), i, "", "INFO"))
    tui.timeline.feed(lines)
    return tui


class TestSourceCmd(unittest.TestCase):
    def test_off_hides_that_source_only(self):
        tui = make()
        _source_cmd(tui, ["/source", "scene", "off"])
        vis = tui._vis()
        self.assertTrue(vis)
        self.assertTrue(all(ln.source != "scene" for ln, _h, _d in vis))

    def test_only_keeps_one_source(self):
        tui = make()
        _source_cmd(tui, ["/source", "scene", "only"])
        vis = tui._vis()
        self.assertTrue(vis)
        self.assertTrue(all(ln.source == "scene" for ln, _h, _d in vis))

    def test_all_clears_filters(self):
        tui = make()
        _source_cmd(tui, ["/source", "scene", "off"])
        _source_cmd(tui, ["/source", "scene", "only"])
        _source_cmd(tui, ["/source", "all"])
        self.assertEqual(len(tui._vis()), 40)          # 全部恢复

    def test_source_not_in_config_warns(self):
        tui = make()
        r = _source_cmd(tui, ["/source", "nope", "off"])
        self.assertIn("不在", r)
        self.assertEqual(len(tui._vis()), 40)          # 未误伤现有源


class TestGoto(unittest.TestCase):
    def test_goto_time_sets_anchor(self):
        tui = make()
        r = _goto(tui, ["/goto", "11:23:45"])
        self.assertIsNotNone(tui.view_anchor)
        self.assertIn("已跳", r)

    def test_goto_full_date_sets_anchor(self):
        tui = make()
        _goto(tui, ["/goto", "2026-08-27 11:23:45"])
        self.assertIsNotNone(tui.view_anchor)

    def test_goto_invalid_time(self):
        tui = make()
        r = _goto(tui, ["/goto", "nonsense"])
        self.assertIn("无效", r)

    def test_goto_no_arg(self):
        tui = make()
        r = _goto(tui, ["/goto"])
        self.assertIn("用法", r)

    def test_goto_empty_buffer(self):
        tui = make()
        tui.timeline.clear()
        r = _goto(tui, ["/goto", "11:23:45"])
        self.assertIn("缓冲为空", r)


if __name__ == "__main__":
    unittest.main()
