"""reader 单元测试: tail 语义/历史回溯/since 定位/轮转截断/不完整行/UTF-8/dx/probe.

涉及真实后台线程 (POLL_INTERVAL=0.2s), 用轮询 + 超时收集, 保持确定性.
"""

import os
import tempfile
import time
import unittest

from logtail.models import LogLine, SourceConfig
from logtail.reader import LogFollower, _last_timestamp


def tmpdir() -> str:
    return tempfile.mkdtemp(prefix="lt_reader_")


def write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def append(path: str, text: str) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)


def collect(follower, want=None, timeout=5.0):
    """轮询 drain 直到拿到 want 条或超时."""
    out = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        out.extend(follower.queue.drain())
        if want is not None and len(out) >= want:
            break
        time.sleep(0.05)
    return out


class FollowerCase(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir()
        self.path = os.path.join(self.dir, "a.log")

    def run_follower(self, history=0, since=0.0, pattern="*.log"):
        f = LogFollower([SourceConfig("s", self.dir, pattern)],
                        history=history, since=since)
        return f


class TestTailSemantics(FollowerCase):
    def test_no_history_starts_at_eof(self):
        write(self.path, "old1\nold2\n")
        f = self.run_follower()
        f.start()
        try:
            time.sleep(0.5)                                          # 等首个扫描周期
            self.assertEqual(f.queue.drain(), [])                    # 旧内容不吐
            append(self.path, "new1\n")
            got = collect(f, want=1)
            self.assertEqual([l.text for l in got], ["new1"])
        finally:
            f.stop()

    def test_history_reads_last_n(self):
        write(self.path, "l1\nl2\nl3\nl4\nl5\n")
        f = self.run_follower(history=2)
        f.start()
        try:
            got = collect(f, want=2)
            self.assertEqual([l.text for l in got], ["l4", "l5"])
        finally:
            f.stop()

    def test_history_n_larger_than_file(self):
        write(self.path, "only\n")
        f = self.run_follower(history=100)
        f.start()
        try:
            got = collect(f, want=1)
            self.assertEqual([l.text for l in got], ["only"])
        finally:
            f.stop()

    def test_since_window_only_recent(self):
        now = time.time()
        import datetime
        fmt = "%Y-%m-%d %H:%M:%S"
        lines = [f"[{datetime.datetime.fromtimestamp(now - 3600).strftime(fmt)}] old line",
                 f"[{datetime.datetime.fromtimestamp(now).strftime(fmt)}] recent line"]
        write(self.path, "\n".join(lines) + "\n")
        f = self.run_follower(since=60)
        f.start()
        try:
            got = collect(f, want=1, timeout=3)
            self.assertEqual([l.text for l in got], ["recent line"])
        finally:
            f.stop()

    def test_since_garbage_file_falls_back(self):
        """全部行无时间戳: 二分必然失败 -> 兜底尾扫; 小文件从头读, 全部吐出."""
        write(self.path, "garbage one\ngarbage two\ngarbage three\n")
        f = self.run_follower(since=60)
        f.start()
        try:
            got = collect(f, want=3, timeout=3)
            self.assertEqual([l.text for l in got],
                             ["garbage one", "garbage two", "garbage three"])
        finally:
            f.stop()

    def test_binary_since_offset_unit(self):
        """二分定位的纯函数行为: 各档 cutoff 都落在正确的行首."""
        from logtail.reader import _binary_since_offset
        now = time.time()
        import datetime
        fmt = "%Y-%m-%d %H:%M:%S"
        times = [now - 10000, now - 5000, now - 1000, now - 100]
        with open(self.path, "w") as f:
            for i, t in enumerate(times):
                f.write(f"[{datetime.datetime.fromtimestamp(t).strftime(fmt)}] line{i}\n")
        size = os.path.getsize(self.path)
        offsets = []
        with open(self.path, "rb") as fh:
            for cut in (now - 20000, now - 6000, now - 2000, now - 150):
                offsets.append(_binary_since_offset(fh, size, cut))
        self.assertTrue(all(o is not None for o in offsets))
        # 各 offset 后第一行正文应分别是 line0..line3
        with open(self.path, "rb") as fh:
            texts = []
            for off in offsets:
                fh.seek(off)
                texts.append(fh.readline().decode().split("] ", 1)[1].strip())
        self.assertEqual(texts, ["line0", "line1", "line2", "line3"])
        # cutoff 早于全部 -> offset 0; 晚于全部 -> size (无行合格)
        with open(self.path, "rb") as fh:
            self.assertEqual(_binary_since_offset(fh, size, now - 99999), 0)
            self.assertEqual(_binary_since_offset(fh, size, now + 99999), size)

    def test_appended_lines_stream(self):
        write(self.path, "")
        f = self.run_follower()
        f.start()
        try:
            time.sleep(0.5)
            for i in range(3):
                append(self.path, f"stream{i}\n")
            got = collect(f, want=3)
            self.assertEqual([l.text for l in got], ["stream0", "stream1", "stream2"])
        finally:
            f.stop()


class TestRotation(FollowerCase):
    def test_truncate_rereads(self):
        write(self.path, "aaaa\nbbbb\n")
        f = self.run_follower(history=5)
        f.start()
        try:
            collect(f, want=2)
            write(self.path, "fresh\n")                               # 截断成更小
            got = collect(f, want=1)
            self.assertIn("fresh", [l.text for l in got])
        finally:
            f.stop()

    def test_replace_inode_rereads(self):
        # 真实轮转模式: 先写临时文件再 rename 覆盖 -> 新 inode 有保证。
        # (注: remove+立刻新建可能复用同一 inode, 轮转检测会失效并从旧 offset
        #  中间起读 —— tail 类工具的公认限制, 与 GNU tail 一致, 不在支持范围。)
        write(self.path, "v1\n")
        f = self.run_follower(history=5)
        f.start()
        try:
            collect(f, want=1)
            tmp = os.path.join(self.dir, "rotating.tmp")
            write(tmp, "v2line1\nv2line2\n")
            os.replace(tmp, self.path)                               # 原子轮转
            got = collect(f, want=2)
            texts = [l.text for l in got]
            self.assertIn("v2line1", texts)
            self.assertIn("v2line2", texts)
        finally:
            f.stop()

    def test_removed_file_stops_tracking(self):
        write(self.path, "x\n")
        f = self.run_follower(history=5)
        f.start()
        try:
            collect(f, want=1)
            os.remove(self.path)
            time.sleep(0.6)                                          # 等清理周期
            probe = f.probe()[0]
            self.assertEqual(probe["files"], 0)
        finally:
            f.stop()


class TestLineIntegrity(FollowerCase):
    def test_incomplete_line_held_until_newline(self):
        write(self.path, "")
        f = self.run_follower()
        f.start()
        try:
            time.sleep(0.5)
            append(self.path, "part")                                 # 无换行: 不吐
            time.sleep(0.5)
            self.assertEqual(f.queue.drain(), [])
            append(self.path, "ial\n")                                # 补全: 整行吐
            got = collect(f, want=1)
            self.assertEqual([l.text for l in got], ["partial"])
        finally:
            f.stop()

    def test_utf8_multibyte_no_garbling(self):
        write(self.path, "中文测试一\n中文测试二\nemoji: 🎮\n")
        f = self.run_follower(history=3)
        f.start()
        try:
            got = collect(f, want=3)
            self.assertEqual([l.text for l in got],
                             ["中文测试一", "中文测试二", "emoji: 🎮"])
        finally:
            f.stop()

    def test_crlf_stripped(self):
        write(self.path, "win line\r\n")
        f = self.run_follower(history=1)
        f.start()
        try:
            got = collect(f, want=1)
            self.assertEqual(got[0].text, "win line")
        finally:
            f.stop()

    def test_timestamp_extracted_to_fields(self):
        write(self.path, "[2026-08-27 10:00:00.500000] body here\n")
        f = self.run_follower(history=1)
        f.start()
        try:
            got = collect(f, want=1)
            ln = got[0]
            self.assertEqual(ln.time_str, "[2026-08-27 10:00:00.500000]")
            self.assertEqual(ln.text, "body here")
        finally:
            f.stop()


class TestDxPaths(unittest.TestCase):
    def worker(self, dx):
        f = LogFollower([SourceConfig("s", "", "", dx=dx)])
        from logtail.reader import _SourceWorker
        return _SourceWorker(f.sources[0], f), f

    def test_echo_returns_paths(self):
        w, _ = self.worker("echo /tmp/a.log")
        self.assertEqual(w._current_paths(), ["/tmp/a.log"])

    def test_false_command_records_error(self):
        w, _ = self.worker("false")
        self.assertEqual(w._current_paths(), [])
        self.assertIn("返回码", w._dx_error)

    def test_missing_command_records_error(self):
        w, _ = self.worker("definitely_missing_cmd_xyz")
        self.assertEqual(w._current_paths(), [])
        self.assertTrue(w._dx_error)

    def test_probe_reports_dx_error(self):
        w, f = self.worker("false")
        # 手动跑一次扫描 (不 start 线程); probe() 依赖已 start 的 worker,
        # 这里直接查 worker 自身的诊断
        w._scan_and_read()
        st = w.probe_status()
        self.assertFalse(st["discovered"])
        self.assertTrue(st["dx_error"])


class TestDiagnose(unittest.TestCase):
    def test_diagnose_no_start(self):
        d = tmpdir()
        write(os.path.join(d, "a.log"),
              "[2026-08-27 10:00:00] x\n[2026-08-27 10:00:01] y\n")
        f = LogFollower([SourceConfig("s", d, "*.log")])
        report = f.diagnose()
        self.assertEqual(len(report), 1)
        self.assertEqual(report[0]["files"], 1)
        self.assertTrue(report[0]["discovered"])
        self.assertEqual(report[0]["dx_error"], "")
        self.assertIsNotNone(report[0]["latest_ts"])

    def test_last_timestamp_garbage(self):
        p = os.path.join(tmpdir(), "g.log")
        write(p, "no timestamps here\nat all\n")
        self.assertIsNone(_last_timestamp(p))


if __name__ == "__main__":
    unittest.main()
