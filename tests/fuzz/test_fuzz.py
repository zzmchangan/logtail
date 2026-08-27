"""模糊测试: 随机输入下不崩溃 + 不变量成立.

全部使用固定种子, 可复现; 不追求覆盖语义, 只守两条底线:
 1) 任何随机输入不让进程崩溃/吐 traceback (退出码只允许 0/2);
 2) 关键不变量: normalize 幂等、裸词匹配 iff 子串、行数守恒、时间戳解析不炸。
"""

import io
import json
import os
import random
import string
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout

from logtail.agent import dump
from logtail.config import Config, ConfigError, load_config
from logtail.correlate import CorrelationKeys, normalize
from logtail.models import SourceConfig
from logtail.reader import LogFollower
from logtail.rules import Rule, RulePatternError, RuleSet
from logtail.timeparse import extract_timestamp

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SEED = 20260827

GARBAGE_CHARS = (
    string.printable + "中文日本語🎮🎉ØßÑ" + "\x00\x01\x1b[31m" + chr(0) + chr(127)
)


def rand_text(rng, n):
    return "".join(rng.choice(GARBAGE_CHARS) for _ in range(rng.randint(0, n)))


def rand_line(rng):
    """随机日志行: 混合有效/垃圾/超长/CRLF/带时间戳."""
    kind = rng.random()
    if kind < 0.2:      # 有效时间戳行
        return ("[2026-08-27 10:%02d:%02d.%06d] %s"
                % (rng.randint(0, 59), rng.randint(0, 59), rng.randint(0, 999999),
                   rand_text(rng, 80)))
    if kind < 0.3:      # 超长行
        return rand_text(rng, 10000)
    if kind < 0.4:      # CRLF
        return rand_text(rng, 50) + "\r"
    if kind < 0.5:      # 看起来像时间戳的垃圾
        return "[10:99:99] [not-a-ts"
    return rand_text(rng, 200)


class TestTimeparseFuzz(unittest.TestCase):
    def test_never_raises_valid_shape(self):
        rng = random.Random(SEED)
        for _ in range(5000):
            s = rand_text(rng, 300)
            try:
                hit = extract_timestamp(s)
            except Exception as e:                                  # pragma: no cover
                self.fail(f"extract_timestamp({s!r}) 抛了 {e!r}")
            if hit is not None:
                (sec, us), start, end = hit
                self.assertIsInstance(sec, float)
                self.assertTrue(0 <= start < end <= len(s))


class TestRulesFuzz(unittest.TestCase):
    def test_bare_word_iff_substring(self):
        rng = random.Random(SEED + 1)
        for _ in range(2000):
            w = rand_text(rng, 10).strip() or "x"
            t = rand_text(rng, 100)
            r = Rule(1, "highlight", w)
            self.assertEqual(r.matches(t), w.lower() in t.lower(),
                             f"裸词语义破坏: {w!r} vs {t!r}")

    def test_regex_never_raises_after_compile(self):
        rng = random.Random(SEED + 2)
        pieces = ["(", ")", "[", "]", "*", "+", "?", "a", "\\d", "re:", "|", "{2,}"]
        texts = [rand_text(rng, 60) for _ in range(50)]
        compiled = 0
        for _ in range(2000):
            pat = "re:" + "".join(rng.choice(pieces) for _ in range(rng.randint(1, 8)))
            try:
                r = Rule(1, "highlight", pat)
            except RulePatternError:
                continue                                             # 干净报错: 合法结局
            compiled += 1
            for t in texts:
                r.matches(t)                                        # 只要不抛就行
        self.assertGreater(compiled, 100)                           # 确认真的编译了一些

    def test_ruleset_random_ops(self):
        rng = random.Random(SEED + 3)
        rs = RuleSet()
        alive = set()
        for _ in range(3000):
            op = rng.random()
            w = rand_text(rng, 8).strip() or "x"
            try:
                if op < 0.4:
                    rs.add(w, "highlight")
                    alive.add(w)
                elif op < 0.6 and alive:
                    w = rng.choice(sorted(alive))
                    rs.remove(w, "highlight")
                    alive.discard(w)
                elif op < 0.7:
                    rs.set_level_filter(rng.choice(["TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"]))
                else:
                    t = rand_text(rng, 50)
                    rs.blocked(t)
                    rs.highlights(t)
            except RulePatternError:
                pass
            # 不变量: 高亮规则集合与本地镜像一致
            self.assertEqual({r.pattern for r in rs.list_highlights()}, alive)


