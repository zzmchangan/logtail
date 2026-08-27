"""轻量时间戳解析器.

只关心能否解析出可用于排序的数值,不解析复杂日志结构。

返回绝对 epoch 秒数 (微秒以整数返回, 作为排序的细化部分), 与 reader 中
"无时间戳则退化为到达时刻 time.time()" 处于同一数值轴, 保证混排正确。

格式 (优先级顺序, 从行首向后):
 1. [YYYY-MM-DD HH:MM:SS(.ms)]  —— 括号内完整日期时间 (真实日志常见)
 2. [hh:mm:ss(.ms)]             —— 括号内纯时间, MMORPG 常见
 3. YYYY-MM-DD HH:MM:SS(.ms)     —— 裸完整日期时间
 4. hh:mm:ss(.ms)                —— 裸时间, 用给定/当天日期补全

parse_timestamp 可接受 _now (datetime) 以便测试时确定日期部分;
默认 None 时取当前时间。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional, Tuple

TsKey = Tuple[float, int]

_RE_BRACKET = re.compile(
    r"^\s*\[\s*(\d{1,2}):(\d{2}):(\d{2})(?:[.,](\d{1,6}))?\s*\]"
)
# 括号内完整日期时间: [YYYY-MM-DD HH:MM:SS.mmm] (真实日志常见, 如 [2026-08-27 11:20:01.228608])
_RE_BRACKET_FULLDATE = re.compile(
    r"^\s*\[\s*(\d{4})-(\d{2})-(\d{2})[T ](\d{1,2}):(\d{2}):(\d{2})(?:[.,](\d{1,6}))?\s*\]"
)
_RE_FULLDATE = re.compile(
    r"^\s*(\d{4})-(\d{2})-(\d{2})[T ](\d{1,2}):(\d{2}):(\d{2})(?:[.,](\d{1,6}))?"
)
_RE_TIME = re.compile(
    r"^\s*(\d{1,2}):(\d{2}):(\d{2})(?:[.,](\d{1,6}))?"
)


def _frac(milli_text: Optional[str]) -> int:
    """把小数部分(1~6 位)补足成微秒."""
    if not milli_text:
        return 0
    return int(milli_text.ljust(6, "0")[:6])


def _compose(sec_of_day: int, frac_us: int, _now: Optional[datetime]) -> TsKey:
    """把当天第 sec_of_day 秒换算成绝对 epoch (秒数, 微秒)."""
    now = _now or datetime.now()
    mid = now.replace(hour=0, minute=0, second=0, microsecond=0)
    epoch = mid.timestamp() + sec_of_day
    return (float(epoch), frac_us)


def parse_timestamp(line: str, _now: Optional[datetime] = None) -> Optional[TsKey]:
    """尝试解析行首时间戳, 返回 (epoch 秒数, 微秒) 或 None."""
    hit = extract_timestamp(line, _now)
    return hit[0] if hit else None


def extract_timestamp(line: str, _now: Optional[datetime] = None):
    """解析行首时间戳, 返回 (ts_key, start, end) 或 None.

    start/end 为时间戳在原文中的字符区间, 便于调用方剥离前导时间戳后
    得到正文 (避免前缀时间戳与正文重复显示)。
    """
    m = _RE_BRACKET.match(line)
    if m:
        hh, mm, ss, frac = m.groups()
        key = _compose(int(hh) * 3600 + int(mm) * 60 + int(ss), _frac(frac), _now)
        return (key, m.start(), m.end())

    m = _RE_BRACKET_FULLDATE.match(line)
    if m:
        y, mo, d, hh, mm, ss, frac = m.groups()
        dt = datetime(int(y), int(mo), int(d), int(hh), int(mm), int(ss))
        return ((dt.timestamp(), _frac(frac)), m.start(), m.end())

    m = _RE_FULLDATE.match(line)
    if m:
        y, mo, d, hh, mm, ss, frac = m.groups()
        dt = datetime(int(y), int(mo), int(d), int(hh), int(mm), int(ss))
        return ((dt.timestamp(), _frac(frac)), m.start(), m.end())

    m = _RE_TIME.match(line)
    if m:
        hh, mm, ss, frac = m.groups()
        key = _compose(int(hh) * 3600 + int(mm) * 60 + int(ss), _frac(frac), _now)
        return (key, m.start(), m.end())

    return None
