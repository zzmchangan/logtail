"""CLI 集成测试: 子进程端到端跑 `python -m logtail`, 覆盖全 flag 矩阵与契约.

验证的不只是行为, 还有对外的稳定契约:
 - stdout/stderr 分离, 退出码 0/2, --help/--version/--diagnose 输出格式。
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ENV = {**os.environ, "PYTHONPATH": ROOT}

FIXTURE = {
    "scene.log": (
        "[2026-08-27 10:00:01.000000] player=100 enter scene\n"
        "[2026-08-27 10:00:02.000000] [Info][1] tick s=aaaa&c=1\n"
        "[2026-08-27 10:00:03.000000] [Error][2] player=100 crash in dungeon\n"
        "[2026-08-27 10:00:04.000000] [Warn][3] player=200 slow frame\n"
        "[2026-08-27 10:00:05.000000] heartbeat ping\n"
    ),
    "guild.log": (
        "[2026-08-27 10:00:01.500000] roleId: 100 join guild\n"
        "[2026-08-27 10:00:02.500000] [Info][4] BroadcastTopicUpdate ok\n"
        "[2026-08-27 10:00:03.500000] [Error][5] Guid:100 guild save fail\n"
        "[2026-08-27 10:00:04.500000] [Debug][6] detail dump\n"
    ),
}

CFG = """\
log_sources:
  - name: scene
    path: {dir}
    pattern: "scene*.log"
  - name: guild
    path: {dir}
    pattern: "guild*.log"
blacklist: ["heartbeat"]
keywords: []
correlation_keys:
  - name: player
    extract:
      - "Guid[:=] *(\\\\d+)"
      - "roleId[:=] *(\\\\d+)"
      - "player[:=] *(\\\\d+)"
