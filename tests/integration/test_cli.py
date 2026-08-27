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
                        "--since", "24h", *extra)

    def sources_of(self, out):
        return {l.split("] ")[1].split()[0] for l in out.strip().splitlines() if l}


class TestOutputContract(CliCase):
    def test_help_stdout_exit0(self):
        r = self.cli("--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("--correlate", r.stdout)
        self.assertEqual(r.stderr, "")

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
                     "--lines", "50", "--since", "24h")
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
        """坑2回归: 文件超过 SINCE_CAP(8MB) 时 --since 窗口可能截断, 必须明示."""
        import logtail.reader as rdr
        big = os.path.join(self.dir, "big.log")
        with open(big, "w") as f:
            # ~9MB 旧行 (远超 8MB cap), 最后两行是"最近"时间戳
            old = "[2026-08-27 00:00:00] old padding line aaaaaaaaaa\n"
            f.write(old * (9 * 1024 * 1024 // len(old.encode()) + 1))
            f.write("[2026-08-27 23:59:59] recent tail one\n")
            f.write("[2026-08-27 23:59:59] recent tail two\n")
        cfg2 = os.path.join(self.dir, "big.yaml")
        with open(cfg2, "w") as f:
            f.write(f"log_sources:\n  - name: big\n    path: {self.dir}\n"
                    f'    pattern: "big.log"\nblacklist: []\n')
        r = self.cli("--agent", "--config", cfg2, "--since", "1h",
                     "--wait", "1.5", "--lines", "100")
        self.assertEqual(r.returncode, 0)
        self.assertIn("8MB", r.stderr)                               # 触顶警告
        self.assertIn("recent tail", r.stdout)                       # 尾部新行仍在


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
