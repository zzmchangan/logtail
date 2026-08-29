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
from collections import Counter, defaultdict
from typing import List, Optional

from .config import Config
from .correlate import CorrelationKeys, normalize
from .models import fmt_hhmmss
from .reader import LogFollower
from .rules import RulePatternError, Rule, RuleSet
from .timeline import RingBuffer

# dump 的 backlog(历史窗口)阶段硬上限 (秒): 信号驱动等待"各源读完全部历史行",
# 只受此上限约束防挂死; --wait 只管 backlog 完成后的实时跟随期。
DUMP_HARD_CAP = 30.0


def format_line(ln, text: str = None) -> str:
    """把一条 LogLine 格式化为带前缀的单行 (与交互版一致, 无颜色转义).

    text 可传截断后的正文 (见 _truncate)。
    """
    ts = ln.time_str or fmt_hhmmss(ln.ts_seconds)
    return f"{ts} {ln.source:<12} {text if text is not None else ln.text}"


def _json_line(ln, text: str = None, fields: Optional[dict] = None) -> str:
    """把一条 LogLine 序列化成单个 JSON 对象 (NDJSON), 供 agent 编程级加工.

    含 ts/ts_seconds(epoch)/source/level/text/seq, 便于按字段聚合与确定性重放;
    --fields 时额外带 "fields": {name: value} (结构化抽取, 省 AI 从长文本抠字段)。
    text 可传截断后的正文 (见 _truncate)。
    """
    rec = {
        "ts": ln.time_str or fmt_hhmmss(ln.ts_seconds),
        "ts_seconds": ln.ts_seconds,
        "source": ln.source,
        "level": ln.level,
        "text": text if text is not None else ln.text,
        "seq": ln.seq,
    }
    if fields:
        rec["fields"] = fields
    return json.dumps(rec, ensure_ascii=False)


def _truncate(text: str, max_len: int) -> str:
    """超长行截断 (大 JSON/proto dump 一条几十 KB, 吃掉输出配额)。

    截断处显式标记原始长度, agent 知道"这行没看全"而不是默默丢内容。
    """
    if max_len <= 0 or len(text) <= max_len:
        return text
    return text[:max_len] + f" ...[logtail: 行过长已截断, 原文 {len(text)} 字符]"


def _emit(ln, as_json: bool, max_len: int = 0, field_fn=None) -> str:
    """按 as_json 选择输出格式: 定宽文本 (默认) 或单行 JSON; 超长行先截断.

    field_fn(ln)->dict: --fields 时提取结构化字段, 注入 JSON 的 "fields"。
    """
    text = _truncate(ln.text, max_len)
    if as_json:
        return _json_line(ln, text, fields=(field_fn(ln) if field_fn else None))
    return format_line(ln, text)


def _field_value(ln, field: str, correlator: "CorrelationKeys") -> Optional[str]:
    """从一行抽出某字段值: 已知 key 用其 extract 正则, 未知 key 回退字面 'key='/'key:'."""
    if correlator.is_defined(field):
        return correlator.extract1(ln.text, field)
    m = re.search(r"\b" + re.escape(field) + r"[:=]\s*\"?([^\s\"\]]+)", ln.text)
    return normalize(m.group(1)) if m else None


def _build_field_fn(fields: List[str], cfg: Config):
    """构造 field_fn(ln)->dict, 用于 --fields 抽取 (None 表示不抽)."""
    if not fields:
        return None
    correlator = CorrelationKeys(cfg.correlation_keys,
                                 case_sensitive=cfg.case_sensitive)

    def field_fn(ln):
        out = {}
        for f in fields:
            v = _field_value(ln, f, correlator)
            if v is not None:
                out[f] = v
        return out

    return field_fn