class TestCorrelateFuzz(unittest.TestCase):
    def test_normalize_idempotent(self):
        rng = random.Random(SEED + 4)
        for _ in range(3000):
            v = rand_text(rng, 20)
            once = normalize(v)
            self.assertEqual(normalize(once), once)
            self.assertNotIn(" ", once)

    def test_extract_never_raises(self):
        rng = random.Random(SEED + 5)
        ck = CorrelationKeys([
            {"name": "k", "extract": [r"(\d+)", r"[", r"a(b)"]},
        ], presets=False)
        for _ in range(2000):
            ck.extract1(rand_text(rng, 100), "k")


class TestConfigFuzz(unittest.TestCase):
    def test_random_yaml_clean_error(self):
        rng = random.Random(SEED + 6)
        keys = ["log_sources", "blacklist", "keywords", "correlation_keys",
                "garbage", "a: b", "- x", "  indent"]
        for _ in range(300):
            doc = "\n".join(rng.choice(keys) + rand_text(rng, 20)
                            for _ in range(rng.randint(0, 5)))
            fd, p = tempfile.mkstemp(suffix=".yaml")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(doc)
                try:
                    load_config(p)
                except ConfigError:
                    pass                                            # 唯一允许的异常
            finally:
                os.unlink(p)


class TestReaderFuzz(unittest.TestCase):
    def test_random_lines_count_conserved(self):
        """完整行(以\n结尾)在 history 足够大时, 吐出的行数 == 非空行数."""
        rng = random.Random(SEED + 7)
        d = tempfile.mkdtemp(prefix="lt_fuzz_")
        path = os.path.join(d, "f.log")
        lines = [rand_line(rng) for _ in range(200)]
        # 保证以换行结尾且无内嵌换行 (行内随机字符含 \n 会分裂成多行)
        lines = [l.replace("\n", " ").replace("\r\n", " ").replace("\r", " ") or "x"
                 for l in lines]
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        expected = len(lines)                                       # 全部非空
        f = LogFollower([SourceConfig("s", d, "*.log")], history=10 ** 9)
        f.start()
        try:
            got = []
            deadline = time.monotonic() + 5
            while len(got) < expected and time.monotonic() < deadline:
                got.extend(f.queue.drain())
                time.sleep(0.05)
            self.assertEqual(len(got), expected)
        finally:
            f.stop()

    def test_incremental_appends_conserved(self):
        """分批追加: 最终吐出行数 == 全部非空行数 (offset 一致性)."""
        rng = random.Random(SEED + 8)
        d = tempfile.mkdtemp(prefix="lt_fuzz2_")
        path = os.path.join(d, "g.log")
        open(path, "w").close()
        f = LogFollower([SourceConfig("s", d, "*.log")])
        f.start()
        total = 0
        try:
            time.sleep(0.5)                                         # 空文件先被跟踪
            for _ in range(5):
                batch = [rand_line(rng).replace("\n", " ").replace("\r", " ") or "y"
                         for _ in range(rng.randint(1, 20))]
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write("\n".join(batch) + "\n")
                total += len(batch)
                time.sleep(0.5)                                     # 等一个轮询周期
            got = []
            deadline = time.monotonic() + 3
            while len(got) < total and time.monotonic() < deadline:
                got.extend(f.queue.drain())
                time.sleep(0.05)
            self.assertEqual(len(got), total)
        finally:
            f.stop()


