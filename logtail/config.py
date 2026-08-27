"""配置加载与保存.

从 YAML (或等价 dict) 读取日志源、黑名单、初始高亮词;
CLI 参数可覆盖 / 追加日志源。启动前做基本校验给出明确错误。
save() 幂等地把运行时高亮词与黑名单写回 YAML, 仅由 /save 命令触发。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import yaml

from .models import SourceConfig


class ConfigError(ValueError):
    """配置错误, message 为面向用户的中文提示."""


@dataclass
class Config:
    path: Optional[str] = None                 # 配置文件路径, 可无
    sources: List[SourceConfig] = field(default_factory=list)
    blacklist: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    correlation_keys: List[dict] = field(default_factory=list)

    # CLI 覆盖项 (不写回配置, 仅本次运行生效)
    history: int = 0
    context_n: int = 5
    date: str = ""                             # YYYY-MM-DD, 填充 {date} 占位符
    since: float = 0.0                         # >0: 只看最近 N 秒 (交互/agent 均可用)
    level: str = ""                            # >= 该级别过滤, 如 "ERROR"; 空=不过滤
    trace: str = ""                            # 实体追踪词; 空=不追踪
    case_sensitive: bool = False               # True: 本次运行所有文本匹配区分大小写
    anchor: float = 0.0                        # >0: --since 窗口钉死在 [anchor-since, anchor]

    def validate(self) -> None:
        """校验源目录存在、pattern 合法, 给出明确错误."""
        if not self.sources:
            raise ConfigError("未配置任何日志源 (log_sources 为空)。")
        for src in self.sources:
            if not src.path and not src.dx:
                raise ConfigError(f"源 {src.name!r} 缺少 path (与 dx 至少其一)。")
            # dx 模式由命令提供具体路径, 不校验目录; glob 模式校验目录存在
            if not src.dx and not os.path.isdir(src.path):
                raise ConfigError(
                    f"源 {src.name!r} 的目录不存在: {src.path!r}"
                )
            if not src.pattern and not src.dx:
                raise ConfigError(f"源 {src.name!r} 缺少 pattern。")


def expand_date(text: str, date: str) -> str:
    """把 {date} / {YYYY} / {MM} / {DD} 占位符替换成具体日期.

    date 为 'YYYY-MM-DD'; 留空则默认为今天, 使 {date} 始终能被填充。
    """
    if not date:
        date = _today()
    if not text:
        return text
    y, m, d = date.split("-")
    return (text.replace("{date}", date)
                .replace("{YYYY}", y)
                .replace("{MM}", m)
                .replace("{DD}", d))


def _today() -> str:
    """返回今天的 YYYY-MM-DD (不依赖外部命令)."""
    import datetime
    return datetime.date.today().isoformat()


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------

def _parse_source(raw: Dict, date: str) -> SourceConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"日志源必须是键值结构: {raw!r}")
    name = str(raw.get("name", "unnamed"))
    path = expand_date(str(raw.get("path", "")), date)
    pattern = expand_date(str(raw.get("pattern", "*.log")), date)
    dx = str(raw.get("dx", ""))

    # dx 命令若含 {date} 也一并替换 (如 dx log SceneServer {date})
    if dx:
        dx = expand_date(dx, date)
        # dx 模式无需 path/pattern; 但仍保留以备 glob 兜底
    return SourceConfig(name=name, path=path, pattern=pattern, dx=dx)


def load_config(path: Optional[str], date: str = "") -> Config:
    """从文件读取配置; 文件不存在时返回空配置 (仅来源靠 CLI)."""
    cfg = Config(path=path, date=date)
    if path is None:
        return cfg
    if not os.path.exists(path):
        raise ConfigError(f"配置文件不存在: {path!r}")

    with open(path, "r", encoding="utf-8") as fh:
        try:
            data = yaml.safe_load(fh) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"配置文件 YAML 解析失败: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError("配置文件顶层必须是映射结构。")

    sources_raw = data.get("log_sources", []) or []
    cfg.sources = [_parse_source(s, date) for s in sources_raw]

    keys = data.get("keywords", []) or []
    bls = data.get("blacklist", []) or []
    cfg.keywords = [str(k) for k in keys]
    cfg.blacklist = [str(b) for b in bls]

    cks = data.get("correlation_keys", []) or []
    cfg.correlation_keys = [dict(k) for k in cks if isinstance(k, dict)]
    return cfg


def apply_cli(cfg: Config, cli_sources: Optional[List[str]],
              history: int, context_n: int, date: str = "") -> Config:
    """把 CLI 覆盖项写进配置; cli_sources 格式 'name:path:pattern'."""
    for entry in (cli_sources or []):
        parts = entry.split(":", 2)
        if len(parts) == 3:
            cfg.sources.append(
                SourceConfig(parts[0], expand_date(parts[1], date),
                             expand_date(parts[2], date)))
        elif len(parts) == 2:
            cfg.sources.append(
                SourceConfig(parts[0], expand_date(parts[1], date), "*.log"))
        else:
            raise ConfigError(
                f"--source 格式应为 name:path[:pattern], 收到: {entry!r}"
            )
    if history:
        cfg.history = history
    if context_n:
        cfg.context_n = context_n
    return cfg


# ---------------------------------------------------------------------------
# 保存 (仅 /save 触发)
# ---------------------------------------------------------------------------

def save_config(path: Optional[str], keywords: List[str],
                blacklist: List[str]) -> str:
    """把当前 keywords/blacklist 写回配置, 保留其余字段 (如 log_sources) 与注释.

    返回实际写入的文件路径; 若没有配置文件路径则抛出配置错误。
    """
    if not path:
        raise ConfigError("没有配置文件路径, 无法 /save (未用 --config 启动)。")

    with open(path, "r", encoding="utf-8") as fh:
        orig = fh.read()

    # 逐行替换 blacklist: 与 keywords: 块, 保留其余原文与注释。
    updated = _replace_list_block(orig, "blacklist", blacklist)
    updated = _replace_list_block(updated, "keywords", keywords)

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(updated)
    return path


# 列表项块的正则: 匹配 "key:" 之后紧跟的若干 "- xxx" 行 (含注释/空行分隔)
# 注意: 这个正则已弃用 (会连同键下的注释行一起被替换), 改用下面的逐行替换法。


def _list_item_no_comment(raw_value: str) -> str:
    """去掉列表项行内注释, 返回裸值 (如 '- hb  # 说明' -> 'hb')."""
    # 去掉 "- " 前缀, 再按 ' #' 切掉注释
    v = raw_value.strip()
    if v.startswith("- "):
        v = v[2:]
    # 找紧跟在值后、以 # 开头的注释; 值本身可能含空格, 用 ' #' 定位
    idx = v.find(" #")
    if idx >= 0:
        v = v[:idx].rstrip()
    # 去掉成对包裹引号: 配置里 '- "hb"' 与运行时裸词 'hb' 应视为同值,
    # 否则 /save 时带引号旧项的行内注释无法贴回 (example.yaml 全是引号写法)。
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    return v


def _list_item_comment(raw_value: str) -> str:
    """取列表项行内注释尾巴 (含 '#' 起), 无则返回空串."""
    idx = raw_value.find(" #")
    if idx >= 0:
        return raw_value[idx:].rstrip()
    return ""


def _replace_list_block(text: str, key: str, items: List[str]) -> str:
    """把文本中 `key:` 列表块下的 `- item` 行替换成给定项, 其余原文不动.

    - 只替换紧跟在顶格 `key:` 后的 `- xxx` 列表行;
    - 独立 `#` 注释行、空行、log_sources 等其余内容原样保留;
    - 若某项仍存在于新增项里, 且原行带行内注释, 则把该行内注释贴回;
    - 若文本里没有该 key (新配置), 则追加到末尾。
    """
    lines = text.split("\n")
    out = []
    found = False
    i = 0
    # 原块中 (值 -> 行内注释) 映射, 用于保留未变更项的行内注释
    inline_comments: dict[str, str] = {}
    while i < len(lines):
        line = lines[i]
        at_key = not found and line == line.lstrip() and line.rstrip() == key + ":"
        if at_key:
            found = True
            out.append(line)
            i += 1
            # 先扫一遍原列表项, 记录它们的行内注释 (值 -> 注释尾巴)
            block_end = i
            while block_end < len(lines) and lines[block_end].strip().startswith("- "):
                raw = lines[block_end].strip()
                val = _list_item_no_comment(raw)
                com = _list_item_comment(raw)
                if com:
                    inline_comments[val] = com
                block_end += 1
            # 输出新项, 尽量带回原行内注释
            for it in items:
                com = inline_comments.get(it, "")
                out.append(f"- {it}{(' ' + com) if com else ''}")
            i = block_end
            continue
        out.append(line)
        i += 1

    if not found:
        if out and out[-1].strip():
            out.append("")          # 空行分隔
        out.append(key + ":")
        for it in items:
            out.append(f"- {it}")
    return "\n".join(out)