def _stats_output(filtered: List, fields: List[str], top: int, cfg: Config,
                  as_json: bool, probe: List[dict], backlog_done: bool,
                  latest_ts: Optional[float],
                  correlate_info: Optional[dict] = None) -> int:
    """--stats: 对过滤后的行按源/级别/字段值聚合, 输出统计 (非正文), 返回退出码.

    filtered 已是 --count 同视野的命中行 (黑名单+级别+match/exclude/correlate+focus)。
    字段值聚合复用 correlation key 抽取; stdout 默认文本表, --json 时输出 JSON。
    """
    by_source = Counter(ln.source for ln in filtered)
    by_level = Counter(ln.level or "(none)" for ln in filtered)
    top_hist = []
    if fields:
        correlator = CorrelationKeys(cfg.correlation_keys,
                                     case_sensitive=cfg.case_sensitive)
        for f in fields:
            hist = Counter()
            for ln in filtered:
                v = _field_value(ln, f, correlator)
                if v is not None:
                    hist[v] += 1
            top_hist.append({
                "field": f,
                "values": [{"value": v, "count": c}
                           for v, c in hist.most_common(top)],
            })
    if as_json:
        rec = {"kind": "logtail.stats", "lines": len(filtered),
               "by_source": dict(by_source), "by_level": dict(by_level)}
        if top_hist:
            rec["top"] = top_hist
        print(json.dumps(rec, ensure_ascii=False))
    else:
        print(f"== logtail.stats == lines={len(filtered)}")
        print(f"source  " + "  ".join(f"{k}={c}" for k, c in by_source.most_common()))
        print(f"level   " + "  ".join(f"{k}={c}" for k, c in by_level.most_common()))
        for th in top_hist:
            print(f"top {th['field']} ({top})")
            for e in th["values"]:
                print(f"  {e['value']:<16} {e['count']}")
    _provenance(probe, False, len(filtered), latest_ts,
                backlog_complete=backlog_done, anchor=cfg.anchor or None)
    return 0


def _build_match_rules(patterns: List[str], cfg: Config) -> List[Rule]:
    """把一组查询词编译成匹配规则 (OR 语义); 复用裸词/re: 逻辑, 继承大小写开关."""
    rs = RuleSet(keywords=patterns or [],
                 case_sensitive=cfg.case_sensitive)
    return rs.list_highlights()


def _build_exclude_rules(patterns: List[str], cfg: Config) -> List[Rule]:
    """把排除词编译成规则 (命中即剔除); 复用黑名单语义, 继承大小写开关."""
    if not patterns:
        return []
    rs = RuleSet(blacklist=patterns, case_sensitive=cfg.case_sensitive)
    return rs.list_blacklist()


def _build_blk(cfg: Config) -> RuleSet:
    """构造黑名单+级别过滤的规则集 (agent 采集阶段应用), 继承大小写开关."""
    blk = RuleSet(blacklist=cfg.blacklist, case_sensitive=cfg.case_sensitive)
    if cfg.level:
        try:
            blk.set_level_filter(cfg.level)
        except ValueError:
            pass
    return blk


def _apply(ln, blk: RuleSet, matchers: List[Rule],
           excludes: List[Rule] = None, focus=None) -> bool:
    """返回该行是否应输出 (黑名单+级别剔除后, matchers 任一命中, 不命中 excludes, 且匹配 focus).

    focus 为单个源名(str)或多个源名(set) —— --focus 支持逗号分隔多源。
    """
    if blk.blocked(ln.text):
        return False
    if not blk.level_ok(ln.level):
        return False
    if focus:
        if isinstance(focus, str):
            if ln.source != focus:
                return False
        elif ln.source not in focus:
            return False
    if matchers and not any(r.matches(ln.text) for r in matchers):
        return False
    if excludes and any(r.matches(ln.text) for r in excludes):
        return False
    return True


def _focus_ok(source: str, focus) -> bool:
    """focus 为 None(不筛)/单源名(str)/多源名(set) 时, source 是否通过."""
    if not focus:
        return True
    if isinstance(focus, str):
        return source == focus
    return source in focus


