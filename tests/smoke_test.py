"""模块级冒烟测试 (无需真实终端).

覆盖 rules (含正则、大小写不敏感)、timeparse (各格式、epoch 轴)、
reader (tail -F / history / 轮转)、timeline (排序/上下文窗/弱化标记)、
config (日期占位符、save 往返)。

用法: PYTHONPATH=. python3 tests/smoke_test.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from logtail import config as cfgmod        # noqa: E402
from logtail import rules, timeparse        # noqa: E402
from logtail.models import LogLine, SourceConfig  # noqa: E402
from logtail.reader import LogFollower       # noqa: E402
from logtail.timeline import MODE_ALL, MODE_CONTEXT, Timeline  # noqa: E402


def test_rules():
    rs = rules.RuleSet(keywords=["item_id", "re:player=123"], blacklist=["heartbeat", "DEBUG"])
    assert rs.blocked("HeartBeat request") and rs.blocked("debug line")
    assert not rs.blocked("normal line")
    hl = rs.highlights("Item_ID=5001 PLAYER=123")
    assert {r.pattern for r in hl} == {"item_id", "re:player=123"}
    # 动态增删
    rs.add("timeout", "highlight")
    assert any(r.pattern == "timeout" for r in rs.list_highlights())
    rs.remove("timeout", "highlight")
    rs.reset(["only"], [])
    assert [r.pattern for r in rs.list_highlights()] == ["only"]
    print("rules OK")


def test_timeparse():
    assert timeparse.parse_timestamp("[10:00:00.123]")[1] == 123000
    assert timeparse.parse_timestamp("[10:00:00]")[0] > 0
    assert timeparse.parse_timestamp("no ts") is None
    # epoch 轴与 time.time() 同轴 (秒为正, 微秒为 int)
    t = timeparse.parse_timestamp("[10:00:00.123]")
    assert t[0] > 0 and isinstance(t[1], int) and 0 <= t[1] < 1_000_000
    print("timeparse OK")


def test_reader():
    base = tempfile.mkdtemp(prefix="lt_rd_")
    try:
        g = os.path.join(base, "g")
        os.makedirs(g)
        with open(os.path.join(g, "a.log"), "w") as fh:
            fh.write("l1\nl2\n")
        # tail -F: 不读历史
        f = LogFollower([SourceConfig("g", g, "*.log")])
        f.start(); time.sleep(0.6)
        assert f.queue.drain() == []
        with open(os.path.join(g, "a.log"), "a") as fh:
            fh.write("[10:00:01.5] C2S x=1\n")
        time.sleep(0.6)
        out = f.queue.drain()
        assert len(out) == 1 and out[0].text.endswith("x=1")
        f.stop()
        print("reader tail -F OK")

        # history: 独立文件回末 N 行
        g2 = os.path.join(base, "g2"); os.makedirs(g2)
        with open(os.path.join(g2, "b.log"), "w") as fh:
            fh.write("l1\nl2\nl3\nl4\n")
        f2 = LogFollower([SourceConfig("g2", g2, "*.log")], history=2)
        f2.start(); time.sleep(0.8)
        texts = [l.text for l in f2.queue.drain()]
        assert texts == ["l3", "l4"], texts
        f2.stop()
        print("reader history OK")

        # 轮转: rename+新同名文件
        g3 = os.path.join(base, "g3"); os.makedirs(g3)
        with open(os.path.join(g3, "c.log"), "w") as fh:
            fh.write("old\n")
        f3 = LogFollower([SourceConfig("g3", g3, "*.log")])
        f3.start(); time.sleep(0.6); f3.queue.drain()
        os.rename(os.path.join(g3, "c.log"), os.path.join(g3, "c.log.1"))
        with open(os.path.join(g3, "c.log"), "w") as fh:
            fh.write("[10:00:05.0] fresh\n")
        time.sleep(0.8)
        assert any("fresh" in l.text for l in f3.queue.drain())
        f3.stop()
        print("reader rotate OK")
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_timeline():
    rs = rules.RuleSet(keywords=["ERROR"], blacklist=[])
    tl = Timeline(rs, maxlen=20)
    lines = [LogLine("s", f"plain {i}", (float(i), 0), i) for i in range(10)]
    lines.append(LogLine("s", "ERROR at 4", (4.0, 500), 200))
    tl.feed(lines)
    tl.set_mode(MODE_CONTEXT); tl.set_context_n(1)
    vis = tl.visible()
    assert [v[0].text for v in vis] == ["plain 4", "ERROR at 4", "plain 5"]
    assert [v[2] for v in vis] == [True, False, True]
    tl.set_mode(MODE_ALL)
    assert len(tl.visible()) == 11 and all(not v[2] for v in tl.visible())
    print("timeline OK")


def test_config():
    d = tempfile.mkdtemp(prefix="lt_cfg_")
    try:
        p = os.path.join(d, "c.yaml")
        with open(p, "w") as fh:
            fh.write(
                "log_sources:\n"
                "  - name: s\n"
                "    dx: \"dx log SceneServer {date}\"\n"
                "blacklist: [hb]\n"
                "keywords: [a]\n"
            )
        cfg = cfgmod.load_config(p, date="2026-08-27")
        assert cfg.sources[0].dx == "dx log SceneServer 2026-08-27"
        # save 往返保留 log_sources
        cfgmod.save_config(p, ["x", "y"], ["hb", "DEBUG"])
        cfg2 = cfgmod.load_config(p)
        assert cfg2.keywords == ["x", "y"]
        assert cfg2.blacklist == ["hb", "DEBUG"]
        assert cfg2.sources[0].name == "s"
        print("config OK")
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    test_rules()
    test_timeparse()
    test_reader()
    test_timeline()
    test_config()
    print("ALL SMOKE TESTS PASSED")
