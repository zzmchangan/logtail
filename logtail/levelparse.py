"""轻量日志级别解析.

从日志行里识别级别 token (TRACE/DEBUG/INFO/WARN/ERROR/FATAL), 大小写不敏感。
不做复杂结构假设, 只做宽泛的 token 匹配 —— 找行首时间戳之后的第一个级别词,
或行内任何位置独立的级别词。解析不到返回 "" (无级别)。

LEVELS 定义了级别顺序与规范化名称, 供过滤/着色使用。
"""

from __future__ import annotations

import re
from typing import List

# (规范化名称, 原始 token)
_LEVEL_TOKENS = [
    ("TRACE", "TRACE"),
    ("DEBUG", "DEBUG"),
    ("INFO", "INFO"),
    ("WARN", "WARN"),
    ("WARN", "WARNING"),
    ("ERROR", "ERROR"),
    ("ERROR", "ERR"),
    ("FATAL", "FATAL"),
    ("FATAL", "CRITICAL"),
    ("FATAL", "CRIT"),
]

# 按严重度排序 (数值越大越严重); 用于 ">= 某级别" 的批量筛选
LEVEL_ORDER = ["TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"]
_LEVEL_WEIGHT = {n: i for i, n in enumerate(LEVEL_ORDER)}

# 词边界: 级别 token 需是独立单词 (前后是非字母数字下划线), 避免误配 "DEBUGGER"
_BOUND = r"(?<![A-Za-z0-9_])({tokens})(?![A-Za-z0-9_])"
# 常见包裹: [WARN] / [Warn] / [ERROR] / (INFO) / level=ERROR, 或裸词
_LEVEL_RE = re.compile(
    _BOUND.format(tokens="|".join(t[1] for t in _LEVEL_TOKENS)),
    re.IGNORECASE,
)


def parse_level(line: str) -> str:
    """识别行内独立级别 token, 返回规范化级别名 (TRACE..FATAL), 无则 ""."""
    m = _LEVEL_RE.search(line)
    if not m:
        return ""
    tok = m.group(0).upper()
    for norm, raw in _LEVEL_TOKENS:
        if raw.upper() == tok:
            return norm
    return tok


def level_weight(level: str) -> int:
    """返回级别权重, 用于 '>= 某级' 筛选; 无级别返回 -1 (最不严重)."""
    return _LEVEL_WEIGHT.get(level, -1)


def filter_by_level(level: str, min_level: str) -> bool:
    """level 是否满足 >= min_level (按严重度)."""
    if not min_level:
        return True
    return level_weight(level) >= level_weight(min_level)
