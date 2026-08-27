"""黑名单 & 高亮词的统一匹配规则.

约定:
 - 裸词      -> 大小写不敏感子串匹配 (pattern.lower() in text.lower())
 - "re:" 前缀 -> 正则表达式匹配 (re.search, 预编译 + IGNORECASE)
所有匹配一律大小写不敏感。
正则编译失败会抛出带明确提示的 ValueError,便于上层给用户友好反馈,
而不是让整个工具崩溃。

RuleSet 把规则按用途分成两类: highlight(高亮, 参与着色)
与 blacklist(黑名单, 采集阶段丢弃)。两者共用同一套匹配逻辑。
"""

from __future__ import annotations

import itertools
import re
from typing import Dict, List, Optional, Tuple


class RulePatternError(ValueError):
    """正则编译失败时抛出, message 为面向用户的中文提示."""


class Rule:
    """单条匹配规则."""

    __slots__ = ("rule_id", "kind", "pattern", "is_regex", "_matcher")

    def __init__(self, rule_id: int, kind: str, pattern: str) -> None:
        self.rule_id = rule_id
        self.kind = kind            # "highlight" 或 "blacklist"
        self.pattern = pattern
        self.is_regex = pattern.startswith("re:")
        if self.is_regex:
            expr = pattern[3:]
            try:
                # IGNORECASE: 正则同样大小写不敏感
                self._matcher = re.compile(expr, re.IGNORECASE)
            except re.error as exc:
                raise RulePatternError(
                    f"正则表达式无效: {expr!r} ({exc})"
                ) from exc
        else:
            self._matcher = None

    def matches(self, text: str) -> bool:
        if self.is_regex:
            return bool(self._matcher.search(text))
        return self.pattern.lower() in text.lower()

    def __repr__(self) -> str:  # pragma: no cover - 仅为调试
        return f"<Rule {self.kind}:{self.pattern!r}>"


class RuleSet:
    """管理高亮与黑名单规则, 支持运行时动态增删."""

    def __init__(self, keywords: Optional[List[str]] = None,
                 blacklist: Optional[List[str]] = None) -> None:
        self._ids = itertools.count(1)
        self._rules: Dict[Tuple[str, str], Rule] = {}   # (kind, pattern) -> Rule
        for kw in (keywords or []):
            self.add(kw, "highlight")
        for bl in (blacklist or []):
            self.add(bl, "blacklist")
        # 级别过滤: min_level -> 只保留严重度 >= 它的行; 空 = 不过滤
        self.min_level = ""
        self.exclude_levels: set[str] = set()

    # ------------------------------------------------------------------
    # 级别过滤
    # ------------------------------------------------------------------
    def set_level_filter(self, min_level: str) -> None:
        from .levelparse import LEVEL_ORDER
        if min_level and min_level.upper() not in LEVEL_ORDER:
            raise ValueError(
                f"无效级别: {min_level!r} (可用 {', '.join(LEVEL_ORDER)})")
        self.min_level = min_level.upper() if min_level else ""

    def add_exclude_level(self, level: str) -> None:
        from .levelparse import LEVEL_ORDER
        if level.upper() in LEVEL_ORDER:
            self.exclude_levels.add(level.upper())

    def clear_exclude_levels(self) -> None:
        self.exclude_levels.clear()

    def level_ok(self, level: str) -> bool:
        """该级别是否应显示 (>= min_level 且不被排除)."""
        from .levelparse import filter_by_level
        if self.min_level and not filter_by_level(level, self.min_level):
            return False
        return level not in self.exclude_levels

    # ------------------------------------------------------------------
    # 增删
    # ------------------------------------------------------------------
    def add(self, pattern: str, kind: str) -> Rule:
        """新增一条规则. 若同用途同内容已存在则返回已有规则."""
        key = (kind, pattern)
        if key in self._rules:
            return self._rules[key]
        rule = Rule(next(self._ids), kind, pattern)
        self._rules[key] = rule
        return rule

    def remove(self, pattern: str, kind: str) -> bool:
        """按原始字符串移除规则, 返回是否真的移除了."""
        return self._rules.pop((kind, pattern), None) is not None

    def clear(self, kind: str) -> int:
        """清空某一用途的全部规则, 返回移除了多少条."""
        to_del = [k for k in self._rules if k[0] == kind]
        for k in to_del:
            del self._rules[k]
        return len(to_del)

    def reset(self, keywords: Optional[List[str]], blacklist: Optional[List[str]]) -> None:
        """重置到给定初始值 (用于 /reset, 相当于回到配置状态)."""
        self.clear("highlight")
        self.clear("blacklist")
        self.min_level = ""
        self.exclude_levels.clear()
        for kw in (keywords or []):
            self.add(kw, "highlight")
        for bl in (blacklist or []):
            self.add(bl, "blacklist")

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def blocked(self, text: str) -> bool:
        """是否命中黑名单 (命中则应当丢弃)."""
        return any(r.matches(text) for r in self._rules.values()
                   if r.kind == "blacklist")

    def highlights(self, text: str) -> List[Rule]:
        """返回文本命中的所有高亮规则 (用于着色)."""
        return [r for r in self._rules.values()
                if r.kind == "highlight" and r.matches(text)]

    def has_highlights(self) -> bool:
        return any(r.kind == "highlight" for r in self._rules.values())

    def list_highlights(self) -> List[Rule]:
        return [r for r in self._rules.values() if r.kind == "highlight"]

    def list_blacklist(self) -> List[Rule]:
        return [r for r in self._rules.values() if r.kind == "blacklist"]
