"""排序缓冲 + 上下文窗口 + 近线回溯.

reader 的多个 worker 线程把原始行推入无界队列; 主线程周期性 drain,
对本批按 (ts_key, seq) 稳定排序后追加到环形缓冲 (scrollback)。

显示策略由 TUI 决定:
 - 全量模式: 显示环形缓冲中所有通过黑名单的行 (滚动回溯范围 = maxlen)
 - 上下文模式: 只显示含高亮词的行, 连同其前后各 N 行;
   窗口外本行不高亮 (弱化着色)。

高亮匹配结果在 feed() 时一次性算出并缓存, 避免每帧对整窗重跑正则。
内存占用由 maxlen 封顶, 不随运行时间增长。
"""

from __future__ import annotations

import re
from typing import Callable, List, Optional, Tuple

from .models import LogLine
from .rules import Rule

MODE_ALL = "all"
MODE_CONTEXT = "context"
MODE_TRACE = "trace"

# 交互版滚动回溯深度 (环形缓冲上限)。日志量大时若只留 4000 行, 冻结后往上翻
# 几秒就到底, 且高并发灌日志时一个刷屏就让旧行被环淘汰、冻住的内容漂走。
# 20000 在作者实测洪峰下足够回溯一段完整战斗; 代价是每次渲染因此改为
# "只折可见窗口" (见 tui._window), 不再每帧折全环, 否则会卡死。
DEFAULT_SCROLLBACK = 20000


class RingBuffer:
    """定长环形缓冲, 直接维护 (LogLine, hl_rules) 列表, 自动淘汰最旧."""

    def __init__(self, maxlen: int) -> None:
        self.maxlen = maxlen
        self._items: List[Tuple[LogLine, List[Rule]]] = []

    def append(self, line: LogLine, hl_rules: List[Rule]) -> None:
        self._items.append((line, hl_rules))
        if len(self._items) > self.maxlen:
            del self._items[: len(self._items) - self.maxlen]

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self):
        return iter(self._items)

    def tail(self, n: int) -> List[Tuple[LogLine, List[Rule]]]:
        return self._items[-n:]


