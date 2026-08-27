"""agent 单元测试: dump 各分支 / correlate / json 契约 / 辅助函数.

dump 走真实 LogFollower (线程), 用确定性夹具文件 + history 定位, 轮询保证稳定.
monitor 是无限循环, 放集成测试用子进程 + SIGINT 覆盖.
"""

import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

from logtail.agent import (
    _emit, _json_line, _provenance, _split_correlate, _split_terms,
    dump, format_line,
)
from logtail.config import Config
from logtail.models import LogLine, SourceConfig


def mkline(text, ts=1000.0, seq=1, source="s", level="") -> LogLine:
    return LogLine(source=source, text=text, ts_key=(float(ts), 0),
                   seq=seq, level=level)


def write_cfg_dir(files: dict, extra_yaml: str = "") -> Config:
    """建临时目录写多个日志文件, 返回指向它们的 Config."""
    d = tempfile.mkdtemp(prefix="lt_agent_")
    for name, text in files.items():
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(text)
    return Config(sources=[SourceConfig("src", d, "*.log")])


class TestHelpers(unittest.TestCase):
    def test_format_line(self):
        ln = mkline("body", source="gw")
        out = format_line(ln)
        parts = out.split(None, 1)
        self.assertEqual(len(parts), 2)
        self.assertIn("gw", out)
        self.assertTrue(out.endswith("body"))

    def test_format_line_prefers_time_str(self):
        ln = mkline("b", ts=0)
        ln.time_str = "[orig]"
        self.assertIn("[orig]", format_line(ln))

    def test_json_line_contract(self):
        raw = _json_line(mkline("t", ts=1.5, seq=7, source="x", level="WARN"))
        d = json.loads(raw)
        self.assertEqual(set(d), {"ts", "ts_seconds", "source", "level", "text", "seq"})
        self.assertEqual((d["ts_seconds"], d["seq"], d["source"], d["level"]), (1.5, 7, "x", "WARN"))

    def test_emit_switches_format(self):
        ln = mkline("t")
        self.assertIn('"text"', _emit(ln, True))
        self.assertNotIn('"text"', _emit(ln, False))

    def test_split_terms(self):
        self.assertEqual(_split_terms(None), [])
        self.assertEqual(_split_terms(""), [])
        self.assertEqual(_split_terms("a b,c  d"), ["a", "b", "c", "d"])

    def test_split_correlate(self):
        self.assertEqual(_split_correlate(None), (None, None, None))
        self.assertEqual(_split_correlate("player=007"), ("player", "007", "7"))
        self.assertEqual(_split_correlate("novalue"), (None, "novalue", None))
        self.assertEqual(_split_correlate("k= 42 "), ("k", "42", "42"))


class TestProvenance(unittest.TestCase):
    def _run(self, probe, summary, out_count=0, latest=None, corr=None):
        err = io.StringIO()
        with redirect_stderr(err):
            _provenance(probe, summary, out_count, latest, corr)
        return err.getvalue()

    def test_no_discovery_warns(self):
        out = self._run([{"source": "a", "files": 0, "discovered": False,
                          "dx_error": "dx 返回码 1"}], False)
        self.assertIn("warning", out)
        self.assertIn("未发现任何日志文件", out)

    def test_partial_dx_fail_warns(self):
        out = self._run([
            {"source": "a", "files": 1, "discovered": True, "dx_error": ""},
            {"source": "b", "files": 0, "discovered": False, "dx_error": "boom"},
        ], False)
        self.assertIn("warning", out)
        self.assertIn("b", out)

    def test_healthy_no_warning(self):
        out = self._run([{"source": "a", "files": 2, "discovered": True,
                          "dx_error": ""}], True, out_count=5, latest=1.0)
        self.assertNotIn("warning", out)
        d = json.loads(out.strip())
        self.assertEqual((d["kind"], d["matched"], d["latest_ts"]), ("logtail.summary", 5, 1.0))

    def test_summary_includes_correlate(self):
        out = self._run([{"source": "a", "files": 1, "discovered": True, "dx_error": ""}],
                        True, corr={"key": "player", "lines_with_key": 3, "matched": 1})
        d = json.loads(out.strip())
        self.assertEqual(d["correlate"]["lines_with_key"], 3)

    def test_empty_probe_no_output(self):
        self.assertEqual(self._run([], False, 0), "")


FIXTURE = (
    "[2026-08-27 10:00:01.000000] player=100 enter scene\n"
    "[2026-08-27 10:00:02.000000] [Info][1] tick s=aaaa&c=1\n"
    "[2026-08-27 10:00:03.000000] [Error][2] player=100 crash in dungeon\n"
    "[2026-08-27 10:00:04.000000] [Warn][3] player=200 slow frame\n"
    "[2026-08-27 10:00:05.000000] heartbeat ping\n"
)


