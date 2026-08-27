"""日志跟随器: 等价 tail -F 的多文件后台跟踪.

为每个日志源启动一个后台线程, 周期性 glob 目录下匹配 pattern 的文件,
读取自上次 offset 起的增量行, 推入线程安全的队列。主线程不阻塞。

轮转处理:
 - 文件截断 (size < offset)   -> offset 归 0, 从头读
 - 文件被新文件替换 (inode 变化) -> 新 inode 从头读, 旧 inode 关闭
 - glob 不再匹配且 inode 失效 -> 从跟踪表移除, 不再重复读
"""

from __future__ import annotations

import glob
import itertools
import os
import shlex
import subprocess
import threading
import time
from typing import Dict, List, Optional, Set

from .models import LogLine, SourceConfig
from .timeparse import extract_timestamp
from .levelparse import parse_level

POLL_INTERVAL = 0.2          # 轮询间隔 (秒)
SINCE_CAP = 8_000_000        # --since 回读上限 (8MB, 足够覆盖启动窗口而不过度耗内存)


class _FileFollower:
    """单个文件的跟踪状态."""

    __slots__ = ("path", "inode", "offset", "closed")

    def __init__(self, path: str, inode: int, offset: int = 0) -> None:
        self.path = path
        self.inode = inode
        self.offset = offset
        self.closed = False


class _SourceWorker(threading.Thread):
    """一个日志源的后台读取线程."""

    def __init__(self, src: SourceConfig, owner: "LogFollower") -> None:
        super().__init__(daemon=True, name=f"reader:{src.name}")
        self.src = src
        self._owner = owner
        self._files: Dict[str, _FileFollower] = {}
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.wait(POLL_INTERVAL):
            try:
                self._scan_and_read()
            except Exception:
                # 单个源的异常不能让整个线程死掉
                pass

    # ------------------------------------------------------------------
    def _current_paths(self) -> List[str]:
        """当前应跟踪的具体文件路径列表.

        若配置了 dx 命令, 则运行它拿返回的文件路径 (每行一个);
        否则按 glob 匹配 path/pattern。
        """
        if self.src.dx:
            return self._dx_paths()
        pattern = os.path.join(self.src.path, self.src.pattern)
        return glob.glob(pattern)

    def _dx_paths(self) -> List[str]:
        try:
            out = subprocess.run(shlex.split(self.src.dx), capture_output=True,
                                 text=True, check=False, timeout=10)
        except OSError as exc:
            # dx 命令找不到等情况: 静默, 下轮重试 (dir 可能尚未就绪)
            return []
        except subprocess.TimeoutExpired:
            return []
        if out.returncode != 0:
            return []
        return [p.strip() for p in out.stdout.splitlines() if p.strip()]

    def _scan_and_read(self) -> None:
        for path in self._current_paths():
            self._read_file(path)

        found = set(self._current_paths())
        # 清理已不存在 / 不再匹配的文件 (轮转后旧文件)
        for path, st in list(self._files.items()):
            if path in found:
                continue
            if not os.path.exists(path):
                st.closed = True
                del self._files[path]

    def _read_file(self, path: str) -> None:
        try:
            st = os.stat(path)
        except OSError:
            return
        inode, size = st.st_ino, st.st_size

        existing = self._files.get(path)
        if existing is None:
            # 新文件: 默认从末尾开始跟踪; --since 按时间戳定位窗口起点, --history 按行数。
            if self._owner.since > 0:
                start = self._since_start(path, size)
            elif self._owner.history:
                start = self._history_start(path, size)
            else:
                start = size
            self._files[path] = _FileFollower(path, inode, start)
            return

        if existing.inode != inode:
            # 同一路径被新文件替换 (rename+新文件 / copytruncate): 重新开读
            existing.closed = True
            self._files[path] = _FileFollower(path, inode, 0)
            return

        if size < existing.offset:
            # 截断 (size < offset): 从头读
            existing.offset = 0

        if size > existing.offset:
            self._emit(path, existing, inode, size)

    def _history_start(self, path: str, size: int) -> int:
        """返回文件开头到 '末 N 行' 起始处之间的字节数, 作为 offset.

        --history N 时, 让新文件从该偏移开始读取, 这样只回溯最近 N 行。
        """
        n = self._owner.history
        if n <= 0 or size == 0:
            return 0
        cap = min(size, 1_000_000)          # 最多回读 1MB, 足够覆盖调试时的 N
        read_start = size - cap
        try:
            with open(path, "rb") as fh:
                fh.seek(read_start)
                data = fh.read(cap)
        except OSError:
            return read_start
        # data 的起点在 read_start, 统计其后的换行数
        nl = data.count(b"\n")
        if nl <= n:
            # 尾块里的行数不足 N: 小文件时 read_start==0 会读全文件;
            # 大文件时退化为从尾块起点开始 (最佳努力)
            return read_start
        # 需要找到起点的字节偏移: 从尾块开头数到第 (nl - n) 个 '\n' 之后
        target = nl - n
        idx = -1
        for _ in range(target):
            idx = data.find(b"\n", idx + 1)
        return read_start + idx + 1

    def _since_start(self, path: str, size: int) -> int:
        """返回 '时间戳 >= 最近 since 秒' 第一条日志的字节偏移, 作为 offset.

        --since 5m 时用于排查服务器启动报错: 日志文件已很大, 想直接回看到
        "最近 5 分钟"那一段。从文件末尾向前扫, 找第一条时间戳落在窗口内的行。
        受回读上限 (SINCE_CAP) 约束; 超限或窗内有行无法解析时退化为从头/末尾。
        """
        secs = self._owner.since
        if secs <= 0 or size == 0:
            return 0
        cutoff = time.time() - secs
        cap = min(size, SINCE_CAP)
        read_start = size - cap
        try:
            with open(path, "rb") as fh:
                fh.seek(read_start)
                data = fh.read(cap)
        except OSError:
            # 读不到就保守: 小文件从头, 大文件从末尾 (仅看新增)
            return read_start if read_start == 0 else size

        # 逐条解析时间戳; 返回第一条 (时间戳 >= cutoff) 行的字节偏移
        offset = read_start
        for raw in data.split(b"\n"):
            if not raw:
                offset += 1
                continue
            line = raw.decode("utf-8", errors="replace")
            hit = extract_timestamp(line)
            if hit is not None and hit[0][0] >= cutoff:
                return offset          # 第一条落在窗口内的行, 从这里开始回看
            offset += len(raw) + 1
        # 整个回读块都在窗口内 (或时间戳均早于 cutoff): 仅看到新增行, 从头回读
        return read_start

    def _emit(self, path: str, st: _FileFollower, inode: int, size: int) -> None:
        try:
            with open(path, "rb") as fh:
                fh.seek(st.offset)
                data = fh.read()
        except OSError:
            return
        if not data:
            return

        # 全程按字节处理, 保证 offset 与文件字节位置一致 (日志含 UTF-8 中文时,
        # 字符数 ≠ 字节数, 若用 len(解码串) 推进 offset 会重复读块导致乱码).
        newline = data.endswith(b"\n")
        if newline:
            chunks = data.split(b"\n")[:-1]         # 去掉末尾空串
            consumed = len(data)
        else:
            chunks = data.split(b"\n")
            tail = chunks.pop() if chunks else b""   # 不完整行留在文件, 下次拼接
            consumed = len(data) - len(tail)
        st.offset += consumed

        for raw in chunks:
            raw = raw.rstrip(b"\r")
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace")
            hit = extract_timestamp(line)
            if hit is not None:
                ts_key, start, end = hit
                time_str = line[start:end]        # 原样保留时间戳文本 (含括号)
                body = line[end:].lstrip(" ")
            else:
                ts_key, time_str, body = (time.time(), 0), "", line
            level = parse_level(line)
            self._owner.put_log(source=self.src.name, text=body,
                                ts_key=ts_key, time_str=time_str, level=level)


