"""命令行入口.

用法示例:
    python -m logtail --config config.yaml
    python -m logtail --config config.yaml --history 50
    python -m logtail --source logic:/data/logs/logic:logic_*.log --context 3
    python -m logtail --config config.yaml --date 2026-08-26   # 看某天日志
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .config import ConfigError, apply_cli, load_config
from .tui import main as tui_main


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="logtail",
        description="多文件实时日志聚合查看工具: 单终端聚合跟踪多日志文件, "
                    "支持黑名单过滤、交互式高亮、上下文聚焦、暂停/恢复、历史回溯。",
        epilog="也可 --agent 开启非交互模式, 供 AI Agent / 管道消费过滤后的少量日志。",
    )
    p.add_argument("--config", "-c", default=None,
                   help="配置文件路径 (YAML); 支持 {date} 占位符")
    p.add_argument("--source", "-s", action="append", default=None,
                   metavar="NAME:PATH[:PATTERN]",
                   help="临时追加/覆盖一个日志源, 可重复")
    p.add_argument("--history", default=0, type=int,
                   help="启动时回溯最近 N 行 (从文件末尾往回)")
    p.add_argument("--context", "-C", default=0, type=int,
                   help="上下文窗口: 交互=默认窗口大小; agent+match 时=每条命中行连带前后各 N 行")
    p.add_argument("--level", default=None,
                   help="只保留 >= 该级别的日志 (TRACE/DEBUG/INFO/WARN/ERROR/FATAL); 交互与 agent 均可用")
    p.add_argument("--date", default="",
                   help="以 YYYY-MM-DD 填充 {date} 占位符, 默认当天")
    # AI Agent 模式 (非交互, 无终端转义)
    p.add_argument("--agent", "-a", action="store_true",
                   help="AI Agent 模式: 输出纯文本日志到 stdout, 供 Agent/grep 消费")
    p.add_argument("--mode", "--dump-monitor", choices=["dump", "monitor"],
                   dest="agent_mode",
                   help="agent 模式形态: dump=一次性收集后退出, monitor=持续打 stdout (默认 dump)")
    p.add_argument("--match", "-m", default=None,
                   help="只输出命中该词的日志 (裸词=子串, re: 前缀=正则, 大小写不敏感; 逗号/空格分隔多词=OR)")
    p.add_argument("--exclude", "-e", default=None,
                   help="排除命中该词的日志 (逗号/空格分隔多词; 与 --match 叠加)")
    p.add_argument("--trace", default=None,
                   help="实体追踪: 只显示所有源中含该词的干净命中行 (无邻居行); 交互 /trace 与 agent 均可用")
    p.add_argument("--since", default=None,
                   help="只看最近一段时间 (如 5m/1h/30s), 按日志自带时间戳过滤; "
                        "交互与 agent 模式均可用。排查服务器启动报错时回看启动窗口。")
    p.add_argument("--count", "--cnt", action="store_true",
                   help="agent dump 模式只输出命中行数, 不打印正文 (快速判断是否爆发)")
    p.add_argument("--lines", "-n", type=int, default=50,
                   help="agent 模式输出/收集的行数上限 (默认 50)")
    p.add_argument("--wait", type=float, default=2.0,
                   help="agent dump 模式收集窗口 (秒, 默认 2.0)")
    p.add_argument("--version", action="version", version=f"logtail {__version__}")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        since = _parse_duration(args.since)
    except ValueError as exc:
        print(f"logtail: 配置错误: {exc}", file=sys.stderr)
        return 2
    try:
        cfg = load_config(args.config, date=args.date)
        apply_cli(cfg, args.source, args.history, args.context, date=args.date)
        cfg.since = since or 0.0
        if args.level:
            cfg.level = args.level.upper()
        if args.trace:
            cfg.trace = args.trace
        cfg.validate()
    except ConfigError as exc:
        print(f"logtail: 配置错误: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"logtail: {exc}", file=sys.stderr)
        return 2

    # AI Agent 模式: 不走 curses, 直接输出纯文本
    if args.agent:
        from .agent import dump, monitor
        try:
            if args.agent_mode == "monitor":
                return monitor(cfg, args.match, args.lines, exclude=args.exclude)
            # dump: --context/-C 可当作命中行的上下文窗口 (结合 --match 用)
            # --trace 作为 match 但零上下文 (纯命中行, 无邻居)
            match = args.trace if args.trace else args.match
            ctx = 0 if args.trace else args.context
            return dump(cfg, match, args.lines, args.wait,
                        context=ctx, since=since,
                        count_only=args.count, exclude=args.exclude)
        except RulePatternError as exc:
            print(f"logtail: --match 规则错误: {exc}", file=sys.stderr)
            return 2

    return tui_main(cfg)


def _parse_duration(text: Optional[str]) -> Optional[float]:
    """把 '30s'/'5m'/'1h' 解析为秒数; 无效或空返回 None."""
    if not text:
        return None
    text = text.strip().lower()
    m = __import__("re").match(r"^(\d+)([smhd])$", text)
    if not m:
        raise ValueError(f"--since 无效: {text!r} (可用 30s/5m/1h)")
    n = int(m.group(1))
    unit = {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    return n * unit


if __name__ == "__main__":
    sys.exit(main())
