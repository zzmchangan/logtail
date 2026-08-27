"""数据模型与颜色池.

本模块只定义纯数据结构与颜色工具,不涉及任何输入输出。
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class SourceConfig:
    """一个日志源的配置."""

    name: str        # 输出前缀,用于标识进程/模块
    path: str        # 目录路径 (可含 {date} 占位符)
    pattern: str     # glob 匹配模式,如 "*.log" (可含 {date} 占位符)
    dx: str = ""     # 可选: 用 `dx <cmd>` 求真实文件路径, 优先于 glob
    enabled: bool = True   # False: 默认不采集, --enable-source <名> 按名启用


@dataclass
class LogLine:
    """一条待输出的日志行.

    ts_key: 排序用的时间戳数值,格式为 (epoch 秒数, 微秒)。
            优先用日志自带时间戳;解析不到时退化为到达时的近似值。
    time_str: 供前缀显示的时间戳字符串 (若原行有空出), 可能为空串。
    text: 正文; 若行首有时间戳则已被剥离, 避免与前缀重复。
    seq:    全局自增单调序列号,用于排序的稳定 tie-breaker。
    """

    source: str
    text: str
    ts_key: Tuple[float, int]
    seq: int
    time_str: str = ""
    level: str = ""                     # TRACE/DEBUG/INFO/WARN/ERROR/FATAL, 无则 ""

    @property
    def ts_seconds(self) -> float:
        return self.ts_key[0] + self.ts_key[1] / 1_000_000.0


# ---------------------------------------------------------------------------
# 颜色池: 为多个高亮词公平分配不同颜色
# ---------------------------------------------------------------------------

# 调色板: 只用前景色区分高亮词,背景固定为默认。索引即分配到的 "颜色 id"。
# 实际的前景色名由 TUI 层( curses.init_pair )映射到终端颜色属性;
# 本模块保持纯数据,不 import curses,以便无终端也能测试。
PALETTE_FG_COLORS = [
    "red",
    "green",
    "yellow",
    "cyan",
    "magenta",
    "blue",
    "bright_red",
    "bright_green",
    "bright_yellow",
    "bright_cyan",
    "bright_magenta",
    "bright_blue",
]


class ColorPool:
    """按序轮转分配颜色 id,使多个高亮词各得其色.

    color_for(keyword_id) 返回一个稳定的 [0, len(PALETTE)) 索引,
    供 TUI 层解析成 curses 颜色对。同一关键词永远拿到同一颜色。
    """

    def __init__(self) -> None:
        self._cycle = itertools.cycle(range(len(PALETTE_FG_COLORS)))
        self._assigned: dict[int, int] = {}   # keyword_id -> palette index

    def color_for(self, keyword_id: int) -> int:
        """返回该关键词对应的颜色 id(调色板索引);未分配则分配一个新的."""
        if keyword_id not in self._assigned:
            self._assigned[keyword_id] = next(self._cycle)
        return self._assigned[keyword_id]

    def color_name(self, keyword_id: int) -> str:
        """返回该关键词对应的前景色名称(用于 /list 展示)."""
        return PALETTE_FG_COLORS[self.color_for(keyword_id) % len(PALETTE_FG_COLORS)]


# 弱化显示用的属性 (在上下文模式窗口外行), 由 TUI 决定具体样式。
DIM_ATTR = "dim"


# ---------------------------------------------------------------------------
# 输出行格式化
# ---------------------------------------------------------------------------

def fmt_hhmmss(seconds: float) -> str:
    """把 epoch 秒数格式化为本地 [HH:MM:SS.mmm].

    用本地时区换算, 使前缀时钟与日志自带时间戳的墙上时间一致
    (否则 epoch % 24h 会得到 UTC 时间, 与本地偏 8 小时)。
    """
    lt = time.localtime(seconds)
    ms = int(round((seconds - int(seconds)) * 1000))
    ms = min(ms, 999)
    return f"[{lt.tm_hour:02d}:{lt.tm_min:02d}:{lt.tm_sec:02d}.{ms:03d}]"