def _provenance(probe: List[dict], summary: bool, out_count: int,
                latest_ts: Optional[float] = None,
                correlate_info: Optional[dict] = None,
                backlog_complete: Optional[bool] = None,
                anchor: Optional[float] = None) -> None:
    """把"发现诊断"写到 stderr, 使"空输出/0命中"能与"源压根没发现"区分开.

    这是对"exit 0 + 空输出 = 假阴性"陷阱的解法: 发现失败(0 文件 / dx 失败)时给出
    独立信号, 否则 agent 会把"源没被发现"当成"没错误"而停止排查。
    - 恒: 所有源都没发现文件, 或某源 dx 失败 -> 打人读警告到 stderr (默认开启, 不污染 stdout)。
    - summary: 额外打一条 JSON 记录到 stderr, 供 agent 程序化 cross-check; 含 latest_ts 锚点。
    - correlate_info: 关联键自报 (lines_total/lines_with_key/matched), 供 agent 判断正则是否写歪。
    - backlog_complete: 读全性自报 —— False 表示硬上限内历史窗口没读完, count 偏小勿当结论。
    - anchor: 本次窗口若被钉死, 报出锚点 (跨次可比的凭据)。
    """
    if not probe and not correlate_info:
        return
    if probe:
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
            "total_files": sum(s.get("files", 0) for s in probe),
            "matched": out_count,
        }
        if latest_ts is not None:
            rec["latest_ts"] = latest_ts
        if correlate_info:
            rec["correlate"] = correlate_info
        if backlog_complete is not None:
            rec["backlog_complete"] = backlog_complete
        if anchor is not None:
            rec["anchor"] = anchor
        print(json.dumps(rec, ensure_ascii=False), file=sys.stderr)


def _end(fail_if_empty: bool, n_out: int) -> int:
    """dump 的退出码: --fail-if-empty 时 0 命中返回 1 (供脚本/CI 判断'没有报错').

    默认 (不传此参数) 保持退出码 0 = 成功 (含 0 命中), 契约不变。
    """
    return 1 if (fail_if_empty and n_out == 0) else 0


