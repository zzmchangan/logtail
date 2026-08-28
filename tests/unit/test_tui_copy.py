"""TUI copy 功能单元测试 (不跑 curses): clipboard helper + /copy 命令逻辑."""

import os
import tempfile
import unittest

from logtail.models import LogLine
from logtail.tui import copy_to_clipboard


def mkline(text, ts=1000.0, seq=1, source="s"):
    return LogLine(source=source, text=text, ts_key=(float(ts), 0), seq=seq)


class TestCopyToClipboard(unittest.TestCase):
    def test_writes_file(self):
        p = os.path.join(tempfile.mkdtemp(), "c.txt")
        ok, path = copy_to_clipboard("hello\nworld", p)
        self.assertEqual(path, p)
        self.assertEqual(open(p).read(), "hello\nworld")
        # osc52 取决于 /dev/tty 可写性(测试环境通常 False), 两者都合法
        self.assertIsInstance(ok, bool)

    def test_empty_text_ok(self):
        p = os.path.join(tempfile.mkdtemp(), "c2.txt")
        _, path = copy_to_clipboard("", p)
        self.assertEqual(open(p).read(), "")


if __name__ == "__main__":
    unittest.main()