class Timeline:
    """维护环形缓冲, 负责排序、上下文窗口计算与回溯可见集."""

    def __init__(self, ruleset, maxlen: int = DEFAULT_SCROLLBACK) -> None:
        self.ruleset = ruleset
        self.ring = RingBuffer(maxlen)
        self.mode = MODE_ALL
        self.context_n = 5                       # 上下文窗: 行数 (默认)
        self.context_seconds = 0.0               # >0 时上下文窗按时间 (秒)
        self.trace_term = ""                # 实体 trace: 只显示含该词的行 (无邻居)

    # ------------------------------------------------------------------
    # 数据流入
    # ------------------------------------------------------------------
    def feed(self, lines: List[LogLine]) -> None:
        """把本批已通过黑名单的行排序后写入缓冲.

        lines 为按来源到达的原始行; 这里按 (ts_key, seq) 稳定排序,
        使同一批内跨文件的行按时间戳正确排列。
        """
        if not lines:
            return
        lines.sort(key=lambda l: (l.ts_key, l.seq))
        for ln in lines:
            hl = self.ruleset.highlights(ln.text)
            self.ring.append(ln, hl)

    def clear(self) -> None:
        self.ring.clear()

    def rehash(self) -> None:
        """用当前 ruleset 重新计算缓冲里每行的高亮规则.

        /keyword、/remove、/clr 修改规则后调用, 让已在滚动缓冲里的存量行
        立即重新着色 (否则只有新进入的行才应用新规则)。
        """
        rebuilt: List[Tuple[LogLine, List[Rule]]] = []
        for ln, _old in self.ring._items:
            rebuilt.append((ln, self.ruleset.highlights(ln.text)))
        self.ring._items = rebuilt

    def set_mode(self, mode: str) -> None:
        if mode not in (MODE_ALL, MODE_CONTEXT, MODE_TRACE):
            raise ValueError(f"未知显示模式: {mode!r}")
        self.mode = mode

    def set_context_n(self, n: int) -> None:
        self.context_n = max(0, int(n))
        self.context_seconds = 0.0

    def set_context_seconds(self, secs: float) -> None:
        self.context_seconds = max(0.0, float(secs))
        self.context_n = 0

    def set_context(self, spec: str) -> bool:
        """设置上下文窗; spec 为 'N' (行数) 或 'Ns'/'Nm' (时间). 返回是否成功."""
        s = spec.strip().lower()
        m = re.match(r"^(\d+)(s|m)?$", s)
        if not m:
            return False
        val = int(m.group(1))
        unit = m.group(2)
        if unit == "s":
            self.set_context_seconds(val)
        elif unit == "m":
            self.set_context_seconds(val * 60)
        else:
            self.set_context_n(val)
        return True

    # ------------------------------------------------------------------
    # 可见集计算
    # ------------------------------------------------------------------
    def visible(self) -> List[Tuple[LogLine, List[Rule], bool]]:
        """返回当前应显示的行, 每项为 (line, hl_rules, dim).

        dim=True 表示该行在上下文模式下因处于高亮行邻域而显示, 但其本身
        不含高亮词 (应弱化着色)。
        """
        n = len(self.ring)
        if n == 0:
            return []

        if self.mode == MODE_ALL:
            return [(ln, hl, False) for ln, hl in self.ring]

        # trace 模式: 只显示含 trace_term 的行, 无邻居 (纯净追踪某实体)
        if self.mode == MODE_TRACE and self.trace_term:
            term = self.trace_term
            return [(ln, hl, False) for ln, hl in self.ring
                    if term.lower() in ln.text.lower()]

        # 上下文模式: 找出含高亮词的行, 展开为前后 N 行 (或按上下文时间窗)
        ctx = self.context_n
        ctx_secs = self.context_seconds
        show: List[Tuple[LogLine, List[Rule], bool]] = []
        added: set = set()

        for i, (line, hl) in enumerate(self.ring._items):
            if not hl:
                continue
            if ctx_secs > 0:
                lo = _time_window_lo(self.ring, i, ctx_secs)
                hi = _time_window_hi(self.ring, i, ctx_secs)
            else:
                lo, hi = max(0, i - ctx), min(n, i + ctx + 1)
            for j in range(lo, hi):
                if j in added:
                    continue
                added.add(j)
                lj, hlj = self.ring._items[j]
                dim = not hlj
                show.append((lj, hlj, dim))
        show.sort(key=lambda t: (t[0].ts_key, t[0].seq))
        return show

    def search(self, matcher, start_idx: int, direction: int) -> int:
        """从 start_idx 的下一条起沿方向搜环形缓冲, 返回匹配行索引, 无则 -1.

        matcher 可为 Rule (含 .matches) 或可调用(str)->bool; 循环搜索 (越界回绕)。
        direction > 0 向下 (往新), < 0 向上 (往回)。
        """
        n = len(self.ring._items)
        if n == 0:
            return -1
        match = matcher.matches if hasattr(matcher, "matches") else matcher
        step = 1 if direction >= 0 else -1
        i = (start_idx + step) % n
        for _ in range(n):
            if match(self.ring._items[i][0].text):
                return i
            i = (i + step) % n
        return -1


def _time_window_lo(ring: "RingBuffer", i: int, secs: float) -> int:
    """返回环形缓冲中, 与第 i 行时间戳相差 >= secs 秒的前边界索引."""
    t = ring._items[i][0].ts_seconds
    lo = i
    while lo > 0 and (t - ring._items[lo - 1][0].ts_seconds) <= secs:
        lo -= 1
    return lo


def _time_window_hi(ring: "RingBuffer", i: int, secs: float) -> int:
    """返回环形缓冲中, 与第 i 行时间戳相差 >= secs 秒的后边界索引 (不含)."""
    n = len(ring._items)
    t = ring._items[i][0].ts_seconds
    hi = i + 1
    while hi < n and (ring._items[hi][0].ts_seconds - t) <= secs:
        hi += 1
    return hi
