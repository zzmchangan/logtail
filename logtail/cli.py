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
            "  采集量: --since(优先, 时间戳二分定位覆盖全文件不受大小限; 二分失败退化尾部\n"
            "          8MB 扫描并 stderr 警告) 或 --history/--lines。\n"
            "  过滤:   --match/--exclude/--level/--focus/--correlate (--exclude 单独用也生效)。\n"
            "          注意: --match 'a|b' 的 | 是字面量非正则OR; 多词OR用空格/逗号或 re:(a|b)。\n"
            "          默认匹配不敏感(ERROR 命中 [Error]); 要精确(Dragon≠dragon2)加 --case-sensitive。\n"
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
            "  --wait(dump) 是backlog完成后实时跟随的时长(约1s无新行提前返回); 历史窗口读取\n"
            "  由信号驱动等完(30s硬上限), 超时 stderr 警告'读取未完成'——空结果勿当结论。\n"
            "  --ctx-same N 仅 agent+match(同进程上下文, 与 -C 全局互补); --diagnose 独立健康检查(不 tail)。\n"
            "  --focus <源名[,源名...]> 源聚焦(精确匹配, 多源逗号分隔; 未知/typo exit 2 列出可用名);\n"
            "  --summary 分源报 backlog_ready(定位哪个源没读完), 预设关联键兼容 JSON 引号写法。\n"
            "  管道纪律: | head 超量截断会偏离 exit 0/2 契约(SIGPIPE); 截断用 --lines N, 判 $? 前别接 head。\n"
            "  --correlate key=value 关联键(抽取+归一化跨源对齐; 未定义 key 回退字面子串);\n"
            "  --summary 的 correlate 自报 lines_with_key 用于判断正则是否写歪/key 是否有区分度。\n"
            "  --discover-keys 采样自报候选关联键的区分度/跨源分布(与 correlate 同视野含黑名单);\n"
            "  --anchor <epoch> 钉死 since 窗口 [anchor-since, anchor], 跨次 count 可比(需配 --since)。\n"
            "  --at 'YYYY-MM-DD HH:MM:SS' 人读时间的锚点(与 --anchor 互斥); --keep head|tail 选\n"
            "  截断保留端(链路起点在头部用 head), 超限 stderr hint 报总数。\n"
            "  动态黑名单(仅本次运行不写回, /save 才落盘): --blacklist-del DEBUG 放行 [Debug] 行、\n"
            "  --no-blacklist 全清(配 --source 一步拉起微服务视野)、--blacklist-add 临时追加。\n"
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
                   help="源聚焦: 只输出指定来源(name)的行, 支持逗号/空格分隔多源, "
                        "如 --focus scene 或 --focus clientgate,login; dx/glob 源均有效")
    p.add_argument("--correlate", default=None,
                   help="按关联键对齐: 跨源只留抽出该 id 的行, 如 --correlate player=123; "
                        "key 在 config correlation_keys/预设里=抽取+归一化, 未定义=回退字面 --trace")
    p.add_argument("--case-sensitive", action="store_true",
                   help="agent 模式所有文本匹配区分大小写(裸词/正则/黑名单/correlate 抽取)。"
                        "默认不敏感(--match ERROR 能命中 [Error]); 要精确(区分 Dragon 玩法与 "
                        "dragon2 账号)才加, 且查级别词别加")
    p.add_argument("--since", default=None,
                   help="只看最近一段时间 (如 5m/1h/30s), 按日志自带时间戳过滤; "
                        "交互与 agent 模式均可用。排查服务器启动报错时回看启动窗口。")
    p.add_argument("--anchor", type=float, default=None,
                   help="把 --since 窗口钉死在 [anchor-since, anchor] (epoch 秒), 不随最新日志"
                        "滑动 —— 跨次 --count 可比 (回归实验)。需与 --since 同用; "
                        "可用上一次 --summary 的 latest_ts 当 anchor")
    p.add_argument("--at", default=None,
                   help="人读时间形式的锚点, 如 --at '2026-08-27 10:09:12' (本地时区, 等价 --anchor); "
                        "与 --anchor 互斥, 需与 --since 同用")
    p.add_argument("--keep", choices=["head", "tail"], default="tail",
                   help="--lines 截断时保留哪端: tail=最新(默认), head=窗口头部(链路起点, "
                        "防被后面的刷屏段吃掉)。超限时 stderr 会打 hint 报总数")
    p.add_argument("--blacklist-add", default=None,
                   help="追加黑名单项(逗号/空格分隔), 仅本次运行、不写回 config")
    p.add_argument("--blacklist-del", default=None,
                   help="精确移除黑名单项(逗号/空格分隔, 按项原文大小写不敏感), 如 "
                        "--blacklist-del DEBUG —— 不切双 config 就能看 [Debug] 行; "
                        "仅本次运行、不写回 config(TUI 的 /save 才落盘)")
    p.add_argument("--no-blacklist", action="store_true",
                   help="清空全部黑名单(查微服务/sceneId 一步到位), 仅本次运行、不写回 config")
    p.add_argument("--allow", default=None,
                   help="deprecated 别名, 等价 --blacklist-del")
    p.add_argument("--discover-keys", action="store_true",
                   help="采样窗口自报候选关联键的区分度/跨源分布(JSON), 找出最佳跨服关联键")
    p.add_argument("--count", "--cnt", action="store_true",
                   help="agent dump 模式只输出命中行数, 不打印正文 (快速判断是否爆发)")
    p.add_argument("--lines", "-n", type=int, default=50,
                   help="agent 模式输出/收集的行数上限 (默认 50)")
    p.add_argument("--wait", type=float, default=2.0,
                   help="agent dump 实时跟随时长 (秒, 默认 2.0)。历史窗口读取由信号驱动"
                        "等完(30s 硬上限), 不受此参数限制")
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
    # --at: 人读时间锚点, 换算成 epoch 后与 --anchor 同语义
    if args.at is not None:
        if args.anchor is not None:
            print("logtail: --at 与 --anchor 互斥 (都是锚点, 给一个即可)", file=sys.stderr)
            return 2
        try:
            args.anchor = _parse_at(args.at)
        except ValueError as exc:
            print(f"logtail: --at 无效: {exc}", file=sys.stderr)
            return 2
    if args.anchor is not None and not since:
        print("logtail: --anchor 需与 --since 同用 (它定义的是 since 窗口的钉死锚点)",
              file=sys.stderr)
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
        # fail-fast: --focus 按配置源名精确匹配(支持逗号/空格分隔多源);
        # typo/大小写错若静默 0 行, 会伪装成"没日志"。列出可用名便于自查。
        if args.focus:
            import re as _re
            names = {s.name for s in cfg.sources}
            wanted = [w for w in _re.split(r"[,\s]+", args.focus.strip()) if w]
            bad = [w for w in wanted if w not in names]
            if bad:
                print(f"logtail: --focus 无效: {', '.join(bad)} 不在配置的日志源里 "
                      f"(精确匹配, 大小写敏感; 可用: {', '.join(sorted(names))})",
                      file=sys.stderr)
                return 2
            args.focus = wanted[0] if len(wanted) == 1 else set(wanted)
        if args.case_sensitive:
            cfg.case_sensitive = True
        if args.anchor:
            cfg.anchor = args.anchor
        # 动态黑名单三件套: --no-blacklist 清空 / --blacklist-del 精确移除(--allow 为
        # 其别名) / --blacklist-add 追加。全部仅影响本次运行, 不写回 config
        # (TUI 的 /save 才落盘, 两处语义不同, 文档须区分)。
        if args.no_blacklist or args.blacklist_del or args.blacklist_add or args.allow:
            import re as _re
            if args.no_blacklist:
                cfg.blacklist = []
            dels = set()
            for words in (args.blacklist_del, args.allow):
                if words:
                    dels |= {w.strip().strip('"').lower()
                             for w in _re.split(r"[,\s]+", words.strip()) if w.strip()}
            if dels:
                lowered = {b.strip().strip('"').lower() for b in cfg.blacklist}
                unmatched = dels - lowered
                if unmatched:
                    print(f"logtail: hint: --blacklist-del 的这些词不在黑名单里"
                          f"(检查拼写): {', '.join(sorted(unmatched))}", file=sys.stderr)
                cfg.blacklist = [b for b in cfg.blacklist
                                 if b.strip().strip('"').lower() not in dels]
            if args.blacklist_add:
                for w in _re.split(r"[,\s]+", args.blacklist_add.strip()):
                    if w and w not in cfg.blacklist:
                        cfg.blacklist.append(w)
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

    # 关联键发现: 采样窗口自报候选 key 的区分度/跨源分布 (不 tail 正文给 agent)
    if args.discover_keys:
        from .agent import discover_keys
        return discover_keys(cfg, args.lines, args.wait, since=since)

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
                        correlate=args.correlate, keep=args.keep)
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
        raise ValueError(f"--since 无效: {text!r} (仅支持单单位, 如 30s/5m/1h/90m; "
                         f"不支持复合(1h30m)/小数(1.5h)/裸数字(90))")
    n = int(m.group(1))
    unit = {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    return n * unit


_AT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
               "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S.%f")


def _parse_at(text: str) -> float:
    """把人读时间 'YYYY-MM-DD HH:MM:SS[.fff]' 解析为本地时区 epoch 秒."""
    import datetime
    s = text.strip()
    for fmt in _AT_FORMATS:
        try:
            return datetime.datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    raise ValueError(f"{text!r} (可用格式: 'YYYY-MM-DD HH:MM:SS', 本地时区)")


if __name__ == "__main__":
    sys.exit(main())