def dump(cfg: Config, match: Optional[str], lines_n: int,
         wait: float = 2.0, context: int = 0,
         since: Optional[float] = None, count_only: bool = False,
         exclude: Optional[str] = None, summary: bool = False,
         ctx_same: int = 0, as_json: bool = False,
         focus: Optional[str] = None, correlate: Optional[str] = None,
         hard_cap: float = DUMP_HARD_CAP, keep: str = "tail",
         fail_if_empty: bool = False,
         fields: Optional[List[str]] = None,
         stats: bool = False, top: int = 5) -> int:
    """收集最近若干行(经黑名单/可选 match/时间窗), 打印后退出.

    收集分两阶段 (BUG0001: 大窗口下固定 --wait 会把"读不完"伪装成"没有"):
    - backlog 阶段: 等各源把"定位起点->文件尾"的历史窗口全部消费完
      (信号驱动, 只受 hard_cap 硬上限约束, 不受 --wait 限制);
    - 实时阶段: backlog 完成后再跟随 wait 秒 (或约 1s 无新行提前返回),
      --wait 的语义是"实时跟随时长", 不是总时长。

    context   > 0: 每条命中行连同**全局时间序**前后各 context 行一起输出。
    ctx_same  > 0: 每条命中行连同**同进程**前后各 ctx_same 行一起输出 (跳过其它进程的行)。
    focus     > 0: 只收集指定来源 (name) 的行 --- 单源聚焦, 对 dx/glob 源均有效。
    correlate > 'key=value': 跨进程按关联键对齐 (抽取+归一化), 只留抽出该 id 的行;
               key 未定义时回退字面 --trace 子串。
    since     > 0: 只看日志时间戳在 [最新日志-至今, 最新日志] 内的行 (秒)。
    count_only    : 只输出命中行数, 不打印正文 (快速判断是否爆发)。
    as_json       : 每行输出一个 JSON 对象 (NDJSON), 而非定宽文本。
    match / exclude: 逗号或空格分隔多词; exclude 命中则剔除。
    hard_cap      : backlog 阶段的硬上限 (秒), 防挂死; 超限打 stderr 警告。
    """
    matchers = _build_match_rules(_split_terms(match), cfg)
    excludes = _build_exclude_rules(_split_terms(exclude), cfg)
    blk = _build_blk(cfg)
    # 关联键: 解析 key=value, 准备抽取/归一化比对. 未知 key 回退字面 --trace.
    c_key, c_raw_val, c_val = _split_correlate(correlate)
    correlator = CorrelationKeys(cfg.correlation_keys,
                                 case_sensitive=cfg.case_sensitive)
    has_correlate = bool(correlate)

    def _correl(ln) -> bool:
        if c_key is None:
            return False
        if c_key and correlator.is_defined(c_key):
            return correlator.extract1(ln.text, c_key) == c_val
        return c_raw_val in ln.text        # 未定义/空 key -> 回退字面子串

    def _hit(ln) -> bool:
        return (_apply(ln, blk, matchers, excludes, focus)
                and (not has_correlate or _correl(ln)))

    field_fn = _build_field_fn(fields, cfg)

    # history/since 多取一些, 让上下文窗/时间窗有素材
    hist = max(lines_n, cfg.history or 0)
    needs_ctx = context or ctx_same
    if needs_ctx:
        hist += needs_ctx * 2 + 2
    follower = LogFollower(cfg.sources, history=hist, since=cfg.since,
                           anchor=cfg.anchor, encoding=cfg.encoding)
    follower.start()

    seen: List = []
    t_start = time.monotonic()
    hard_deadline = t_start + hard_cap            # backlog 阶段硬上限 (防挂死)
    live_deadline = None                          # backlog 完成后: 实时跟随截止
    idle = 0
    backlog_done = False
    backlog_secs = 0.0                            # backlog 阶段耗时 (宽窗护栏用)
    try:
        while True:
            now = time.monotonic()
            if now >= hard_deadline:
                break
            if live_deadline is not None and now >= live_deadline:
                break
            batch = follower.queue.drain()
            if batch:
                idle = 0
            for ln in batch:
                # 黑名单 + 级别剔除 + (可选)单源聚焦 (match 在最后输出时再判, 这里先收集)
                if (not blk.blocked(ln.text) and blk.level_ok(ln.level)
                        and _focus_ok(ln.source, focus)):
                    seen.append(ln)
            # backlog 信号: 各源历史窗口读完 -> 进入实时跟随期 (给 wait 秒收新行)。
            # 若中途又发现新文件 (dx 后到), 回到 backlog 阶段等它读完。
            if follower.backlog_ready():
                if live_deadline is None:
                    live_deadline = time.monotonic() + wait
                    backlog_done = True
                    backlog_secs = time.monotonic() - t_start   # backlog 阶段耗时
            else:
                if live_deadline is not None:
                    live_deadline = None
                    backlog_done = False
            if live_deadline is not None:
                if not batch:
                    idle += 1        # 实时期: ~1s 无新批提前退出
                if idle >= 20:
                    break
            time.sleep(0.05)
    finally:
        probe = follower.probe()   # stop() 会清空 _workers, 故先取
        follower.stop()

    if not backlog_done:
        # 硬上限内历史窗口没读完: 结果不完备, 必须明示且定位到具体源
        not_ready = [s.get("source", "?") for s in probe
                     if s.get("backlog_ready") is False]
        print(f"logtail: warning: 历史窗口读取未完成 ({hard_cap:g}s 硬上限内 backlog 未读完), "
              f"结果可能不完整 —— 未读完的源: {', '.join(not_ready) or '(未知)'}; "
              f"检查该源是否极慢/文件是否超大, 或拆小窗口重查", file=sys.stderr)
    elif since and since > 21600 and backlog_secs > 2.0:
        # P1 宽窗护栏: 主动提示优于文档警告 (agent 对手册警告执行力弱)
        print(f"logtail: hint: 宽窗(--since > 6h)读取耗时 {backlog_secs:.1f}s —— "
              f"建议拆小窗口(≤90m)分段探针, 先窄后宽定位再展开", file=sys.stderr)

    seen.sort(key=lambda l: (l.ts_key, l.seq))

    # 时间窗锚点: 以"最新一条日志的时间戳"为参考, 而非 wall-clock (time.time()).
    # 这样实时 tail 与历史 --date 扫描都正确; 锚点也暴露给 summary, 供 agent 自校验。
    # --anchor 时窗口钉死 [anchor-since, anchor]: 跨次运行可比 (回归实验),
    # 追加的新行不进来 (双边夹), 不再随最新时间戳滑动。
    latest_ts = max((ln.ts_seconds for ln in seen), default=None)
    if cfg.anchor > 0 and since and since > 0 and seen:
        cutoff = cfg.anchor - since
        seen = [ln for ln in seen
                if cutoff - 1 <= ln.ts_seconds <= cfg.anchor + 1]
    elif since and since > 0 and seen and latest_ts is not None:
        cutoff = latest_ts - since
        seen = [ln for ln in seen if ln.ts_seconds >= cutoff - 1]

    # 关联键自报: 统计该 key 在窗口内的覆盖度 (供 agent 判断正则是否写歪/该 id 是否跨进程存在)
    correlate_info = None
    if has_correlate:
        lines_total = len(seen)
        if c_key and correlator.is_defined(c_key):
            lines_with_key = sum(1 for ln in seen
                                 if correlator.extract1(ln.text, c_key) is not None)
            if lines_with_key == 0 and seen:
                print(f"warning: 关联键 '{c_key}' 在窗口内未抽到任何值 (共 {lines_total} 行) -> "
                      "检查该 key 的 extract 正则, 或该 id 是否真的跨进程存在", file=sys.stderr)
        else:
            lines_with_key = lines_total   # 未定义/回退字面: 无法统计"含 key"
        correlate_info = {"key": c_key, "value": c_val,
                          "lines_total": lines_total,
                          "lines_with_key": lines_with_key}

    # 选出命中行 (或全部重新过滤) 并决定输出
    if stats:
        # --stats: 输出统计而非正文 (同 --count 视野, 复用命中过滤)
        filtered = [ln for ln in seen if _hit(ln)]
        return _stats_output(filtered, fields or [], top, cfg, as_json,
                             probe, backlog_done, latest_ts, correlate_info)

    if count_only:
        # count 不管有无 match: 统计通过 黑名单+级别+match/exclude+correlate 的行数
        n_hit = sum(1 for ln in seen if _hit(ln))
        print(n_hit)
        if correlate_info:
            correlate_info["matched"] = n_hit
        _provenance(probe, summary, n_hit, latest_ts, correlate_info,
                    backlog_complete=backlog_done, anchor=cfg.anchor or None)
        return _end(fail_if_empty, n_hit)

    if not matchers and not has_correlate and not excludes:
        out = seen[:lines_n] if keep == "head" else seen[-lines_n:]
        if len(seen) > len(out):
            print(f"logtail: hint: 窗口共 {len(seen)} 行, --lines {lines_n} 只输出"
                  f"{'前' if keep == 'head' else '后'} {len(out)} 条 "
                  f"(调大 --lines 或 --keep head|tail 切换保留端)", file=sys.stderr)
        for ln in out:
            print(_emit(ln, as_json, cfg.max_line_len, field_fn))
        _provenance(probe, summary, len(out), latest_ts,
                    backlog_complete=backlog_done, anchor=cfg.anchor or None)
        return _end(fail_if_empty, len(out))

    hit_idx = [i for i, ln in enumerate(seen) if _hit(ln)]
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
    elif context > 0:
        for i in hit_idx:
            lo = max(0, i - context)
            hi = min(len(seen), i + context + 1)
            for j in range(lo, hi):
                if j not in out_idx:
                    out_idx.append(j)
        out_idx.sort()
    else:
        out_idx = list(hit_idx)

    # 统一截断点: --keep head 保留窗口头部(链路起点), 默认 tail 保留最新
    if len(out_idx) > lines_n:
        total = len(out_idx)
        out_idx = out_idx[:lines_n] if keep == "head" else out_idx[-lines_n:]
        print(f"logtail: hint: 命中 {len(hit_idx)} 条(含上下文共 {total} 行), "
              f"--lines {lines_n} 只输出{'前' if keep == 'head' else '后'} {len(out_idx)} 行 "
              f"(调大 --lines 或 --keep head|tail 切换保留端)", file=sys.stderr)

    for i in out_idx:
        print(_emit(seen[i], as_json, cfg.max_line_len, field_fn))
    if correlate_info:
        correlate_info["matched"] = len(out_idx)
    _provenance(probe, summary, len(out_idx), latest_ts, correlate_info,
                backlog_complete=backlog_done, anchor=cfg.anchor or None)
    return _end(fail_if_empty, len(out_idx))


