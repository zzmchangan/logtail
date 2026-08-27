"""AI Agent 用非交互输出模式 (无 curses、无终端转义).

供 Agent 在修 bug 时拿『过滤后、少量』的日志, 而不是全量刷屏。两种形态:
 - dump:    一次性收集最近 N 行(经黑名单过滤, 可选 --match 只留命中行), 打印后退出。
 - monitor: 持续把过滤后的日志打到 stdout (可管道给 grep/head), 直到被 Ctrl+C 终止。

两者共用同一套过滤: 黑名单(必)、match(可选)。match 复用关键词的写法
 (裸词=大小写不敏感子串, re: 前缀=正则)。
"""

from __future__ import annotations

import json
import re
import sys
import time
from bisect import bisect_left
from collections import defaultdict
from typing import List, Optional

from .config import Config
from .models import fmt_hhmmss
from .reader import LogFollower
from .rules import RulePatternError, Rule, RuleSet
from .timeline import RingBuffer


def format_line(ln) -> str:
    """把一条 LogLine 格式化为带前缀的单行 (与交互版一致, 无颜色转义)."""
    ts = ln.time_str or fmt_hhmmss(ln.ts_seconds)
    return f"{ts} {ln.source:<12} {ln.text}"


def _json_line(ln) -> str:
    """把一条 LogLine 序列化成单个 JSON 对象 (NDJSON), 供 agent 编程级加工.

    含 ts/ts_seconds(epoch)/source/level/text/seq, 便于按字段聚合与确定性重放。
    """
    return json.dumps({
        "ts": ln.time_str or fmt_hhmmss(ln.ts_seconds),
        "ts_seconds": ln.ts_seconds,
        "source": ln.source,
        "level": ln.level,
        "text": ln.text,
        "seq": ln.seq,
    }, ensure_ascii=False)


def _emit(ln, as_json: bool) -> str:
    """按 as_json 选择输出格式: 定宽文本 (默认) 或单行 JSON."""
    return _json_line(ln) if as_json else format_line(ln)


def _build_match_rules(patterns: List[str]) -> List[Rule]:
    """把一组查询词编译成匹配规则 (OR 语义); 复用裸词/re: 逻辑."""
    rs = RuleSet(keywords=patterns or [])
    return rs.list_highlights()


def _build_exclude_rules(patterns: List[str]) -> List[Rule]:
    """把排除词编译成规则 (命中即剔除); 复用黑名单语义."""
    if not patterns:
        return []
    rs = RuleSet(blacklist=patterns)
    return rs.list_blacklist()


def _build_blk(cfg: Config) -> RuleSet:
    """构造黑名单+级别过滤的规则集 (agent 采集阶段应用)."""
    blk = RuleSet(blacklist=cfg.blacklist)
    if cfg.level:
        try:
            blk.set_level_filter(cfg.level)
        except ValueError:
            pass
    return blk


def _apply(ln, blk: RuleSet, matchers: List[Rule],
           excludes: List[Rule] = None, focus: Optional[str] = None) -> bool:
    """返回该行是否应输出 (黑名单+级别剔除后, matchers 任一命中, 不命中 excludes, 且匹配 focus)."""
    if blk.blocked(ln.text):
        return False
    if not blk.level_ok(ln.level):
        return False
    if focus and ln.source != focus:
        return False
    if matchers and not any(r.matches(ln.text) for r in matchers):
        return False
    if excludes and any(r.matches(ln.text) for r in excludes):
        return False
    return True


