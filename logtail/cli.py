"""命令行入口.

用法示例:
    python -m logtail --config config.yaml
    python -m logtail --config config.yaml --history 50
    python -m logtail --source logic:/data/logs/logic:logic_*.log --context 3
    python -m logtail --config config.yaml --date 2026-08-26   # 看某天日志
"""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .config import ConfigError, apply_cli, load_config
from .rules import RulePatternError
from .tui import main as tui_main


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="logtail",
        description="多文件实时日志聚合查看工具: 单终端聚合跟踪多日志文件, "
                    "支持黑名单过滤、交互式高亮、上下文聚焦、暂停/恢复、历史回溯。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "非交互 / 脚本 / AI 场景必须加 --agent, 否则进入全屏交互 TUI (curses, 需真实终端)。\n"
            "\n"
            "三层读取模型 (三个独立旋钮, --lines 不是'读多少行'而是输出上限):\n"
            "  采集量: --since(优先, 按时间戳定位, 8MB/文件上限, 触顶 stderr 警告) 或 --history/--lines。\n"
            "  过滤:   --match/--exclude/--level/--focus/--correlate (--exclude 单独用也生效)。\n"
            "  输出量: --lines 只限正文条数; --count 统计全部读取量、不受 --lines 限。\n"
            "\n"
            "输出契约 (AI Agent / 脚本接入):\n"
            "  日志正文只写 stdout, 错误/提示写 stderr (可 2>/dev/null 只取正文)。\n"
            "  每行 = '{时间戳} {来源:<12} {正文}'; 退出码: 0=成功(含 0 命中), 2=配置/参数/正则错误。\n"
            "  agent 一次性收集后按 (时间戳, 序列号) 全局排序; 无时间戳行退化为到达时刻。\n"
            "  空结果/--count 0 时先看 stderr: 有 warning 或 --summary 的 JSON 里 files==0/发现失败\n"
            "  -> 是'源没被发现'而非'没错误' (避免 exit 0 + 空输出 = 假阴性); files>0 才可信。\n"
            "\n"
            "作用域提示:\n"
            "  --lines/--wait/--count 仅 agent 生效; --mode monitor 忽略 -C/--since/--count。\n"
            "  --since(dump) 以窗口内最新日志时间戳为参考; '-C 5s' 时间窗仅交互版可用 (agent 的 -C 只接受行数)。\n"
            "  --wait(dump) 自收到首条有效行后约 1s 无新行才提前返回(初始化期不提前, 会等到 --wait)。\n"
            "  --ctx-same N 仅 agent+match(同进程上下文, 与 -C 全局互补); --diagnose 独立健康检查(不 tail)。\n"
            "  --focus <源名> 单源聚焦(按配置源名筛, dx/glob 均有效, 与 --ctx-same 互补)。\n"
            "  --correlate key=value 关联键(抽取+归一化跨源对齐; 未定义 key 回退字面子串);\n"
            "  --summary 的 correlate 自报 lines_with_key 用于判断正则是否写歪/key 是否有区分度。\n"
            "  --date 仅对含 {date} 占位符的源生效(dx 命令无占位符时给 --date 无效, stderr 会警告)。\n"
        ),
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
    p.add_argument("--focus", default=None,
                   help="单源聚焦: 只输出指定来源 (name) 的行, 如 --focus scene; dx/glob 源均有效")
    p.add_argument("--correlate", default=None,
                   help="按关联键对齐: 跨源只留抽出该 id 的行, 如 --correlate player=123; "
                        "key 在 config correlation_keys/预设里=抽取+归一化, 未定义=回退字面 --trace")
    p.add_argument("--since", default=None,
                   help="只看最近一段时间 (如 5m/1h/30s), 按日志自带时间戳过滤; "
                        "交互与 agent 模式均可用。排查服务器启动报错时回看启动窗口。")
    p.add_argument("--count", "--cnt", action="store_true",
                   help="agent dump 模式只输出命中行数, 不打印正文 (快速判断是否爆发)")
    p.add_argument("--lines", "-n", type=int, default=50,
                   help="agent 模式输出/收集的行数上限 (默认 50)")
    p.add_argument("--wait", type=float, default=2.0,
                   help="agent dump 模式收集窗口 (秒, 默认 2.0)")
    p.add_argument("--summary", action="store_true",
                   help="agent 结束时把'发现诊断'(JSON)写到 stderr, 区分'无日志'与'源未发现'")
    p.add_argument("--json", action="store_true", dest="as_json",
                   help="agent 每行输出一个 JSON 对象 (NDJSON), 供编程级加工 (默认定宽文本)")
    p.add_argument("--ctx-same", type=int, default=0,
                   help="agent+match: 每条命中行连同**同进程**前后各 N 行 (跳过其它进程的行)")
    p.add_argument("--diagnose", action="store_true",
                   help="只做发现健康检查(不 tail): 每源文件数/dx 状态/最后时间戳, JSON 输出")
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
            # fail-fast: 无效级别立即报错, 而不是静默不过滤 (agent 的 _build_blk
            # 会吞掉 ValueError, 拼错级别会输出全量、极难察觉)
            from .levelparse import LEVEL_ORDER
            if args.level.upper() not in LEVEL_ORDER:
                print(f"logtail: --level 无效: {args.level!r} "
                      f"(可用 {', '.join(LEVEL_ORDER)})", file=sys.stderr)
                return 2
            cfg.level = args.level.upper()
        if args.trace:
            cfg.trace = args.trace
        cfg.validate()
        # --date 只对含 {date} 占位符的源生效; dx 源若命令里没写 {date}
        # (且 dx CLI 不接受日期参数), 给了 --date 也仍读当天 —— 明示而非静默。
        if args.date:
            blind = [s.name for s in cfg.sources
                     if s.dx and "{date}" not in s.dx
                     and "{YYYY}" not in s.dx and "{MM}" not in s.dx
                     and "{DD}" not in s.dx]
            if blind:
                print(f"logtail: warning: --date 对 dx 命令不含日期占位符的源无效 "
                      f"(仍读当天): {', '.join(blind)}", file=sys.stderr)
    except ConfigError as exc:
        print(f"logtail: 配置错误: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"logtail: {exc}", file=sys.stderr)
        return 2

    # 健康检查: 只探测源是否被发现, 不 tail 不读正文. 供 agent 先验证再信 "0 命中".
    if args.diagnose:
        import json
        from .reader import LogFollower
        report = LogFollower(cfg.sources).diagnose()
        total_files = sum(s["files"] for s in report)
        print(json.dumps({
            "kind": "logtail.diagnose",
            "sources": report,
            "total_files": total_files,
        }, ensure_ascii=False))
        return 0

    # AI Agent 模式: 不走 curses, 直接输出纯文本
    if args.agent:
        from .agent import dump, monitor
        try:
            if args.agent_mode == "monitor":
                return monitor(cfg, args.match, args.lines,
                               exclude=args.exclude, summary=args.summary,
                               as_json=args.as_json, focus=args.focus,
                               correlate=args.correlate)
            # dump: --context/-C 可当作命中行的上下文窗口 (结合 --match 用)
            # --trace 作为 match 但零上下文 (纯命中行, 无邻居)
            match = args.trace if args.trace else args.match
            ctx = 0 if args.trace else args.context
            return dump(cfg, match, args.lines, args.wait,
                        context=ctx, since=since,
                        count_only=args.count, exclude=args.exclude,
                        summary=args.summary, ctx_same=args.ctx_same,
                        as_json=args.as_json, focus=args.focus,
                        correlate=args.correlate)
        except RulePatternError as exc:
            print(f"logtail: --match 规则错误: {exc}", file=sys.stderr)
            return 2
        except BrokenPipeError:
            # 下游 (head/grep) 提前关闭管道: 静默退出, 不打 traceback。
            # 把 stdout 指到 devnull, 避免 Python 关闭缓冲时再抛一次。
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
            return 0

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