def _split_terms(text: Optional[str]) -> List[str]:
    """把 match/exclude 拆成多词: 空格或逗号分隔."""
    if not text:
        return []
    return [t for t in re.split(r"[,\s]+", text.strip()) if t]


def _split_correlate(text: Optional[str]):
    """把 --correlate 'key=value' 拆成 (key, 原始值, 归一化值).

    无 '=' 时返回 (None, text, None) -> 由调用方回退字面子串 (与 --trace 一致)。
    """
    if not text:
        return None, None, None
    if "=" not in text:
        return None, text, None
    key, _, raw = text.partition("=")
    return key.strip(), raw.strip(), normalize(raw)


# 候选关联键发现用的内置正则集 (大小写不敏感; 配置 correlation_keys 同名覆盖)。
# 目的不是猜哪个 id 是"对的", 而是把每个候选的区分度/跨源分布摆出来让使用者挑。
_DISCOVER_CANDIDATES = [
    ("player", [r'"?guid"?[:=] *"?(\d+)', r'"?roleid"?[:=] *"?(\d+)',
                r'"?player"?[:=] *"?(\d+)', r'"?userid"?[:=] *"?(\d+)']),
    ("session", [r"[?&\s]s=([0-9a-zA-Z]+)"]),
    ("uid", [r"\buid[:=] *(\w+)"]),
    ("request", [r"\brequest_?id[:=] *(\w+)", r"\breqid[:=] *(\w+)"]),
    ("call", [r"\bcall_?id[:=] *(\w+)"]),
    ("order", [r"\border_?id[:=] *(\w+)"]),
    ("scene", [r'"?scene(?:_?id)?"?[:=] *"?(\w+)']),
    ("instance", [r"\binstance_?id[:=] *(\w+)"]),
]


