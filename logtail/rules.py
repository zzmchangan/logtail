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


# 形似"灾难性回溯"的启发式: 一个被整体 +/* 量化的组, 组内又含量词 —— 经典的
# (a+)+ / (.*)+ / (\w+)* 形态, 在长文本上会让 stdlib 的 re 回溯爆炸卡死进程。
# 只抓最常见的一类, 不保全局; 命中就预警, 而不是等它卡死主循环。
_CATA_RE = re.compile(r"\([^()]*[+*][^()]*\)[+*]")


def looks_catastrophic(expr: str) -> bool:
    """启发式判断一个正则是否'形似灾难性回溯'(组内含量词且整体再被量化)."""
    return bool(_CATA_RE.search(expr))


def catastrophic_rules(rules: Optional[List["Rule"]]) -> List["Rule"]:
    """从一组规则里挑出形似灾难性回溯的 (供上层预警; RuleSet.add 只标记不预警)."""
    return [r for r in (rules or []) if r.is_regex and r.catastrophic]


class Rule:
    """单条匹配规则."""

    __slots__ = ("rule_id", "kind", "pattern", "is_regex", "case_sensitive",
                 "_matcher", "catastrophic")

    def __init__(self, rule_id: int, kind: str, pattern: str,
                 case_sensitive: bool = False) -> None:
        self.rule_id = rule_id
        self.kind = kind            # "highlight" 或 "blacklist"
        self.pattern = pattern
        self.case_sensitive = case_sensitive
        self.is_regex = pattern.startswith("re:")
        self.catastrophic = False
        if self.is_regex:
            expr = pattern[3:]
            self.catastrophic = looks_catastrophic(expr)
            try:
                # 默认大小写不敏感; case_sensitive 时精确 (re: 目前无法自带关闭)
                flags = 0 if case_sensitive else re.IGNORECASE
                self._matcher = re.compile(expr, flags)
            except re.error as exc:
                raise RulePatternError(
                    f"正则表达式无效: {expr!r} ({exc})"
                ) from exc
        else:
            self._matcher = None

    def matches(self, text: str) -> bool:
        if self.is_regex:
            return bool(self._matcher.search(text))
        if self.case_sensitive:
            return self.pattern in text
        return self.pattern.lower() in text.lower()

    def __repr__(self) -> str:  # pragma: no cover - 仅为调试
        return f"<Rule {self.kind}:{self.pattern!r}>"


class RuleSet:
    """管理高亮与黑名单规则, 支持运行时动态增删."""

    def __init__(self, keywords: Optional[List[str]] = None,
                 blacklist: Optional[List[str]] = None,
                 case_sensitive: bool = False) -> None:
        self._ids = itertools.count(1)
        self._rules: Dict[Tuple[str, str], Rule] = {}   # (kind, pattern) -> Rule
        self._case_sensitive = case_sensitive
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
        rule = Rule(next(self._ids), kind, pattern,
                    case_sensitive=self._case_sensitive)
        self._rules[key] = rule
        return rule

    def remove(self, pattern: str, kind: str) -> int:
        """移除某用途下所有与 pattern 相同的规则, 返回移除条数 (0 = 没删到).

        默认匹配大小写不敏感, 移除也应大小写不敏感 —— 否则 /rm ERROR 打成
        error 会报"未找到", 与"匹配不分大小写"不一致。case_sensitive 配置时
        则精确匹配。同一关键词的大小写变体会被一起清掉, 不留冗余规则。
        """
        if self._case_sensitive:
            return 1 if self._rules.pop((kind, pattern), None) is not None else 0
        low = pattern.lower()
        to_pop = [k for k in self._rules if k[0] == kind and k[1].lower() == low]
        for k in to_pop:
            del self._rules[k]
        return len(to_pop)

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
