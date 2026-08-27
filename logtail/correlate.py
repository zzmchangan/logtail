"""关联键 (correlation key): 从每行日志抽取共享标识, 归一化后跨源对齐.

解决"同一个逻辑 id 在不同进程被打印成不同格式"的痛点:
 - player=123 (Scene) / RoleId:123 (Guild) / guid:123 (Match) 字面匹配串不起来,
   但抽取 + 归一化后都是 "123", 就能跨源对齐成一条逻辑链路 (一个玩家/副本/请求)。
 - 每个 key = 一组提取正则 (覆盖各进程不同写法), 第一个命中即取, 归一化。
 - 未定义的 key 由调用方回退成现有字面 --trace 子串行为 (老用法不受影响)。

不预设"哪个 id 是稳定关联点": 工具只做机制, 唯一性由 self-report (lines_with_key)
让使用者自己验证 —— 延续"别猜因果, 给尺子"原则。
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional


def normalize(raw: str) -> str:
    """归一化提取值: 去掉所有空白; 纯数字则去前导零 (避免 '007'/'7' 不一致)."""
    v = re.sub(r"\s+", "", raw)
    if v.isdigit():
        v = v.lstrip("0") or "0"
    return v


# 内置预设 (开箱即用; 未定义 key 时才用, 配置里同名 key 优先)
_PRESETS: Dict[str, List[str]] = {
    "player": [
        r"player[:=]\s*(\d+)",
        r"roleid[:=]\s*(\d+)",
        r"guid[:=]\s*(\d+)",
    ],
    # 候选: 跨服调用/会话 token (如 [s=16bd7af3&c=413378]); 唯一性需用 self-report 验证
    "session": [
        r"&?s=([0-9a-fA-F]+)",
        r"s=([0-9a-fA-F]+)",
    ],
}


class CorrelationKeys:
    """管理一组关联键: 每个 key 按顺序试其正则, 取第一个抓到非空值的."""

    def __init__(self, raw: Optional[List[dict]] = None,
                 presets: bool = True,
                 case_sensitive: bool = False) -> None:
        self._keys: Dict[str, List[re.Pattern]] = {}
        flags = 0 if case_sensitive else re.IGNORECASE
        for k in (raw or []):
            name = str(k.get("name", "")).strip()
            patterns = k.get("extract") or []
            if not name or not patterns:
                continue
            compiled = []
            for p in patterns:
                try:
                    compiled.append(re.compile(p, flags))
                except re.error:
                    continue
            self._keys[name] = compiled
        if presets:
            for name, patterns in _PRESETS.items():
                if name not in self._keys:
                    self._keys[name] = [re.compile(p, flags) for p in patterns]

    def is_defined(self, key: str) -> bool:
        return key in self._keys

    def keys(self) -> List[str]:
        return list(self._keys)

    def extract1(self, line: str, key: str) -> Optional[str]:
        """抽取 line 中 key 的值 (按该 key 的正则顺序, 第一个抓到非空即用)."""
        for c in self._keys.get(key, []):
            m = c.search(line)
            if not m:
                continue
            grp = m.group(1) if m.groups() else m.group(0)
            if grp is not None:
                return normalize(grp)
        return None

    def value_of(self, text: str) -> Dict[str, str]:
        """一行里所有 key 的提取结果 (供 self-report 统计)."""
        out: Dict[str, str] = {}
        for key in self._keys:
            v = self.extract1(text, key)
            if v is not None:
                out[key] = v
        return out