def discover_keys(cfg: Config, lines_n: int, wait: float = 2.0,
                  since: Optional[float] = None,
                  hard_cap: float = DUMP_HARD_CAP) -> int:
    """采样窗口, 自报各候选关联键的区分度与跨源分布 (stdout 一行 JSON).

    对每个候选 key 抽取+归一化, 报告 {lines_with_key, distinct_values,
    sources, sample_values}: distinct 少且覆盖满 = 全服常量无区分度;
    多源出现 + distinct 高 = 好的跨服关联键。数据里有没有好 key 由日志决定,
    工具负责把它找出来摆好看。
    """
    follower = LogFollower(cfg.sources, history=max(lines_n, cfg.history or 0),
                           since=cfg.since, encoding=cfg.encoding)
    follower.start()
    # 与 correlate 同一视野: 应用黑名单/级别过滤 —— 报告的数字就是
    # --correlate 实际能看到的, 避免"discover 说很好、correlate 全 0"的错位。
    blk = _build_blk(cfg)
    seen: List = []
    hard_deadline = time.monotonic() + hard_cap
    live_deadline = None
    try:
        while True:
            now = time.monotonic()
            if now >= hard_deadline:
                break
            if live_deadline is not None and now >= live_deadline:
                break
            batch = follower.queue.drain()
            for ln in batch:
                if not blk.blocked(ln.text) and blk.level_ok(ln.level):
                    seen.append(ln)
            if follower.backlog_ready():
                if live_deadline is None:
                    live_deadline = time.monotonic() + wait
            else:
                live_deadline = None
            time.sleep(0.05)
    finally:
        probe = follower.probe()
        follower.stop()

    seen.sort(key=lambda l: (l.ts_key, l.seq))
    if since and since > 0 and seen:
        latest = max(l.ts_seconds for l in seen)
        seen = [l for l in seen if l.ts_seconds >= latest - since - 1]

    cands = dict(_DISCOVER_CANDIDATES)
    for k in cfg.correlation_keys:
        name = str(k.get("name", "")).strip()
        if name:
            cands[name] = list(k.get("extract") or [])

    keys_out = []
    for name, patterns in cands.items():
        ck = CorrelationKeys([{"name": name, "extract": patterns}],
                             presets=False, case_sensitive=cfg.case_sensitive)
        values: dict = {}
        sources = set()
        n = 0
        for ln in seen:
            v = ck.extract1(ln.text, name)
            if v is None:
                continue
            n += 1
            values[v] = values.get(v, 0) + 1
            sources.add(ln.source)
        keys_out.append({
            "key": name,
            "lines_with_key": n,
            "distinct_values": len(values),
            "sources": sorted(sources),
            "sample_values": list(values)[:3],
        })

    print(json.dumps({
        "kind": "logtail.discover_keys",
        "lines_total": len(seen),
        "keys": keys_out,
    }, ensure_ascii=False))
    _provenance(probe, False, len(seen), None)
    return 0