class TestAgentFuzz(unittest.TestCase):
    def _cfg(self, d):
        return Config(sources=[SourceConfig("s", d, "*.log")], history=10 ** 9)

    def test_dump_never_crashes_on_garbage(self):
        rng = random.Random(SEED + 9)
        d = tempfile.mkdtemp(prefix="lt_fuzz3_")
        with open(os.path.join(d, "h.log"), "w", encoding="utf-8") as f:
            f.write("\n".join(rand_line(rng).replace("\n", " ") or "z"
                              for _ in range(100)) + "\n")
        cfg = self._cfg(d)
        words = [rand_text(rng, 6).strip() or "q" for _ in range(20)]
        for w in words:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = dump(cfg, w, 50, wait=0.8)
            self.assertEqual(rc, 0)

    def test_count_equals_output_lines(self):
        """不变量: --count 的数字 == 不带 --count 时输出的行数."""
        rng = random.Random(SEED + 10)
        d = tempfile.mkdtemp(prefix="lt_fuzz4_")
        with open(os.path.join(d, "i.log"), "w", encoding="utf-8") as f:
            f.write("\n".join(
                f"[2026-08-27 10:00:{i % 60:02d}] word{i % 7} payload"
                for i in range(60)) + "\n")
        cfg = self._cfg(d)
        for w in [f"word{i}" for i in range(7)]:
            buf = io.StringIO()
            with redirect_stdout(buf):
                dump(cfg, w, 100, wait=1.0, count_only=True)
            n = int(buf.getvalue().strip())
            buf = io.StringIO()
            with redirect_stdout(buf):
                dump(cfg, w, 100, wait=1.0)
            self.assertEqual(n, len(buf.getvalue().strip().splitlines()),
                             f"count 与正文行数不一致: {w}")


class TestCliFuzz(unittest.TestCase):
    """随机 flag 组合: 退出码只允许 0/2, stderr 无 traceback."""

    def test_random_flag_combos(self):
        rng = random.Random(SEED + 11)
        d = tempfile.mkdtemp(prefix="lt_fuzz5_")
        with open(os.path.join(d, "j.log"), "w", encoding="utf-8") as f:
            f.write("\n".join(
                f"[2026-08-27 10:00:{i % 60:02d}] [{rng.choice(['Info', 'Error', 'Warn'])}]"
                f" player={rng.randint(1, 5)} msg{i}"
                for i in range(50)) + "\n")
        cfg = os.path.join(d, "c.yaml")
        with open(cfg, "w") as f:
            f.write(f"log_sources:\n  - name: s\n    path: {d}\n    pattern: \"*.log\"\n"
                    "blacklist: []\nkeywords: []\n"
                    'correlation_keys:\n  - name: player\n    extract: ["player[:=] *(\\\\d+)"]\n')
        base = ["--agent", "--config", cfg, "--wait", "0.5", "--lines", "30",
                "--since", "24h"]
        opts = [
            lambda: ["--match", rng.choice(["player=1", "msg", "zzz", "re:p\\d+"])] if rng.random() < 0.8 else [],
            lambda: ["--level", rng.choice(["DEBUG", "INFO", "WARN", "ERROR"])] if rng.random() < 0.5 else [],
            lambda: ["--json"] if rng.random() < 0.5 else [],
            lambda: ["--count"] if rng.random() < 0.3 else [],
            lambda: ["-C", str(rng.randint(0, 3))] if rng.random() < 0.3 else [],
            lambda: ["--ctx-same", str(rng.randint(0, 3))] if rng.random() < 0.3 else [],
            lambda: ["--correlate", f"player={rng.randint(1, 6)}"] if rng.random() < 0.4 else [],
            lambda: ["--focus", rng.choice(["s", "nope"])] if rng.random() < 0.3 else [],
            lambda: ["--summary"] if rng.random() < 0.4 else [],
            lambda: ["--exclude", rng.choice(["msg", "zzz"])] if rng.random() < 0.3 else [],
        ]
        for i in range(15):
            extra = [a for gen in opts for a in gen()]
            r = subprocess.run([sys.executable, "-m", "logtail", *base, *extra],
                               capture_output=True, text=True, timeout=30,
                               cwd=ROOT, env={**os.environ, "PYTHONPATH": ROOT})
            self.assertIn(r.returncode, (0, 2),
                          f"第 {i} 轮 {extra} 退出码 {r.returncode}")
            self.assertNotIn("Traceback", r.stderr,
                             f"第 {i} 轮 {extra}\n{r.stderr[:500]}")


if __name__ == "__main__":
    unittest.main()