def _provenance(probe: List[dict], summary: bool, out_count: int,
                latest_ts: Optional[float] = None) -> None:
    """把"发现诊断"写到 stderr, 使"空输出/0命中"能与"源压根没发现"区分开.

    这是对"exit 0 + 空输出 = 假阴性"陷阱的解法: 发现失败(0 文件 / dx 失败)时给出
    独立信号, 否则 agent 会把"源没被发现"当成"没错误"而停止排查。
    - 恒: 所有源都没发现文件, 或某源 dx 失败 -> 打人读警告到 stderr (默认开启, 不污染 stdout)。
    - summary: 额外打一条 JSON 记录到 stderr, 供 agent 程序化 cross-check; 含 latest_ts 锚点。
    """
    if not probe:
        return
    total_files = sum(s.get("files", 0) for s in probe)
    discovered = any(s.get("discovered") for s in probe)
    dx_fail = [s["source"] for s in probe if s.get("dx_error")]
    if not discovered:
        msg = f"warning: 未发现任何日志文件 (共 {len(probe)} 源)"
        if dx_fail:
            msg += f"; dx 源失败: {', '.join(dx_fail)}"
        msg += " —— 输出为空 ≠ '无错误', 请先排查日志源 (dx 能否返回路径/目录是否为空)"
        print(msg, file=sys.stderr)
    elif dx_fail:
        print(f"warning: 以下源未发现日志/dx 失败: {', '.join(dx_fail)}", file=sys.stderr)
    if summary:
        rec = {
            "kind": "logtail.summary",
            "sources": probe,
            "total_files": total_files,
            "matched": out_count,
        }
        if latest_ts is not None:
            rec["latest_ts"] = latest_ts
        print(json.dumps(rec, ensure_ascii=False), file=sys.stderr)


def dump(cfg: Config, match: Optional[str], lines_n: int,
         wait: float = 2.0, context: int = 0,
         since: Optional[float] = None, count_only: bool = False,
         exclude: Optional[str] = None, summary: bool = False,
         ctx_same: int = 0, as_json: bool = False,
         focus: Optional[str] = None) -> int:
    """收集最近若干行(经黑名单/可选 match/时间窗), 打印后退出.

    context   > 0: 每条命中行连同**全局时间序**前后各 context 行一起输出。
    ctx_same  > 0: 每条命中行连同**同进程**前后各 ctx_same 行一起输出 (跳过其它进程的行)。
    focus     > 0: 只收集指定来源 (name) 的行 --- 单源聚焦, 对 dx/glob 源均有效。
    since     > 0: 只看日志时间戳在 [最新日志-至今, 最新日志] 内的行 (秒)。
    count_only    : 只输出命中行数, 不打印正文 (快速判断是否爆发)。
    as_json       : 每行输出一个 JSON 对象 (NDJSON), 而非定宽文本。
    match / exclude: 逗号或空格分隔多词; exclude 命中则剔除。
    """
    matchers = _build_match_rules(_split_terms(match))
    excludes = _build_exclude_rules(_split_terms(exclude))
    blk = _build_blk(cfg)
    # history/since 多取一些, 让上下文窗/时间窗有素材
    hist = max(lines_n, cfg.history or 0)
    needs_ctx = context or ctx_same
    if needs_ctx:
        hist += needs_ctx * 2 + 2
    follower = LogFollower(cfg.sources, history=hist, since=cfg.since)
    follower.start()

    seen: List = []
    deadline = time.monotonic() + wait
    idle = 0
    saw_batch = False        # 是否已收到过任何批 (区分"正在初始化"与"日志确已静默")
    try:
        while time.monotonic() < deadline:
            batch = follower.queue.drain()
            if batch:
                saw_batch = True
            for ln in batch:
                # 黑名单 + 级别剔除 + (可选)单源聚焦 (match 在最后输出时再判, 这里先收集)
                if (not blk.blocked(ln.text) and blk.level_ok(ln.level)
                        and (not focus or ln.source == focus)):
                    seen.append(ln)
            if seen:
                idle = 0
            elif saw_batch:
                idle += 1        # 已越过初始化、开始收到批 -> 才开始计空闲
            else:
                idle = 0         # 尚未收到任何批 (如 dx 子进程尚未出首行), 耐心等待
            if idle >= 20:       # ~1s 无有效行 (且已越过初始化), 提前退出
                break
            time.sleep(0.05)
    finally:
        probe = follower.probe()   # stop() 会清空 _workers, 故先取
        follower.stop()

    seen.sort(key=lambda l: (l.ts_key, l.seq))

    # 时间窗锚点: 以"最新一条日志的时间戳"为参考, 而非 wall-clock (time.time()).
    # 这样实时 tail 与历史 --date 扫描都正确; 锚点也暴露给 summary, 供 agent 自校验。
    latest_ts = max((ln.ts_seconds for ln in seen), default=None)
    if since and since > 0 and seen and latest_ts is not None:
        cutoff = latest_ts - since
        seen = [ln for ln in seen if ln.ts_seconds >= cutoff - 1]

    # 选出命中行 (或全部重新过滤) 并决定输出
    if count_only:
        # count 不管有无 match: 统计通过 黑名单+级别+match/exclude 的行数
        n_hit = sum(1 for ln in seen if _apply(ln, blk, matchers, excludes, focus))
        print(n_hit)
        _provenance(probe, summary, n_hit, latest_ts)
        return 0

    if not matchers:
        out = seen[-lines_n:]           # 无 match: 输出最近 lines_n 行
        for ln in out:
            print(_emit(ln, as_json))
        _provenance(probe, summary, len(out), latest_ts)
        return 0
    else:
        hit_idx = [i for i, ln in enumerate(seen)
                   if _apply(ln, blk, matchers, excludes, focus)]
        out_idx: List[int] = []
        if ctx_same:
            # 同源上下文: 命中行所在进程的前后各 ctx_same 行 (其它进程的行不参与,
            # 用"该源在 seen 里的下标序列"取邻居, 天然规避跨进程交错)。
            src_idx: dict[str, List[int]] = defaultdict(list)
            for j, ln in enumerate(seen):
                src_idx[ln.source].append(j)
            added: set = set()
            for i in hit_idx:
                pos = bisect_left(src_idx[seen[i].source], i)
                lo = max(0, pos - ctx_same)
                hi = min(len(src_idx[seen[i].source]), pos + ctx_same + 1)
                for j in src_idx[seen[i].source][lo:hi]:
                    if j not in added:
                        added.add(j)
                        out_idx.append(j)
            out_idx.sort()
            out_idx = out_idx[-lines_n:] if len(out_idx) > lines_n else out_idx
        elif context > 0:
            for i in hit_idx:
                lo = max(0, i - context)
                hi = min(len(seen), i + context + 1)
                for j in range(lo, hi):
                    if j not in out_idx:
                        out_idx.append(j)
            out_idx.sort()
            out_idx = out_idx[-lines_n:] if len(out_idx) > lines_n else out_idx
        else:
            out_idx = hit_idx[-lines_n:]

        for i in out_idx:
            print(_emit(seen[i], as_json))
        _provenance(probe, summary, len(out_idx), latest_ts)
    return 0