def monitor(cfg: Config, match: Optional[str], lines_n: int,
            exclude: Optional[str] = None, summary: bool = False,
            as_json: bool = False, focus: Optional[str] = None,
            correlate: Optional[str] = None, separator: bool = False,
            fields: Optional[List[str]] = None) -> int:
    """持续把过滤后的日志打到 stdout, 直到 Ctrl+C.

    separator: 源切换时插入 '──[source]──' 分隔行 (仅文本模式, 便于人肉扫管道)。
    fields   : --fields 抽取, 注入 --json 的 "fields"。
    """
    matchers = _build_match_rules(_split_terms(match), cfg)
    excludes = _build_exclude_rules(_split_terms(exclude), cfg)
    blk = _build_blk(cfg)
    c_key, c_raw_val, c_val = _split_correlate(correlate)
    correlator = CorrelationKeys(cfg.correlation_keys,
                                 case_sensitive=cfg.case_sensitive)
    field_fn = _build_field_fn(fields, cfg)

    def _correl(ln) -> bool:
        if c_key is None:
            return c_raw_val is None or c_raw_val in ln.text
        if correlator.is_defined(c_key):
            return correlator.extract1(ln.text, c_key) == c_val
        return c_raw_val in ln.text        # 未定义 key -> 回退字面子串

    follower = LogFollower(cfg.sources, history=cfg.history or lines_n,
                           since=cfg.since, encoding=cfg.encoding)
    follower.start()
    warned = False
    graces = 0
    last_source = None
    try:
        while True:
            batch = follower.queue.drain()
            for ln in batch:
                if (_apply(ln, blk, matchers, excludes, focus)
                        and (not correlate or _correl(ln))):
                    if (separator and not as_json and last_source is not None
                            and ln.source != last_source):
                        print(f"──────[{ln.source}]──────")
                        sys.stdout.flush()
                    print(_emit(ln, as_json, cfg.max_line_len, field_fn))
                    sys.stdout.flush()
                    last_source = ln.source
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