class LogFollower:
    """管理全部后台读取线程, 暴露 reset() 供 /reset 使用."""

    def __init__(self, sources: List[SourceConfig], history: int = 0,
                 since: float = 0.0) -> None:
        self.sources = sources
        self.history = history               # >0 表示启动时回溯末 N 行
        self.since = since                   # >0 表示按时间戳回看最近 N 秒
        self.queue = _UnboundedQueue()
        self._seq = itertools.count(1)
        self._lock = threading.Lock()
        self._workers: List[_SourceWorker] = []

    def next_seq(self) -> int:
        with self._lock:
            return next(self._seq)

    def put_log(self, source: str, text: str, ts_key, time_str: str = "",
                level: str = "") -> None:
        seq = self.next_seq()
        self.queue.put(LogLine(source=source, text=text, ts_key=ts_key,
                               seq=seq, time_str=time_str, level=level))

    def start(self) -> None:
        for src in self.sources:
            w = _SourceWorker(src, self)
            w.start()
            self._workers.append(w)

    def stop(self) -> None:
        for w in self._workers:
            w.stop()
        for w in self._workers:
            w.join(timeout=2.0)
        self._workers.clear()

    def reset(self) -> None:
        """丢弃当前读取偏移; worker 下一轮会重新 glob 并定位到文件末尾."""
        for w in self._workers:
            w._files.clear()


class _UnboundedQueue:
    """线程安全的无界队列 (list + Condition 实现)."""

    def __init__(self) -> None:
        self._items = []
        self._cond = threading.Condition()

    def put(self, item: LogLine) -> None:
        with self._cond:
            self._items.append(item)
            self._cond.notify()

    def drain(self) -> List[LogLine]:
        """取出当前全部待处理项."""
        with self._cond:
            items, self._items = self._items, []
            return items

    def clear(self) -> None:
        with self._cond:
            self._items = []

    def __len__(self) -> int:
        with self._cond:
            return len(self._items)