def _split_terms(text: Optional[str]) -> List[str]:
    """把 match/exclude 拆成多词: 空格或逗号分隔."""
    if not text:
        return []
    return [t for t in re.split(r"[,\s]+", text.strip()) if t]


def monitor(cfg: Config, match: Optional[str], lines_n: int,
            exclude: Optional[str] = None, summary: bool = False,
            as_json: bool = False, focus: Optional[str] = None) -> int:
    """持续把过滤后的日志打到 stdout, 直到 Ctrl+C."""
    matchers = _build_match_rules(_split_terms(match))
    excludes = _build_exclude_rules(_split_terms(exclude))
    blk = _build_blk(cfg)
    follower = LogFollower(cfg.sources, history=cfg.history or lines_n,
                           since=cfg.since)
    follower.start()
    warned = False
    graces = 0
    try:
        while True:
            batch = follower.queue.drain()
            for ln in batch:
                if _apply(ln, blk, matchers, excludes, focus):
                    print(_emit(ln, as_json))
                    sys.stdout.flush()
            graces += 1
            # 一次性提示: 启动约 1s 后若仍无任何发现 (dx 慢/失败/空目录), 打诊断到 stderr
            if not warned and graces >= 20:
                _provenance(follower.probe(), summary, out_count=0)
                warned = True
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        follower.stop()
    return 0