def dump_to(cfg, *args, **kw) -> tuple:
    """跑 dump, 返回 (stdout 文本, exit code)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = dump(cfg, kw.pop("match", None), kw.pop("lines_n", 50),
                  wait=kw.pop("wait", 1.5), **kw)
    return buf.getvalue(), rc


class TestDump(unittest.TestCase):
    def setUp(self):
        self.cfg = write_cfg_dir({"a.log": FIXTURE})
        # 提前回溯全部内容, since 用日志时间(过去)保持全量在窗口内
        self.cfg.history = 100

    def test_plain_output_all(self):
        out, rc = dump_to(self.cfg)
        lines = out.strip().splitlines()
        self.assertEqual(rc, 0)
        self.assertEqual(len(lines), 5)                              # 无黑名单全出
        self.assertIn("player=100 enter scene", out)

    def test_blacklist_applies(self):
        self.cfg.blacklist = ["heartbeat"]
        out, _ = dump_to(self.cfg)
        self.assertNotIn("heartbeat", out)
        self.assertEqual(len(out.strip().splitlines()), 4)

    def test_match_filters(self):
        out, _ = dump_to(self.cfg, match="player=100")
        self.assertEqual(len(out.strip().splitlines()), 2)

    def test_exclude(self):
        out, _ = dump_to(self.cfg, match="player=100", exclude="crash")
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertIn("enter", out)

    def test_count(self):
        out, _ = dump_to(self.cfg, match="player=100", count_only=True)
        self.assertEqual(out.strip(), "2")

    def test_level_filter(self):
        self.cfg.level = "ERROR"
        out, _ = dump_to(self.cfg)
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertIn("crash", out)

    def test_context_global(self):
        out, _ = dump_to(self.cfg, match="crash", context=1)
        self.assertEqual(len(out.strip().splitlines()), 3)           # 前后各一行

    def test_correlate_cross_writes(self):
        self.cfg.correlation_keys = [{
            "name": "player",
            "extract": [r"Guid[:=] *(\d+)", r"roleId[:=] *(\d+)", r"player[:=] *(\d+)"],
        }]
        # 夹具里 player=100 出现两次
        out, _ = dump_to(self.cfg, correlate="player=100")
        self.assertEqual(len(out.strip().splitlines()), 2)

    def test_correlate_undefined_falls_back_literal(self):
        out, _ = dump_to(self.cfg, correlate="zzz=100")
        self.assertEqual(len(out.strip().splitlines()), 2)           # 字面 "100"

    def test_json_output(self):
        out, _ = dump_to(self.cfg, match="crash", as_json=True)
        d = json.loads(out.strip())
        self.assertEqual(d["level"], "ERROR")
        self.assertIn("crash", d["text"])

    def test_since_keeps_recent_anchor(self):
        # 全部行都在过去; since 相对"最新日志行"锚点 -> 大窗口全保留
        out, _ = dump_to(self.cfg, since=86400.0)
        self.assertEqual(len(out.strip().splitlines()), 5)
        # 小窗口: cutoff = 最新(05) - 0.5 - 1(容忍) = 03.5 -> 只留 04/05 两行
        out, _ = dump_to(self.cfg, since=0.5)
        self.assertEqual(len(out.strip().splitlines()), 2)

    def test_focus(self):
        out, _ = dump_to(self.cfg, focus="no-such-source")
        self.assertEqual(out.strip(), "")


    def test_exclude_only_filters_output(self):
        """坑1回归: --exclude 单独用 (无 match/correlate/count) 时, 正文输出也必须剔除."""
        out, _ = dump_to(self.cfg, exclude="player")
        self.assertNotIn("player", out)                             # enter/crash/slow 全剔
        self.assertEqual(len(out.strip().splitlines()), 2)          # 只剩 tick + heartbeat
        self.assertIn("tick", out)

    def test_exclude_only_keeps_non_matching(self):
        self.cfg.blacklist = ["heartbeat"]
        out, _ = dump_to(self.cfg, exclude="crash")
        self.assertNotIn("crash", out)
        self.assertIn("enter", out)                                  # 不含 crash 的行保留


class TestDumpEdgeCases(unittest.TestCase):
    def test_empty_dir_outputs_nothing_exit_zero(self):
        cfg = write_cfg_dir({})
        out, rc = dump_to(cfg)
        self.assertEqual((out.strip(), rc), ("", 0))


if __name__ == "__main__":
    unittest.main()