"""


class CliCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="lt_cli_")
        for name, text in FIXTURE.items():
            with open(os.path.join(cls.dir, name), "w", encoding="utf-8") as f:
                f.write(text)
        cls.cfg = os.path.join(cls.dir, "cfg.yaml")
        with open(cls.cfg, "w", encoding="utf-8") as f:
            f.write(CFG.format(dir=cls.dir))

    def cli(self, *args, timeout=30):
        return subprocess.run(
            [sys.executable, "-m", "logtail", *args],
            capture_output=True, text=True, timeout=timeout, cwd=ROOT, env=ENV)

    def A(self, *extra):
        """agent dump 基础参数: 全量窗口."""
        return self.cli("--agent", "--config", self.cfg,
                        "--wait", "1", "--lines", "50",
                        "--since", "8760h", *extra)

    def sources_of(self, out):
        return {l.split("] ")[1].split()[0] for l in out.strip().splitlines() if l}


class TestOutputContract(CliCase):
    def test_help_stdout_exit0(self):
        r = self.cli("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("--correlate", r.stdout)
        self.assertEqual(r.stderr, "")

    def test_help_maintained_with_reading_model(self):
        """--help 必须与语义同步维护: 三层读取模型/上限/各 flag 作用域都要在."""
        r = self.cli("--help")
        for needle in ("三层读取模型", "8MB", "--date", "--correlate",
                       "--ctx-same", "--focus", "--diagnose", "假阴性",
                       "硬上限", "读取未完成", "字面量", "head",
                       "case-sensitive", "anchor", "discover-keys",
                       "--at", "--keep", "--blacklist-del", "--no-blacklist",
                       "--enable-source", "max-line-len", "回溯深度"):
            self.assertIn(needle, r.stdout)

    def test_version(self):
        r = self.cli("--version")
        self.assertEqual(r.returncode, 0)
        self.assertIn("logtail", r.stdout)

    def test_exit_0_on_zero_matches(self):
        r = self.A("--match", "zzz_nothing")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")

    def test_exit_2_bad_config(self):
        r = self.cli("--agent", "--config", "/nonexistent.yaml")
        self.assertEqual(r.returncode, 2)
        self.assertIn("配置", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_exit_2_bad_regex(self):
        r = self.A("--match", "re:[invalid")
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("Traceback", r.stderr)

    def test_exit_2_bad_since(self):
        r = self.cli("--agent", "--config", self.cfg, "--since", "5x")
        self.assertEqual(r.returncode, 2)

    def test_exit_2_bad_level(self):
        """坑5回归: 无效 --level 必须 fail-fast exit 2, 而非静默忽略输出全量."""
        r = self.A("--level", "DEUGB")
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIn("level", r.stderr.lower())

    def test_exit_2_unknown_focus(self):
        """第三场演练回归: --focus 未知/typo/大小写错 必须 fail-fast 并列出可用源名.

        旧行为: 静默 count=0 / exit=0 / stderr 空 —— 假阴性防护的最后盲区
        (--diagnose/--summary 只证明"源活着", 证明不了"focus 拼对了")。
        """
        for bad in ("scne", "GUILD", "no-such-source"):
            r = self.A("--focus", bad, "--count")
            self.assertEqual(r.returncode, 2, f"--focus {bad} 应 exit 2")
            self.assertNotIn("Traceback", r.stderr)
            # 错误信息里列出可用源名, typo 一眼可见
            self.assertIn("scene", r.stderr)
            self.assertIn("guild", r.stderr)

    def test_exit_2_bad_source_format(self):
        r = self.cli("--agent", "--config", self.cfg, "-s", "nopathsep")
        self.assertEqual(r.returncode, 2)

    def test_stdout_clean_of_errors(self):
        r = self.A()
        self.assertEqual(r.stderr, "")                               # 健康时 stderr 干净

    def test_line_format(self):
        r = self.A()
        for line in r.stdout.strip().splitlines():
            self.assertRegex(line, r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+\] \w+")


class TestFilteringMatrix(CliCase):
    def test_blacklist_applied(self):
        r = self.A()
        self.assertNotIn("heartbeat", r.stdout)
        self.assertEqual(len(r.stdout.strip().splitlines()), 8)      # 9-1

    def test_global_time_order(self):
        r = self.A()
        lines = r.stdout.strip().splitlines()
        ts = [l.split("]")[0] for l in lines]
        self.assertEqual(ts, sorted(ts))

    def test_match(self):
        r = self.A("--match", "crash")
        self.assertEqual(len(r.stdout.strip().splitlines()), 1)

    def test_match_multi_or(self):
        r = self.A("--match", "crash slow")
        self.assertEqual(len(r.stdout.strip().splitlines()), 2)

    def test_match_pipe_is_literal(self):
        """排坑回归: --match 'a|b' 的 | 是字面量, 不是正则 OR (0 命中而非两词并集)."""
        r = self.A("--match", "tick|dump")
        self.assertEqual(r.stdout.strip(), "")                    # 无行含字面 "tick|dump"
        r = self.A("--match", "re:(tick|dump)")
        self.assertEqual(len(r.stdout.strip().splitlines()), 2)   # 正则 OR 才是并集

    def test_case_sensitive_flag(self):
        """--case-sensitive: 精确匹配裸词/正则/黑名单; 不传时行为与旧版一致."""
        # 夹具: "[Error][2] player=100 crash" 含小写 player / 大写 Error
        # 1) 裸词: 默认不敏感 Dragon==dragon; 敏感则不命中
        r = self.A("--match", "player=100")
        self.assertEqual(len(r.stdout.strip().splitlines()), 2)   # 不敏感: 命中
        r = self.A("--match", "PLAYER=100")
        self.assertEqual(len(r.stdout.strip().splitlines()), 2)   # 不敏感: 大写也命中
        r = self.A("--case-sensitive", "--match", "PLAYER=100")
        self.assertEqual(r.stdout.strip(), "")                    # 敏感: 大写不命中
        r = self.A("--case-sensitive", "--match", "player=100")
        self.assertEqual(len(r.stdout.strip().splitlines()), 2)   # 敏感: 精确命中
        # 2) 级别词陷阱: 敏感下 ERROR 撞不到 [Error]
        r = self.A("--match", "ERROR")
        self.assertEqual(len(r.stdout.strip().splitlines()), 2)   # 不敏感: [Error] 命中
        r = self.A("--case-sensitive", "--match", "ERROR")
        self.assertEqual(r.stdout.strip(), "")                    # 敏感: 撞不到
        # 3) re: 正则同样受控
        r = self.A("--case-sensitive", "--match", "re:ERROR")
        self.assertEqual(r.stdout.strip(), "")
        r = self.A("--case-sensitive", "--match", "re:Error")
        self.assertEqual(len(r.stdout.strip().splitlines()), 2)   # 大小写精确
        # 4) 黑名单: 敏感下 "DEBUG" 滤不掉 "[Debug]" (主配置 DEBUG 黑名单依赖不敏感)
        cfg_cs = os.path.join(self.dir, "cs.yaml")
        with open(cfg_cs, "w") as f:
            f.write(f"log_sources:\n  - name: s\n    path: {self.dir}\n"
                    f'    pattern: "guild*.log"\nblacklist: ["DEBUG"]\n')
        base = ["--agent", "--config", cfg_cs, "--wait", "1",
                "--lines", "50", "--since", "8760h"]
        r = self.cli(*base)
        self.assertNotIn("detail dump", r.stdout)                 # 不敏感: [Debug] 行被滤
        r = self.cli(*base, "--case-sensitive")
        self.assertIn("detail dump", r.stdout)                     # 敏感: DEBUG≠[Debug], 保留

    def test_anchor_pins_window_across_runs(self):
        """强建议#1: --anchor 钉窗 —— 新日志到来后跨次 --count 仍可比.

        无 anchor: 窗口随最新时间戳滑动, 追加新行后窗口前移 (旧行可能推出、
        新行算进来); 有 anchor: 窗口钉死 [anchor-since, anchor], 追加的新行
        不算进来 (双边夹), 跨次运行可比。
        """
        import time as _t
        t0 = _t.time() - 60                                        # 锚点: 1 分钟前 (实时场景)
        fmt = "%Y-%m-%d %H:%M:%S"
        log = os.path.join(self.dir, "anchor.log")
        with open(log, "w") as f:
            f.write(f"[{_t.strftime(fmt, _t.localtime(t0 - 300))}] early marker\n")
            f.write(f"[{_t.strftime(fmt, _t.localtime(t0))}] anchor point\n")
        cfg2 = os.path.join(self.dir, "anchor.yaml")
        with open(cfg2, "w") as f:
            f.write(f"log_sources:\n  - name: s\n    path: {self.dir}\n"
                    f'    pattern: "anchor.log"\nblacklist: []\n')
        base = ["--agent", "--config", cfg2, "--wait", "1", "--lines", "50"]
        # 第一次 (since=120s): 窗口 [t0-120, t0] -> anchor point 在内, 早前 marker 在外
        r1 = self.cli(*base, "--since", "120s", "--match", "marker", "--count")
        self.assertEqual(int(r1.stdout.strip()), 0)
        # 追加更新的行 (模拟日志继续写)
        with open(log, "a") as f:
            f.write(f"[{_t.strftime(fmt, _t.localtime(t0 + 50))}] early marker later\n")
        # 无 anchor: 窗口滑到 [latest-120, latest], 新行算进来 -> 1 (漂移)
        r2 = self.cli(*base, "--since", "120s", "--match", "marker", "--count")
        self.assertEqual(int(r2.stdout.strip()), 1)
        # anchor 钉在 t0: 窗口 [t0-120, t0] -> 新行(t0+50)被上界夹掉 -> 仍 0, 与第一次可比
        r3 = self.cli(*base, "--since", "120s", "--anchor", str(int(t0)),
                      "--match", "marker", "--count")
        self.assertEqual(int(r3.stdout.strip()), 0)
        # anchor 窗口内的行仍正常读到: anchor point 在 [t0-120, t0] 内
        r4 = self.cli(*base, "--since", "120s", "--anchor", str(int(t0)),
                      "--match", "point", "--count")
        self.assertEqual(int(r4.stdout.strip()), 1)

    def test_anchor_requires_since(self):
        log = os.path.join(self.dir, "anchor2.log")
        open(log, "w").close()
        cfg2 = os.path.join(self.dir, "anchor2.yaml")
        with open(cfg2, "w") as f:
            f.write(f"log_sources:\n  - name: s\n    path: {self.dir}\n"
                    f'    pattern: "anchor2.log"\n')
        r = self.cli("--agent", "--config", cfg2, "--anchor", "123")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--since", r.stderr)

    def test_keep_head_and_truncation_hint(self):
        """痛点#1: 链路头部关键时 --lines 尾部保留会被后面刷屏段吃掉.

        --keep head 保留窗口头部; 超限时 stderr 提示命中/输出条数。
        """
        d = self.dir
        with open(os.path.join(d, "keep.log"), "w") as f:
            for i in range(10):
                f.write(f"[2026-08-27 10:00:{i:02d}] auth step {i}\n")
            for i in range(10, 20):
                f.write(f"[2026-08-27 10:01:{i - 10:02d}] flood padding {i}\n")
        cfg2 = os.path.join(d, "keep.yaml")
        with open(cfg2, "w") as f:
            f.write(f"log_sources:\n  - name: s\n    path: {d}\n"
                    f'    pattern: "keep.log"\nblacklist: []\n')
        base = ["--agent", "--config", cfg2, "--wait", "1", "--since", "8760h"]
        # 默认 tail: --lines 3 只留最后的刷屏段 (登录起点被吃掉)
        r = self.cli(*base, "--lines", "3")
        self.assertNotIn("auth", r.stdout)
        self.assertIn("flood", r.stdout)
        # keep head: 保留窗口头部 (登录认证段)
        r = self.cli(*base, "--lines", "3", "--keep", "head")
        self.assertIn("auth step 0", r.stdout)
        self.assertNotIn("flood", r.stdout)
        # match 场景同样支持: 命中散布, head 留最早命中
        r = self.cli(*base, "--lines", "2", "--keep", "head", "--match", "auth")
        self.assertEqual(r.stdout.strip().splitlines()[0].strip().endswith("auth step 0"), True)
        self.assertNotIn("auth step 5", r.stdout)
        # 截断提示: 窗口 20 行只输出 3 条 -> stderr hint
        r = self.cli(*base, "--lines", "3")
        self.assertIn("hint", r.stderr)
        self.assertIn("20", r.stderr)                              # 窗口总行数

    def test_at_human_time_anchor(self):
        """痛点#2: --at "YYYY-MM-DD HH:MM:SS" 人读时间, 不用手算 epoch."""
        d = self.dir
        with open(os.path.join(d, "at.log"), "w") as f:
            f.write("[2026-08-27 10:00:01] before at\n")
            f.write("[2026-08-27 10:00:03] at point\n")
            f.write("[2026-08-27 10:00:05] after at\n")
        cfg2 = os.path.join(d, "at.yaml")
        with open(cfg2, "w") as f:
            f.write(f"log_sources:\n  - name: s\n    path: {d}\n"
                    f'    pattern: "at.log"\nblacklist: []\n')
        base = ["--agent", "--config", cfg2, "--wait", "1", "--lines", "50"]
        # --at 10:00:03, since 60s: 窗口 [10:00:03-60, 10:00:03] -> 前两行, 第三行被夹掉
        r = self.cli(*base, "--since", "60s", "--at", "2026-08-27 10:00:03")
        self.assertEqual(r.returncode, 0)
        self.assertIn("before at", r.stdout)
        self.assertIn("at point", r.stdout)
        self.assertNotIn("after at", r.stdout)
        # 无效格式 exit 2
        r = self.cli(*base, "--since", "60s", "--at", "not a time")
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("Traceback", r.stderr)
        # --at 与 --anchor 互斥
        r = self.cli(*base, "--since", "60s", "--at", "2026-08-27 10:00:03",
                     "--anchor", "123")
        self.assertEqual(r.returncode, 2)
        # --at 也需 --since
        r = self.cli(*base, "--at", "2026-08-27 10:00:03")
        self.assertEqual(r.returncode, 2)

    def test_allow_bypasses_blacklist(self):
        """痛点#3: --allow 单参数豁免黑名单项, 不用切双 config."""
        d = self.dir
        with open(os.path.join(d, "allow.log"), "w") as f:
            f.write("[2026-08-27 10:00:01] [Debug] ms detail line\n")
            f.write("[2026-08-27 10:00:02] heartbeat spam\n")
            f.write("[2026-08-27 10:00:03] normal line\n")
        cfg2 = os.path.join(d, "allow.yaml")
        with open(cfg2, "w") as f:
            f.write(f"log_sources:\n  - name: s\n    path: {d}\n"
                    f'    pattern: "allow.log"\nblacklist: ["DEBUG", "heartbeat"]\n')
        base = ["--agent", "--config", cfg2, "--wait", "1", "--lines", "50",
                "--since", "8760h"]
        # 默认: DEBUG 黑名单滤掉 [Debug] 行
        r = self.cli(*base)
        self.assertNotIn("ms detail", r.stdout)
        # --allow debug (大小写不敏感): 只豁免 DEBUG, heartbeat 仍滤
        r = self.cli(*base, "--allow", "debug")
        self.assertIn("ms detail", r.stdout)
        self.assertNotIn("heartbeat", r.stdout)
        # 豁免不存在的词 -> stderr 提示 (防 typo 静默无效)
        r = self.cli(*base, "--allow", "no_such_word")
        self.assertIn("hint", r.stderr)
        self.assertIn("no_such_word", r.stderr)

    def test_dynamic_blacklist_family(self):
        """动态黑名单三件套: --blacklist-add / --blacklist-del / --no-blacklist.

        仅影响本次运行、不写回 config; --no-blacklist 与 --source 组合一步到位。
        """
        d = self.dir
        with open(os.path.join(d, "bl.log"), "w") as f:            # 自建夹具, 无顺序依赖
            f.write("[2026-08-27 10:00:01] [Debug] ms detail line\n"
                    "[2026-08-27 10:00:02] heartbeat spam\n"
                    "[2026-08-27 10:00:03] normal line\n")
        cfg2 = os.path.join(d, "bl.yaml")
        with open(cfg2, "w") as f:
            f.write(f"log_sources:\n  - name: s\n    path: {d}\n"
                    f'    pattern: "bl.log"\nblacklist: ["DEBUG", "heartbeat"]\n')
        base = ["--agent", "--config", cfg2, "--wait", "1", "--lines", "50",
                "--since", "8760h"]
        # --blacklist-del DEBUG: 等价 --allow debug (移除指定项, 其余仍滤)
        r = self.cli(*base, "--blacklist-del", "DEBUG")
        self.assertIn("ms detail", r.stdout)
        self.assertNotIn("heartbeat", r.stdout)
        # --no-blacklist: 全清 (heartbeat 也回来)
        r = self.cli(*base, "--no-blacklist")
        self.assertIn("ms detail", r.stdout)
        self.assertIn("heartbeat", r.stdout)
        # --blacklist-add normal: 追加过滤项
        r = self.cli(*base, "--blacklist-add", "normal")
        self.assertNotIn("normal line", r.stdout)
        # --no-blacklist + --blacklist-add: 清空后再加 (只滤新加的)
        r = self.cli(*base, "--no-blacklist", "--blacklist-add", "heartbeat")
        self.assertIn("ms detail", r.stdout)
        self.assertNotIn("heartbeat", r.stdout)
        # --blacklist-del typo 提示
        r = self.cli(*base, "--blacklist-del", "nope_word")
        self.assertIn("hint", r.stderr)
        # 不写回 config: 配置文件内容不变
        with open(cfg2) as f:
            self.assertIn('"DEBUG"', f.read())

    def test_no_blacklist_with_source_combo(self):
        """设计点: --no-blacklist + --source 临时源一次到位 (微服务场景)."""
        d = self.dir
        with open(os.path.join(d, "bl2.log"), "w") as f:            # 自建夹具, 无顺序依赖
            f.write("[2026-08-27 10:00:01] [Debug] ms detail line\n")
        cfg2 = os.path.join(d, "bl2.yaml")
        with open(cfg2, "w") as f:
            f.write(f"log_sources:\n  - name: s\n    path: {d}\n"
                    f'    pattern: "bl2.log"\nblacklist: ["DEBUG"]\n')
        r = self.cli("--agent", "--config", cfg2, "--wait", "1",
                     "--lines", "50", "--since", "8760h",
                     "-s", f"ms:{d}:bl2.log", "--no-blacklist", "--focus", "ms")
        self.assertEqual(r.returncode, 0)
        self.assertIn("ms detail", r.stdout)                       # [Debug] 行放行

    def test_focus_multiple_sources(self):
        """反馈#3: --focus 逗号分隔多源 (clientgate+login 同看一轮搞定)."""
        r = self.A("--focus", "scene,guild")
        self.assertEqual(self.sources_of(r.stdout), {"scene", "guild"})
        r = self.A("--focus", "scene")
        self.assertEqual(self.sources_of(r.stdout), {"scene"})
        # 多源里混入未知名 -> exit 2 (列出可用名)
        r = self.A("--focus", "scene,nope")
        self.assertEqual(r.returncode, 2)
        self.assertIn("guild", r.stderr)

    def test_max_line_len_truncation(self):
        """反馈#1: 大 JSON/proto dump 行(几KB~几十KB)吃掉输出配额 -> --max-line-len 截断."""
        d = self.dir
        huge = "x" * 5000
        with open(os.path.join(d, "huge.log"), "w") as f:
            f.write(f"[2026-08-27 10:00:01] short line\n")
            f.write(f"[2026-08-27 10:00:02] GetCollectionDatas proto dump {huge}\n")
        cfg2 = os.path.join(d, "huge.yaml")
        with open(cfg2, "w") as f:
            f.write(f"log_sources:\n  - name: s\n    path: {d}\n"
                    f'    pattern: "huge.log"\nblacklist: []\n')
        base = ["--agent", "--config", cfg2, "--wait", "1", "--lines", "50",
                "--since", "8760h"]
        # 默认不截断
        r = self.cli(*base)
        self.assertIn(huge[:200], r.stdout)
        # --max-line-len 100: 长行截断 + 标记, 短行不受影响
        r = self.cli(*base, "--max-line-len", "100")
        lines = r.stdout.strip().splitlines()
        self.assertLessEqual(len(lines[1]), 200)               # 截断后远小于 5000
        self.assertIn("截断", lines[1])
        self.assertIn("short line", lines[0])                  # 短行原样
        # json 模式同样截断
        r = self.cli(*base, "--max-line-len", "100", "--json")
        import json as _j
        recs = [_j.loads(l) for l in r.stdout.strip().splitlines()]
        dump = [x for x in recs if "proto" in x["text"]][0]
        self.assertIn("截断", dump["text"])
        self.assertLess(len(dump["text"]), 200)

    def test_enable_source(self):
        """反馈#3: --enable-source login,clientgate 按名启用 config 里的注释态源.

        enabled: false 的源默认不采集; --enable-source 按名启用; typo exit 2。
        """
        d = self.dir
        with open(os.path.join(d, "en.log"), "w") as f:
            f.write("[2026-08-27 10:00:01] enabled source line\n")
        with open(os.path.join(d, "dis.log"), "w") as f:
            f.write("[2026-08-27 10:00:01] disabled source line\n")
        cfg2 = os.path.join(d, "en.yaml")
        with open(cfg2, "w") as f:
            f.write(f"log_sources:\n"
                    f"  - name: alpha\n    path: {d}\n    pattern: \"en.log\"\n"
                    f"  - name: beta\n    path: {d}\n    pattern: \"dis.log\"\n"
                    f"    enabled: false\nblacklist: []\n")
        base = ["--agent", "--config", cfg2, "--wait", "1", "--lines", "50",
                "--since", "8760h"]
        # 默认: enabled:false 源不采集
        r = self.cli(*base)
        self.assertIn("enabled source line", r.stdout)
        self.assertNotIn("disabled source line", r.stdout)
        # --enable-source off: 启用
        r = self.cli(*base, "--enable-source", "beta")
        self.assertIn("disabled source line", r.stdout)
        # --diagnose 只看当前启用的源
        r = self.cli("--diagnose", "--config", cfg2)
        names = [s["source"] for s in json.loads(r.stdout.strip())["sources"]]
        self.assertEqual(names, ["alpha"])
        # typo -> exit 2 列出可用名
        r = self.cli(*base, "--enable-source", "betaa")
        self.assertEqual(r.returncode, 2)
        self.assertIn("beta", r.stderr)

    def test_wait_default_zero(self):
        """P0: agent dump 默认 --wait=0 —— 活水日志下 idle 永不触发, 默认 2s 纯浪费.

        跟随看新行是交互需求; 一次性 dump 拿到 backlog 即返回。
        """
        from logtail.cli import build_parser
        self.assertEqual(build_parser().get_default("wait"), 0.0)

    def test_userid_in_player_preset(self):
        """P2: scene LoginPoint 用 userid: 写法, player 预设必须认."""
        from logtail.correlate import CorrelationKeys
        ck = CorrelationKeys()
        self.assertEqual(ck.extract1("userid:1276679028761 LoginPoint step1", "player"),
                         "1276679028761")
        self.assertEqual(ck.extract1('"userId":"123"', "player"), "123")

    def test_wide_window_slow_hint(self):
        """P1: 宽窗(since>6h)+backlog 慢时 stderr 护栏提示分段探针."""
        d = self.dir
        with open(os.path.join(d, "wide.log"), "w") as f:
            f.write("[2026-08-27 10:00:01] wide window line\n")
        cfg2 = os.path.join(d, "wide.yaml")
        with open(cfg2, "w") as f:
            f.write('log_sources:\n  - name: s\n    dx: "bash -c \'sleep 3 && '
                    f'echo {d}/wide.log\'"\nblacklist: []\n')
        r = self.cli("--agent", "--config", cfg2, "--since", "8760h",
                     "--wait", "0", "--lines", "10")
        self.assertEqual(r.returncode, 0)
        self.assertIn("分段", r.stderr)                         # 宽窗护栏提示

    def test_hard_cap_flag(self):
        """解除限制: --hard-cap 可调 (宽窗用户不再被 30s 上限卡死)."""
        d = self.dir
        with open(os.path.join(d, "hc.log"), "w") as f:
            f.write("[2026-08-27 10:00:01] hc line\n")
        cfg2 = os.path.join(d, "hc.yaml")
        with open(cfg2, "w") as f:
            f.write('log_sources:\n  - name: s\n    dx: "bash -c \'sleep 3 && '
                    f'echo {d}/hc.log\'"\nblacklist: []\n')
        # hard-cap 0.5s < dx 3s -> 未完成警告(快速失败)
        r = self.cli("--agent", "--config", cfg2, "--since", "8760h",
                     "--hard-cap", "0.5")
        self.assertEqual(r.returncode, 0)
        self.assertIn("未完成", r.stderr)
        from logtail.cli import build_parser
        self.assertEqual(build_parser().get_default("hard_cap"), 30.0)

    def test_since_bare_number_is_seconds(self):
        """解除潜规则: --since 90 = 90 秒 (裸数字不再 exit 2)."""
        from logtail.cli import _parse_duration
        self.assertEqual(_parse_duration("90"), 90)
        self.assertEqual(_parse_duration("90s"), 90)
        with self.assertRaises(ValueError):                        # 复合仍拒绝
            _parse_duration("1h30m")

    def test_summary_per_source_backlog(self):
        """反馈#1: --summary 分源报 backlog_ready, 定位"哪个源没读完"."""
        r = self.A("--summary")
        d = json.loads(r.stderr.strip().splitlines()[-1])
        for s in d["sources"]:
            self.assertIn("backlog_ready", s)
            self.assertTrue(s["backlog_ready"])                    # 健康夹具必读完

    def test_summary_backlog_complete_field(self):
        """强建议#2: --summary 必须自报读全性 (backlog_complete)."""
        r = self.A("--summary")
        d = json.loads(r.stderr.strip().splitlines()[-1])
        self.assertIn("backlog_complete", d)
        self.assertTrue(d["backlog_complete"])                     # 正常小夹具必读全

    def test_discover_keys_reports_candidates(self):
        """强建议#3: --discover-keys 采样自报候选关联键的区分度与跨源分布."""
        d = self.dir
        with open(os.path.join(d, "dk.log"), "w") as f:
            f.write("[2026-08-27 10:00:01] player=100 enter s=const&c=1\n"
                    "[2026-08-27 10:00:02] roleId: 100 join s=const&c=1\n"
                    "[2026-08-27 10:00:03] player=200 leave s=const&c=1\n"
                    "[2026-08-27 10:00:04] nothing here\n")
        cfg2 = os.path.join(d, "dk.yaml")
        with open(cfg2, "w") as f:
            f.write(f"log_sources:\n  - name: s\n    path: {d}\n"
                    f'    pattern: "dk.log"\nblacklist: []\n')
        r = self.cli("--agent", "--config", cfg2, "--discover-keys",
                     "--wait", "1", "--since", "8760h")
        self.assertEqual(r.returncode, 0)
        d = json.loads(r.stdout.strip())
        self.assertEqual(d["kind"], "logtail.discover_keys")
        self.assertEqual(d["lines_total"], 4)
        by_name = {k["key"]: k for k in d["keys"]}
        # player: 3/4 行有 key, 2 个不同值, 有区分度
        self.assertEqual(by_name["player"]["lines_with_key"], 3)
        self.assertEqual(by_name["player"]["distinct_values"], 2)
        # session(s=): 3/4 行有但值全相同 -> 无区分度
        self.assertEqual(by_name["session"]["distinct_values"], 1)

    def test_exclude(self):
        r = self.A("--match", "player=100", "--exclude", "crash")
        self.assertEqual(len(r.stdout.strip().splitlines()), 1)

    def test_level(self):
        r = self.A("--level", "ERROR", "--count")
        self.assertEqual(r.stdout.strip(), "2")

    def test_count(self):
        r = self.A("--match", "player=100", "--count")
        self.assertEqual(r.stdout.strip(), "2")

    def test_context_global_mixed_sources(self):
        r = self.A("--match", "crash", "-C", "1")
        self.assertIn("scene", self.sources_of(r.stdout))
        self.assertIn("guild", self.sources_of(r.stdout))

    def test_ctx_same_single_source(self):
        r = self.A("--match", "crash", "--ctx-same", "2")
        self.assertEqual(self.sources_of(r.stdout), {"scene"})

    def test_focus(self):
        r = self.A("--focus", "guild")
        self.assertEqual(self.sources_of(r.stdout), {"guild"})

    def test_trace(self):
        r = self.A("--trace", "guild")
        # 正文含 "guild" 的行: join guild / guild save fail
        self.assertEqual(len(r.stdout.strip().splitlines()), 2)

    def test_correlate_cross_writes(self):
        r = self.A("--correlate", "player=100")
        # scene: enter + crash; guild: roleId join + Guid fail
        self.assertEqual(len(r.stdout.strip().splitlines()), 4)
        self.assertEqual(self.sources_of(r.stdout), {"scene", "guild"})

    def test_correlate_unknown_key_literal(self):
        r = self.A("--correlate", "notakey=100")
        self.assertEqual(len(r.stdout.strip().splitlines()), 4)

    def test_json_ndjson(self):
        r = self.A("--match", "crash", "--json")
        rec = json.loads(r.stdout.strip())
        self.assertEqual(rec["level"], "ERROR")
        self.assertEqual(set(rec), {"ts", "ts_seconds", "source", "level", "text", "seq"})

    def test_summary_stderr_json(self):
        r = self.A("--summary")
        self.assertEqual(r.returncode, 0)
        d = json.loads(r.stderr.strip().splitlines()[-1])
        self.assertEqual(d["kind"], "logtail.summary")
        self.assertEqual(d["total_files"], 2)
        self.assertIn("latest_ts", d)

    def test_diagnose_contract(self):
        r = self.cli("--diagnose", "--config", self.cfg)
        self.assertEqual(r.returncode, 0)
        d = json.loads(r.stdout.strip())
        self.assertEqual(d["kind"], "logtail.diagnose")
        self.assertEqual(d["total_files"], 2)
        self.assertTrue(all(s["discovered"] for s in d["sources"]))

    def test_diagnose_dx_failure_visible(self):
        cfg2 = os.path.join(self.dir, "bad.yaml")
        with open(cfg2, "w") as f:
            f.write('log_sources:\n  - name: bad\n    dx: "false"\n')
        r = self.cli("--diagnose", "--config", cfg2)
        d = json.loads(r.stdout.strip())
        self.assertFalse(d["sources"][0]["discovered"])
        self.assertTrue(d["sources"][0]["dx_error"])

    def test_source_cli_append(self):
        r = self.cli("--agent", "--config", self.cfg, "-s",
                     f"extra:{self.dir}:scene*.log", "--wait", "1",
                     "--lines", "50", "--since", "8760h")
        self.assertIn("extra", r.stdout)

    def test_date_placeholder(self):
        r = self.cli("--agent", "--config", self.cfg, "--date", "2026-08-26",
                     "-s", f"d:{self.dir}:no_such_file_*.log",
                     "--wait", "1", "--lines", "5")
        # 无匹配文件也不崩, exit 0
        self.assertEqual(r.returncode, 0)

    def test_date_warns_for_dx_source_without_placeholder(self):
        """坑4回归: --date 对不含 {date} 的 dx 源无效, 必须向 stderr 明示."""
        cfg2 = os.path.join(self.dir, "dx.yaml")
        with open(cfg2, "w") as f:
            f.write('log_sources:\n  - name: s\n    dx: "echo /tmp/x.log"\n')
        r = self.cli("--agent", "--config", cfg2, "--date", "2026-08-26",
                     "--wait", "0.5", "--lines", "5")
        self.assertEqual(r.returncode, 0)
        self.assertIn("--date", r.stderr)
        self.assertIn("dx", r.stderr)

    def test_since_cap_warns_on_large_file(self):
        """坑2回归(兜底路径): 二分失败(无时间戳行)时退化为尾部 8MB 扫描, 必须明示."""
        big = os.path.join(self.dir, "big.log")
        tail_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 60))
        with open(big, "w") as f:
            # ~9MB 无时间戳垃圾行 -> 二分探针必然失败 -> 走 8MB 尾扫兜底
            old = "no timestamp garbage padding line aaaaaaaaaa\n"
            f.write(old * (9 * 1024 * 1024 // len(old.encode()) + 1))
            f.write(f"[{tail_ts}] recent tail one\n")           # 动态时间: 免日期依赖
            f.write(f"[{tail_ts}] recent tail two\n")
        cfg2 = os.path.join(self.dir, "big.yaml")
        with open(cfg2, "w") as f:
            f.write(f"log_sources:\n  - name: big\n    path: {self.dir}\n"
                    f'    pattern: "big.log"\nblacklist: []\n')
        r = self.cli("--agent", "--config", cfg2, "--since", "1h",
                     "--wait", "1.5", "--lines", "100")
        self.assertEqual(r.returncode, 0)
        self.assertIn("8MB", r.stderr)                               # 兜底触顶警告
        self.assertIn("recent tail", r.stdout)                       # 尾部新行仍在

    def test_since_binary_covers_window_beyond_8mb(self):
        """二分定位回归: >8MB 文件上, --since 窗口起点落在 8MB 尾巴之外时也必须读到."""
        import time as _t
        now = _t.time()
        big = os.path.join(self.dir, "bin.log")
        early_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 7000))
        late_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 100))
        with open(big, "w") as f:
            # 先 1MB "早窗口"行(ts=now-7000s, 在 since=2h 窗口内但超出 8MB 尾巴),
            # 再 8.5MB 近期填充行(ts=now-100s)
            early = f"[{early_ts}] earlyline marker\n"
            f.write(early * (1024 * 1024 // len(early.encode()) + 1))
            late = f"[{late_ts}] latefill padding\n"
            f.write(late * (int(8.5 * 1024 * 1024) // len(late.encode()) + 1))
        cfg2 = os.path.join(self.dir, "bin.yaml")
        with open(cfg2, "w") as f:
            f.write(f"log_sources:\n  - name: big\n    path: {self.dir}\n"
                    f'    pattern: "bin.log"\nblacklist: []\n')
        r = self.cli("--agent", "--config", cfg2, "--since", "2h",
                     "--wait", "3", "--lines", "100000")
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("8MB", r.stderr)                            # 二分成功不打触顶警告
        # 旧行为(8MB 尾扫): early 区在尾巴之外 -> 漏; 二分定位: 必须命中
        # (--lines 是输出上限会截掉窗口头部, 故用 --count 断言真实读取覆盖)
        n = self.cli("--agent", "--config", cfg2, "--since", "2h",
                     "--wait", "3", "--match", "earlyline", "--count")
        self.assertGreater(int(n.stdout.strip()), 0)
        m = self.cli("--agent", "--config", cfg2, "--since", "2h",
                     "--wait", "3", "--match", "latefill", "--count")
        self.assertGreater(int(m.stdout.strip()), 0)

    def test_dump_waits_for_backlog_with_slow_source(self):
        """BUG0001 回归: 慢源(dx 要 2s)下, 默认 --wait(2s) 不得漏掉窗口历史行.

        dump 必须"读到历史窗口全部消费完"再返回(信号驱动), --wait 只管
        backlog 完成后的实时跟随期; 旧行为固定 deadline 在数据到达前就退出。
        """
        import time as _t
        now = _t.time()
        log = os.path.join(self.dir, "slow.log")
        early_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 3000))
        late_ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 10))
        with open(log, "w") as f:
            for i in range(50):
                f.write(f"[{early_ts}] walk marker {i}\n")
            for i in range(50):
                f.write(f"[{late_ts}] late padding {i}\n")
        # dx 命令 sleep 2 再吐路径: 模拟慢发现源 (首条数据 ~2.5s 才到)
        cfg2 = os.path.join(self.dir, "slow.yaml")
        with open(cfg2, "w") as f:
            f.write('log_sources:\n  - name: s\n    dx: "bash -c \'sleep 2 && '
                    f'echo {log}\'"\nblacklist: []\n')
        # 默认 --wait(2s), 不允许用户自己加大: 工具必须等到 backlog 读完
        r = self.cli("--agent", "--config", cfg2, "--since", "1h",
                     "--match", "walk", "--count")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(int(r.stdout.strip()), 50,
                         f"慢源下漏读窗口历史行: {r.stdout!r} / {r.stderr[:300]!r}")


class TestMonitor(CliCase):
    def monitor(self, *extra, secs=3):
        """启动 monitor, secs 后 SIGINT, 返回 CompletedProcess."""
        p = subprocess.Popen(
            [sys.executable, "-m", "logtail", "--agent", "--mode", "monitor",
             "--config", self.cfg, "--lines", "50", *extra],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=ROOT, env=ENV)
        time.sleep(secs)
        p.send_signal(signal.SIGINT)
        try:
            out, err = p.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
            out, err = p.communicate()
            self.fail(f"monitor 未响应 SIGINT: {err[:200]}")
        return p.returncode, out, err

    def test_monitor_streams_filtered(self):
        rc, out, err = self.monitor("--match", "player=100")
        self.assertEqual(rc, 0)
        self.assertEqual(len(out.strip().splitlines()), 2)
        self.assertNotIn("Traceback", err)

    def test_monitor_correlate(self):
        rc, out, err = self.monitor("--correlate", "player=100")
        self.assertEqual(rc, 0)
        self.assertEqual(len(out.strip().splitlines()), 4)

    def test_monitor_exits_cleanly_on_broken_pipe(self):
        """head 提前关闭管道不应打 traceback (README 承诺可管道)."""
        cmd = (f"{sys.executable} -m logtail --agent --mode monitor "
               f"--config {self.cfg} --lines 50 | head -1")
        r = subprocess.run(["bash", "-c", cmd], capture_output=True,
                           text=True, timeout=15, cwd=ROOT, env=ENV)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(len(r.stdout.strip().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
