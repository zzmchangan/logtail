"""curses 全屏交互界面.

上方为实时滚动日志区,下方为固定输入行 + 状态栏。
主循环每 120ms 刷新: 排空 reader 队列 -> 黑名单过滤 -> 时间线排序 ->
按当前显示模式渲染 scrollback。

命令在输入行里解析执行, 支持 /keyword /k、/clear、/remove、-C N、/context N、
/all、/pause、/resume、/blacklist、/unblacklist、/list、/save、/reset、
/help、/quit|/q 与 Ctrl+C。
"""

from __future__ import annotations

import bisect
import curses
import os
import re
import subprocess
import time
import unicodedata
from datetime import datetime
from typing import List, Optional, Sequence, Set, Tuple

from . import __version__
from .agent import format_line
from .config import Config, ConfigError, load_config, save_config
from .models import LogLine, PALETTE_FG_COLORS, ColorPool, fmt_hhmmss
from .reader import LogFollower
from .rules import RulePatternError, RuleSet
from .timeline import MODE_ALL, MODE_CONTEXT, MODE_TRACE, Timeline

TICK_MS = 120                      # 主循环刷新间隔 (ms), 对应"延迟 < 200ms"

# 每帧最多排空并处理的日志行数。洪峰时一次性几万行会卡死主循环 (feed 高亮 +
# 排序全是主线程单帧做), 限流后每帧只消化这么多行, 界面保持可响应, 其余留在队列
# 下帧继续。代价是超大洪峰下"底部追新"会略滞后, 但可交互优先。
DRAIN_BATCH = 20000

DEFAULT_CONTEXT_N = 5

# ncurses 的 button5 (滚轮下) 真实值; Python curses 未导出 BUTTON5 常量。
_BUTTON5 = 0x200000


class Tui:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.ruleset = RuleSet(keywords=cfg.keywords, blacklist=cfg.blacklist)
        self.timeline = Timeline(self.ruleset)
        self.timeline.set_context_n(cfg.context_n or DEFAULT_CONTEXT_N)
        self.follower = LogFollower(cfg.sources, history=cfg.history,
                                    since=cfg.since, encoding=cfg.encoding)
        if cfg.level:
            try:
                self.ruleset.set_level_filter(cfg.level)
            except ValueError:
                pass
        if cfg.trace:
            self.timeline.trace_term = cfg.trace
            self.timeline.set_mode(MODE_TRACE)

        self.color_pool = ColorPool()
        self._pair_by_idx: dict[int, int] = {}   # palette idx -> curses pair
        self._fg_attr: dict[str, int] = {}       # color name -> curses attr
        self.has_color = False

        # 显示与交互状态
        self.paused = False
        self._pending: List = []                 # pause 期间积压的行
        self.screen = None
        # 滚动用"内容锚点"而非绝对行号: 一旦用户向上滚, 把**某条日志行**钉在视口
        # 顶端; 新日志追加或环形缓冲从头部淘汰旧行时, 按对象身份重新定位该行,
        # 冻住的内容不漂移。None 表示跟随底部 (最新)。
        self.view_anchor: Optional[LogLine] = None   # 冻结时钉在顶部的日志行
        self.view_offset: int = 0                     # 该行折行后从第几段开始显示
        self.need_refresh = False                # 是否有新行, 重置自动回底

        # 每源开关 (/source): muted=静音源集合, only=只看该源 (显示层过滤, 照读照存)
        self.muted_sources: Set[str] = set()
        self.only_source: Optional[str] = None

        # 搜索状态: 回车裁决 (命令->执行, 非命令->搜索); n/N 上下跳
        self.search_active = False
        self.search_pat = ""
        self.search_rule = None               # 编译后的匹配规则
        self.search_idx = -1                  # 当前搜到的索引 (环形缓冲内)
        self._hit_line = None                 # 当前搜索命中的 LogLine (渲染时反显)

        # 命令历史: 输入框非空时 ↑/↓ 取上/下一条
        self._history: List[str] = []
        self._hist_idx: Optional[int] = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def run(self) -> int:
        self.follower.start()
        try:
            curses.wrapper(self._main)
        except KeyboardInterrupt:
            pass
        finally:
            self.follower.stop()
        return 0

    def _init_colors(self, stdscr) -> None:
        if not curses.has_colors():
            return
        curses.start_color()
        curses.use_default_colors()          # bg = 终端默认
        self.has_color = True
        name_to_c = {
            "red": curses.COLOR_RED,
            "green": curses.COLOR_GREEN,
            "yellow": curses.COLOR_YELLOW,
            "cyan": curses.COLOR_CYAN,
            "magenta": curses.COLOR_MAGENTA,
            "blue": curses.COLOR_BLUE,
            "bright_red": curses.COLOR_RED,
            "bright_green": curses.COLOR_GREEN,
            "bright_yellow": curses.COLOR_YELLOW,
            "bright_cyan": curses.COLOR_CYAN,
            "bright_magenta": curses.COLOR_MAGENTA,
            "bright_blue": curses.COLOR_BLUE,
        }
        bright = {"bright_red", "bright_green", "bright_yellow", "bright_cyan",
                  "bright_magenta", "bright_blue"}
        # 为每个调色板前景色建一个颜色对; 存的是 color_pair() 的属性掩码
        for idx, name in enumerate(PALETTE_FG_COLORS):
            try:
                curses.init_pair(idx + 1, name_to_c[name], -1)
                attr = curses.color_pair(idx + 1)
                self._pair_by_idx[idx] = attr
                self._fg_attr.setdefault(name, attr)
            except curses.error:
                pass
        for name in bright:
            if name in self._fg_attr:
                self._fg_attr[name] |= curses.A_BOLD

    def _attr_for(self, rule) -> int:
        """返回某高亮规则对应的 curses 属性 (color_pair 掩码)."""
        if not self.has_color:
            return 0
        idx = self.color_pool.color_for(rule.rule_id)
        return self._pair_by_idx.get(idx, 0)

    def _search_attr(self) -> int:
        """搜索词专属醒目属性: 加粗 + 反显, 让命中的关键词一眼可见."""
        if not self.has_color:
            return curses.A_BOLD | curses.A_REVERSE
        return curses.A_BOLD | curses.A_REVERSE

    _LEVEL_COLOR = {"ERROR": "red", "FATAL": "red", "WARN": "yellow",
                    "INFO": "cyan", "DEBUG": "blue", "TRACE": "blue"}

    def _level_attr(self, level: str) -> int:
        """级别自动着色: ERROR/FATAL 红, WARN 黄, 其余青/蓝; 无级别或终端不支持返回 0."""
        if not self.has_color or not level:
            return 0
        name = self._LEVEL_COLOR.get(level.upper())
        return self._fg_attr.get(name, 0) if name else 0

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def _main(self, stdscr) -> None:
        self.screen = stdscr
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.timeout(TICK_MS)
        self._init_colors(stdscr)
        self._resize_ok(stdscr)
        try:
            # 启用滚轮 (鼠标) 事件
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
        except (curses.error, AttributeError):
            pass

        buf = ""
        msg = f"logtail v{__version__}  (/{'help'} 查看命令)"
        while True:
            # 排空队列
            self._drain()
            # 渲染
            self._render(stdscr, buf, msg)
            # 处理输入
            key = stdscr.getch()
            if key == -1:
                continue
            action, buf, msg = self._handle_key(key, buf)
            if action == "quit":
                return
            if action == "clear_input":
                buf = ""
                continue
            if action == "execute":
                buf, msg = self._execute(buf, msg)

    # ------------------------------------------------------------------
    # 数据
    # ------------------------------------------------------------------
    def _drain(self) -> None:
        batch = self.follower.queue.take_upto(DRAIN_BATCH)
        if not batch:
            return
        if self.paused:
            # 冻结显示, 但把错过的行先攒起来, 恢复时补回
            self._pending.extend(batch)
            return
        self._apply(batch)

    def _apply(self, batch) -> None:
        # 黑名单 + 级别过滤 (采集阶段丢弃)
        kept = [ln for ln in batch
                if not self.ruleset.blocked(ln.text)
                and self.ruleset.level_ok(ln.level)]
        self.timeline.feed(kept)
        self.need_refresh = True

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------
    def _resize_ok(self, stdscr) -> None:
        try:
            curses.resize_term(0, 0)
        except curses.error:
            pass

    def _render(self, stdscr, buf: str, msg: str) -> None:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        if h <= 0 or w <= 0:
            return
        input_h = 1
        status_h = 1
        log_h = max(0, h - input_h - status_h)

        visible = self._vis()
        # 只折可见窗口 (window-bounded): 环形缓冲最大 20000 行, 若每帧全折一遍
        # wrap_text 要 300ms+, 主循环直接卡死。这里只折视口需要的那几行。
        rows = self._window(visible, log_h, w)
        # 冻结时视口没被填满 = 锚点行已贴近缓冲末尾 -> 自动解除冻结 (回到跟随底部)
        if self.view_anchor is not None and len(rows) < log_h:
            self.view_anchor, self.view_offset = None, 0
            rows = self._window(visible, log_h, w)
        total = len(visible)
        self.need_refresh = False

        for row, (prefix, seg, hl_rules, dim, level, is_hit, _line) in enumerate(rows):
            if row >= log_h:
                break
            self._render_display_row(stdscr, row, prefix, seg, hl_rules, dim, w, level, is_hit)

        pause_marker = " PAUSED" if self.paused else ""
        freeze_marker = " FREEZE" if self.view_anchor is not None else ""
        mode = "context|{n}".format(n=self.timeline.context_n) if self.timeline.mode == MODE_CONTEXT else "all"
        n_kw = len(self.ruleset.list_highlights())
        search_marker = f"  /{self.search_pat}{self.search_idx if self.search_idx>=0 else '×'} n/N↑↓" if self.search_active else ""
        # 冻结且已到缓冲最顶时, 提示还有 N 行更早日志已被环形缓冲丢弃 (A6)
        discarded = self.timeline.fed_total - len(self.timeline.ring)
        top_marker = ""
        if (self.timeline.mode == MODE_ALL and self.view_anchor is not None
                and self.timeline.ring._items
                and self.view_anchor is self.timeline.ring._items[0][0]
                and discarded > 0):
            top_marker = f" ·已丢弃{discarded}行更早"
        status = f" mode={mode} hl={n_kw} lines={total}{pause_marker}{freeze_marker}{top_marker}{search_marker}   {msg}"
        status = strip_nul(status)
        buf = strip_nul(buf)
        try:
            stdscr.addnstr(h - 2, 0, status[: w - 1], w - 1,
                           curses.A_REVERSE if self.paused else 0)
            stdscr.addnstr(h - 1, 0, buf[: w - 1], w - 1)
            stdscr.move(h - 1, min(len(buf), w - 1))
        except curses.error:
            pass
        stdscr.refresh()

    # ------------------------------------------------------------------
    # 视口: 内容锚定的窗口折行 (window-bounded)
    # ------------------------------------------------------------------
    def _term_w(self) -> int:
        """终端宽度 (列); 无真实屏幕时回退 80."""
        if self.screen:
            try:
                _, w = self.screen.getmaxyx()
                return max(1, w)
            except curses.error:
                pass
        return 80

    # ------------------------------------------------------------------
    # 每源开关 (/source): 显示层过滤
    # ------------------------------------------------------------------
    def _source_kept(self, ln: LogLine) -> bool:
        """该行来源是否应显示 (only 优先, 其次 not muted)."""
        if self.only_source is not None:
            return ln.source == self.only_source
        return ln.source not in self.muted_sources

    def _vis(self) -> List:
        """当前应显示的可见行列表 (含 /source 过滤), 供渲染与滚动共用."""
        return [(ln, hl, dim) for ln, hl, dim in self.timeline.visible()
                if self._source_kept(ln)]

    def _segments(self, line: LogLine, w: int) -> List[str]:
        """某条日志行在宽度 w 下折成的显示段 (续行无前缀)."""
        prefix = self._prefix_of(line)
        start_x = self._str_width(prefix)
        body_w = max(1, w - 1 - start_x)
        return wrap_text(line.text, body_w) or [""]

    def _row_count(self, line: LogLine, w: int) -> int:
        """该行折行后的显示行数."""
        return len(self._segments(line, w))

    def _visible_idx_of(self, visible, line: LogLine) -> Optional[int]:
        """按对象身份找 line 在可见列表中的索引; 找不到 (已淘汰) 返回 None."""
        for i, (ln, _hl, _dim) in enumerate(visible):
            if ln is line:
                return i
        return None

    def _append_rows(self, rows: List, line: LogLine, hl_rules, dim,
                     w: int, offset: int, hit_line, search_rule) -> None:
        """把一条日志行的折行段追加进 rows (要么全折, 由外层控制行数)."""
        segs = self._segments(line, w)
        offset = min(offset, max(0, len(segs) - 1))       # 折行宽度变化后夹回范围
        prefix = self._prefix_of(line)
        is_hit = hit_line is not None and line is hit_line
        row_hl = hl_rules
        if is_hit and search_rule is not None:
            row_hl = [search_rule] + list(hl_rules)
        sub = segs[offset:] if offset else segs
        for j, seg in enumerate(sub):
            pfx = prefix if (offset == 0 and j == 0) else None
            rows.append((pfx, seg, row_hl, dim, line.level, is_hit, line))

    def _window(self, visible, log_h: int, w: int) -> List:
        """返回视口要画的显示行 (window-bounded, 不折整条环形缓冲).

        每项 7 元组 (prefix_or_None, seg, hl_rules, dim, level, is_hit, line)。
        冻结(view_anchor 非 None)时锚点行钉在视口顶部 (随内容定位, 不随行号漂移);
        否则窗口贴缓冲尾部 (实时跟随)。
        """
        if log_h <= 0 or not visible:
            return []
        n = len(visible)
        hit_line, search_rule = self._hit_line, self.search_rule

        if self.view_anchor is None:
            # 跟随底部: 从后往前数行, 找起点使该处往后折出的行数 >= log_h
            acc, start = 0, n - 1
            while start > 0 and acc < log_h:
                acc += self._row_count(visible[start][0], w)
                start -= 1
            rows: List = []
            for i in range(start, n):
                line, hl_rules, dim = visible[i]
                self._append_rows(rows, line, hl_rules, dim, w, 0,
                                  hit_line, search_rule)
            return rows[-log_h:] if len(rows) > log_h else rows

        # 冻结: 锚点行钉在视口顶部
        ai = self._visible_idx_of(visible, self.view_anchor)
        if ai is None:
            # 锚点被淘汰 / 上下文窗口变化 -> 回到跟随底部
            self.view_anchor, self.view_offset = None, 0
            return self._window(visible, log_h, w)
        rows = []
        for i in range(ai, n):
            line, hl_rules, dim = visible[i]
            off = self.view_offset if i == ai else 0
            self._append_rows(rows, line, hl_rules, dim, w, off,
                              hit_line, search_rule)
            if len(rows) >= log_h:
                break
        return rows[:log_h]

    # ------------------------------------------------------------------
    # 滚动: 按"显示行"局部移动内容锚点 (不扫描全环, 只走 delta 附近的行)
    # ------------------------------------------------------------------
    def _top_of_window_bottom(self, visible, log_h: int, w: int) -> Tuple[LogLine, int]:
        """跟随底部时视口顶端所在的行 (只从末尾往回数 log_h 行, 不扫全环).

        返回 (line, offset): line 占据视口顶端的显示行, offset 为该行内第几段。
        """
        n = len(visible)
        if n == 0:
            return None, 0
        acc, i = 0, n - 1
        while i >= 0:
            rc = self._row_count(visible[i][0], w)
            if acc + rc >= log_h:
                return visible[i][0], acc + rc - log_h
            acc += rc
            i -= 1
        return visible[0][0], 0

    def _at_bottom(self, visible, i: int, off: int, w: int, log_h: int) -> bool:
        """从第 i 行第 off 段到可见列表末尾, 剩余显示行数是否 <= log_h (视口已贴底)."""
        n = len(visible)
        cnt = self._row_count(visible[i][0], w) - off
        j = i + 1
        while cnt <= log_h and j < n:
            cnt += self._row_count(visible[j][0], w)
            j += 1
        return cnt <= log_h

    def _walk_anchor(self, visible, w: int, delta: int) -> None:
        """把内容锚点在可见列表里移动 delta 个显示行 (局部遍历).

        delta > 0 = 向下 (往新), < 0 = 向上 (往回)。向下滚到底(剩余不足一屏)则解除冻结。
        """
        n = len(visible)
        i = self._visible_idx_of(visible, self.view_anchor)
        if i is None:
            self.view_anchor, self.view_offset = None, 0
            return
        off = self.view_offset
        log_h = max(1, self._log_h())
        if delta > 0:
            remaining = delta
            while remaining > 0:
                rc = self._row_count(visible[i][0], w)
                avail = rc - off
                if remaining < avail:
                    off += remaining
                    remaining = 0
                else:
                    remaining -= avail
                    i += 1
                    if i >= n:
                        self.view_anchor, self.view_offset = None, 0
                        return
                    off = 0
            if self._at_bottom(visible, i, off, w, log_h):
                self.view_anchor, self.view_offset = None, 0   # 滚到底 -> 恢复跟随
                return
        elif delta < 0:
            remaining = -delta
            while remaining > 0:
                if remaining <= off:
                    off -= remaining
                    remaining = 0
                else:
                    remaining -= off + 1
                    i -= 1
                    if i < 0:
                        i, off = 0, 0
                        remaining = 0
                    else:
                        off = self._row_count(visible[i][0], w) - 1
        self.view_anchor = visible[i][0]
        self.view_offset = max(0, off)

    def _scroll_by(self, visible, w: int, delta: int) -> None:
        """从当前视口位置移动 delta 个显示行; 到最底则解除冻结 (回到跟随底部).

        delta > 0 = 向下 (往新), < 0 = 向上 (往回)。
        跟随底部时向下无变化; 向上先冻结在"当前底窗口的顶端"再移动。
        """
        if not visible:
            self.view_anchor, self.view_offset = None, 0
            return
        if self.view_anchor is None:
            if delta >= 0:
                return                          # 已在底部, 向下滚保持跟随
            log_h = max(1, self._log_h())
            line, off = self._top_of_window_bottom(visible, log_h, w)
            if line is None:
                return
            self.view_anchor, self.view_offset = line, off
        self._walk_anchor(visible, w, delta)

    def _anchor_top(self, visible) -> None:
        """跳到最顶: 锚定第一行."""
        if visible:
            self.view_anchor, self.view_offset = visible[0][0], 0

    @staticmethod
    def _str_width(text: str) -> int:
        return sum(char_width(ch) for ch in text)

    def _render_display_row(self, stdscr, row, prefix, seg, hl_rules, dim, w,
                            level="", is_hit=False) -> None:
        prefix = strip_nul(prefix) if prefix is not None else None
        base = curses.A_DIM if (dim and self.has_color) else 0
        hit_attr = curses.A_REVERSE if is_hit else 0        # 搜索命中行反显
        start_x = 0
        if prefix is not None and is_hit:
            hit_attr |= base
        if prefix is not None:
            # 前缀 = [时间戳] 来源列; 对来源列按级别着色 (ERROR 红 / WARN 黄 ...)
            try:
                if level and self.has_color and start_x == 0:
                    self._render_prefix_colored(stdscr, row, prefix, level,
                                                base, w, is_hit)
                    start_x = self._str_width(prefix)
                else:
                    stdscr.addnstr(row, 0, prefix[: w - 1],
                                   min(w - 1, len(prefix)), base | hit_attr)
                    start_x = self._str_width(prefix)
            except curses.error:
                pass
        self._render_highlighted(stdscr, row, start_x, seg, hl_rules, dim, w,
                                 is_hit)

    def _render_prefix_colored(self, stdscr, row, prefix, level, base, w,
                               is_hit=False) -> None:
        """渲染前缀, 把来源列按级别着色 (时间戳保持不变)."""
        # prefix = "[06:20:10.100] scene       " -> ts_str + source_padded
        lattr = self._level_attr(level) | base | (curses.A_REVERSE if is_hit else 0)
        ts_end = prefix.find("]")
        ts_part = prefix[: ts_end + 1] if ts_end >= 0 else ""
        rest = prefix[ts_end + 1:] if ts_end >= 0 else prefix
        stdscr.addnstr(row, 0, ts_part[: w - 1], min(w - 1, len(ts_part)),
                       base | (curses.A_REVERSE if is_hit else 0))
        stdscr.addnstr(row, len(ts_part), rest[: w - 1 - len(ts_part)],
                       min(max(0, w - 1 - len(ts_part)), len(rest)), lattr)

    def _prefix_of(self, line) -> str:
        """返回某行的固定前缀 (时间戳 + 来源列), 供测量/渲染使用."""
        ts = line.time_str or fmt_hhmmss(line.ts_seconds)
        return f"{ts} {line.source:<12} "

    def _render_highlighted(self, stdscr, row, start_x, seg, hl_rules,
                            dim, w, is_hit=False) -> None:
        """把已折行的 seg 从 start_x 起渲染; 命中高亮词着色, 搜索命中行整体反显."""
        seg = strip_nul(seg)               # NUL 会让 addnstr 抛 ValueError
        body_w = max(0, w - 1 - start_x)
        if body_w <= 0:
            return
        basis = dim and self.has_color
        hit_attr = curses.A_REVERSE if is_hit else 0

        if not self.has_color or not hl_rules:
            base = curses.A_DIM if basis else 0
            try:
                stdscr.addnstr(row, start_x, seg, min(body_w, len(seg)),
                               base | hit_attr)
            except curses.error:
                pass
            return

        spans = []
        for rule in hl_rules:
            # 搜索规则用专属醒目属性 (加粗+反显), 其余用普通高亮色
            if self.search_rule is not None and rule is self.search_rule:
                attr = self._search_attr()
            else:
                attr = self._attr_for(rule)
            for s, e in self._match_spans(rule, seg):
                spans.append((s, e, attr))
        spans.sort(key=lambda s: (s[0], s[1] - s[0]))
        merged = []
        for s in spans:
            if merged and s[0] < merged[-1][1]:
                continue
            merged.append(s)

        base = curses.A_DIM if basis else 0
        pos = 0
        x = start_x
        try:
            for s, e, attr in merged:
                if s > pos:
                    stdscr.addnstr(row, x, seg[pos:s], max(0, w - 1 - x), base | hit_attr)
                    x += self._seg_width(seg[pos:s])
                col = attr | (curses.A_DIM if basis else 0) | hit_attr
                stdscr.addnstr(row, x, seg[s:e], max(0, w - 1 - x), col)
                x += self._seg_width(seg[s:e])
                pos = e
            if pos < len(seg):
                stdscr.addnstr(row, x, seg[pos:], max(0, w - 1 - x), base | hit_attr)
        except curses.error:
            pass

    def _seg_width(self, text: str) -> int:
        return self._str_width(text)

    @staticmethod
    def _match_spans(rule, seg_text: str):
        """返回 rule 在 seg_text 中命中的 (start,end) 区间列表."""
        low = seg_text.lower()
        if rule.is_regex:
            return [(m.start(), m.end()) for m in rule._matcher.finditer(low)]
        pat = rule.pattern.lower()
        out, pos = [], 0
        while True:
            i = low.find(pat, pos)
            if i < 0:
                break
            out.append((i, i + len(pat)))
            pos = i + 1
        return out

    # ------------------------------------------------------------------
    # 输入处理
    # ------------------------------------------------------------------
    def _handle_key(self, key, buf: str):
        """返回 (action, buf, msg). action: quit/execute/clear_input/None.

        输入框为空: ↑/↓/PgUp/滚轮 = 滚动; 非空: ↑/↓ = 命令历史。
        """
        if key == curses.KEY_MOUSE:
            return self._handle_mouse(buf)
        if key in (curses.KEY_ENTER, 10):
            # 回车: 在执行前先解冻跳到最新 (命令/搜索提交后回底)
            return "execute", buf, ""
        if key == 27:                       # ESC 清空输入 + 退出搜索态
            self.search_active = False
            self.search_idx = -1
            self._hit_line = None
            return "clear_input", "", ""
        if key in (curses.KEY_BACKSPACE, 127, curses.KEY_DC):
            return None, buf[:-1], ""

        # 输入框非空时 ↑/↓ 走命令历史
        if buf and key == curses.KEY_UP:
            return None, self._cmd_history(-1), ""
        if buf and key == curses.KEY_DOWN:
            return None, self._cmd_history(+1), ""

        # 输入框为空时 ↑/↓ 走滚动
        visible = self._vis()
        w = self._term_w()
        log_h = max(1, self._log_h())

        if key == curses.KEY_UP:            # 向上回溯一行 (进入冻结/上移锚点)
            self._scroll_by(visible, w, -1)
            return None, buf, ""
        if key == curses.KEY_DOWN:          # 向下; 到底则恢复自动跟随
            self._scroll_by(visible, w, +1)
            return None, buf, ""
        if key == curses.KEY_PPAGE:         # 上翻一页
            self._scroll_by(visible, w, -log_h)
            return None, buf, ""
        if key == curses.KEY_NPAGE:         # 下翻一页
            self._scroll_by(visible, w, +log_h)
            return None, buf, ""
        if key == 6:                        # Ctrl-F: 下翻一页 (vim 习惯, 同 PgDn)
            self._scroll_by(visible, w, +log_h)
            return None, buf, ""
        if key == 2:                        # Ctrl-B: 上翻一页 (vim 习惯, 同 PgUp)
            self._scroll_by(visible, w, -log_h)
            return None, buf, ""
        if key == curses.KEY_HOME:          # 跳到最顶
            self._anchor_top(visible)
            return None, buf, ""
        if key == curses.KEY_END:           # 跳到最底, 恢复自动跟随
            self.view_anchor, self.view_offset = None, 0
            return None, buf, ""
        # g / G: 仅当输入框为空时才当作"跳顶/跳底"快捷键 (不打断命令输入)
        if not buf and key == ord('g'):
            self._anchor_top(visible)
            return None, buf, ""
        if not buf and key == ord('G'):
            self.view_anchor, self.view_offset = None, 0   # 跳最新, 恢复跟随
            return None, buf, ""
        # 搜索已提交后 n/N 上下跳 (输入框为空时)
        if not buf and self.search_active and self.search_rule is not None:
            if key == ord('n'):
                self._do_search(self.search_rule.pattern, +1)
                return None, buf, ""
            if key == ord('N'):
                self._do_search(self.search_rule.pattern, -1)
                return None, buf, ""
        if 32 <= key < 127:
            return None, buf + chr(key), ""
        return None, buf, ""

    def _cmd_history(self, direction: int) -> str:
        """↑/↓ 取上一条/下一条命令历史; 无历史返回空."""
        if not self._history:
            return ""
        n = len(self._history)
        # 初始指针指向"最晚之后" (当前正在打的内容)
        if self._hist_idx is None:
            self._hist_idx = n
        if direction < 0:                       # ↑ 更早
            self._hist_idx = max(0, self._hist_idx - 1)
        else:                                   # ↓ 更晚
            self._hist_idx = min(n, self._hist_idx + 1)
        if self._hist_idx >= n:
            return ""                            # 已在最晚下, 回到空输入
        return self._history[self._hist_idx]

    def _handle_mouse(self, buf: str):
        """滚轮事件: 上滚=回看历史, 下滚=前进; 滚到底自动解除冻结.

        实测 (用户终端): 上滚=BUTTON4_PRESSED(0x10000), 下滚=0x200000 (button5,
        ncurses 真实值, Python curses 未导出 BUTTON5 常量)。
        为兼容多个终端, 下滚匹配 0x200000 / BUTTON4_RELEASED(经典映射) /
        BUTTON4_CLICKED(部分终端)。
        """
        try:
            _id, x, y, z, bstate = curses.getmouse()
        except (curses.error, ValueError):
            return None, buf, ""
        # 调试: 设 LOGTAIL_MOUSE_DEBUG=1 时, 把每个鼠标事件写到 /tmp/logtail_mouse.log
        if os.environ.get("LOGTAIL_MOUSE_DEBUG"):
            try:
                with open("/tmp/logtail_mouse.log", "a") as fh:
                    fh.write(f"bstate=0x{bstate:x} dec={bstate}\n")
            except OSError:
                pass
        visible = self._vis()
        w = self._term_w()
        log_h = max(1, self._log_h())
        step = max(3, log_h // 3)           # 每次滚 3 行 (或 1/3 屏)

        if bstate & curses.BUTTON4_PRESSED:        # 上滚: 回看更早
            self._scroll_by(visible, w, -step)
        elif bstate & (_BUTTON5 | curses.BUTTON4_RELEASED | curses.BUTTON4_CLICKED):
            self._scroll_by(visible, w, +step)     # 下滚: 前进
        return None, buf, ""

    def _log_h(self) -> int:
        if self.screen:
            h, _ = self.screen.getmaxyx()
            return max(0, h - 2)
        return 0

    # ------------------------------------------------------------------
    # 搜索: 回车裁决 (非命令词即搜索), n/N 上下跳
    # ------------------------------------------------------------------
    def _do_search(self, q: str, direction: int) -> None:
        """用查询词 q 搜索 scrollback, 跳到第一天命中的行."""
        from .rules import RuleSet
        if not q:
            return
        rule = None
        try:
            rule = RuleSet(keywords=[q]).list_highlights()[0]
        except Exception:
            return
        n = len(self.timeline.ring)
        # 首次搜索: 向下从末尾前一个开始 (会绕回第0), 向上从末尾开始 (绕回最后一个)
        if self.search_idx >= 0:
            start = self.search_idx
        else:
            start = n - 1 if direction > 0 else n
        idx = self.timeline.search(rule, start, direction)
        if idx >= 0:
            self.search_idx = idx
            self.search_rule = rule
            hit = self.timeline.ring._items[idx][0]      # 供渲染标记命中行
            self._hit_line = hit
            # 定位命中行: 先锚定它, 再向上走半屏, 使它大致出现在视口中部 (局部遍历)
            self.view_anchor, self.view_offset = hit, 0
            self._scroll_by(self.timeline.visible(), self._term_w(),
                            -((self._log_h() or 20) // 2))
        else:
            self.search_idx = -1
            self._hit_line = None

    def _execute(self, buf: str, msg: str):
        buf = buf.strip()
        if buf:
            self._record_history(buf)
            self.view_anchor, self.view_offset = None, 0   # 提交后回底跟随
            return "", self._dispatch(buf, msg)
        return "", msg

    def _record_history(self, cmd: str) -> None:
        if cmd and (not self._history or self._history[-1] != cmd):
            self._history.append(cmd)
        self._hist_idx = None

    def _dispatch(self, buf: str, msg: str) -> str:
        """回车裁决: 若首词是已知命令则执行, 否则作为搜索词跳第一条命中."""
        head = buf.split()[0] if buf.split() else ""
        if head in _KNOWN_COMMANDS:
            self.search_active = False
            return _run_command(self, buf)
        # 非命令 -> 当作搜索 (词可能是 json/tsv 里的值, 含 / 则剥掉)
        q = buf.lstrip("/").strip()
        if q:
            self._do_search(q, +1)
            self.search_active = True
            self.search_pat = q
            return f"搜索: {q!r} (n/N 上下跳, ESC 退出)"
        return msg


# ---------------------------------------------------------------------------
# 命令解析与执行
# ---------------------------------------------------------------------------

_EXECUTORS = {}


def command(name):
    def deco(fn):
        _EXECUTORS[name] = fn
        return fn
    return deco


_KNOWN_COMMANDS = {
    "/keyword", "/k", "/clear", "/clr", "/remove", "/rm",
    "-C", "/context", "/ctx", "/all", "--all",
    "/pause", "/resume", "/blacklist", "/bl", "/unblacklist", "/ubl",
    "/level", "/trace", "/list", "/save", "/reset", "/help", "/?", "/quit", "/q",
    "/less", "/goto", "/source",
}


def _run_command(tui: Tui, raw: str) -> str:
    """执行一条命令, 返回回显消息."""
    parts = raw.split()
    head = parts[0]

    # /keyword 或 /k: 支持一次加多个高亮词
    if head in ("/keyword", "/k"):
        return _add_keyword(tui, parts)
    if head in ("/clear", "/clr"):
        n = tui.ruleset.clear("highlight")
        tui.timeline.rehash()         # 只清除高亮着色, 保留日志行
        return f"已清除 {n} 个高亮词"
    if head in ("/remove", "/rm"):
        return _remove_keyword(tui, parts)
    if head in ("-C", "/context", "/ctx"):
        return _context(tui, parts)
    if head in ("/all", "--all"):
        tui.timeline.set_mode(MODE_ALL)
        return "全量模式"
    if head == "/pause":
        tui.paused = True
        return "已暂停 (恢复时补回错过的行)"
    if head == "/resume":
        tui.paused = False
        if tui._pending:
            tui._apply(tui._pending)
            n = len(tui._pending)
            tui._pending.clear()
            return f"已恢复, 补回 {n} 行"
        return "已恢复"
    if head in ("/blacklist", "/bl"):
        return _add_blacklist(tui, parts)
    if head in ("/unblacklist", "/ubl"):
        return _remove_blacklist(tui, parts)
    if head == "/level":
        return _set_level(tui, parts)
    if head == "/trace":
        return _set_trace(tui, parts)
    if head == "/list":
        return _list(tui)
    if head == "/less":
        return _less(tui)
    if head == "/goto":
        return _goto(tui, parts)
    if head == "/source":
        return _source_cmd(tui, parts)
    if head == "/save":
        return _save(tui)
    if head == "/reset":
        return _reset(tui)
    if head in ("/help", "/?"):
        return _HELP
    if head in ("/quit", "/q"):
        raise SystemExit(0)
    return f"未知命令: {head!r}  (输入 /help 查看帮助)"


def _require_arg(parts, what: str):
    if len(parts) < 2:
        return None
    return parts[1]


def _add_keyword(tui: Tui, parts) -> str:
    pats = parts[1:]
    if not pats:
        return "用法: /keyword <词> [<词>...]  (或 /k 词1 词2 ...)"
    added, errors = [], []
    for pat in pats:
        try:
            tui.ruleset.add(pat, "highlight")
            added.append(pat)
        except RulePatternError as exc:
            errors.append(f"{pat!r}: {exc}")
    msg = f"已添加 {len(added)} 个高亮词: {', '.join(added)}"
    if errors:
        msg += f"  |  失败: {'; '.join(errors)}"
    if added:
        tui.timeline.rehash()          # 让存量行立即按新规则着色
    return msg


def _remove_keyword(tui: Tui, parts) -> str:
    pats = parts[1:]
    if not pats:
        return "用法: /remove <词> [<词>...]  (或 /rm 词1 词2 ...)"
    removed, missing = [], []
    for pat in pats:
        if tui.ruleset.remove(pat, "highlight"):
            removed.append(pat)
        else:
            missing.append(pat)
    msg = f"已移除: {', '.join(removed)}" if removed else ""
    if missing:
        msg += (", " if msg else "") + f"未找到: {', '.join(missing)}"
    if removed:
        tui.timeline.rehash()
    return msg or "未移除任何高亮词"


def _context(tui: Tui, parts) -> str:
    val = _require_arg(parts, "N")
    if val is None:
        return f"用法: -C <N|Ns>  (或 /context N, /ctx N)  (当前窗口: {_ctx_desc(tui)})"
    spec = val
    if not tui.timeline.set_context(spec):
        return f"无效窗口: {val!r} (用 行数N 或 时间Ns/Nm, 如 -C 5 或 -C 5s)"
    tui.timeline.set_mode(MODE_CONTEXT)
    return f"上下文模式: {_ctx_desc(tui)}"


def _ctx_desc(tui: Tui) -> str:
    if tui.timeline.context_seconds > 0:
        return f"前后各 {tui.timeline.context_seconds:g} 秒"
    return f"前后各 {tui.timeline.context_n} 行"


def _add_blacklist(tui: Tui, parts) -> str:
    pats = parts[1:]
    if not pats:
        return "用法: /blacklist <规则> [<规则>...]  (或 /bl)"
    added, errors = [], []
    for pat in pats:
        try:
            tui.ruleset.add(pat, "blacklist")
            added.append(pat)
        except RulePatternError as exc:
            errors.append(f"{pat!r}: {exc}")
    msg = f"已添加 {len(added)} 个黑名单: {', '.join(added)}"
    if errors:
        msg += f"  |  失败: {'; '.join(errors)}"
    return msg


def _remove_blacklist(tui: Tui, parts) -> str:
    pats = parts[1:]
    if not pats:
        return "用法: /unblacklist <规则> [<规则>...]  (或 /ubl)"
    removed, missing = [], []
    for pat in pats:
        if tui.ruleset.remove(pat, "blacklist"):
            removed.append(pat)
        else:
            missing.append(pat)
    msg = f"已移除黑名单: {', '.join(removed)}" if removed else ""
    if missing:
        msg += (", " if msg else "") + f"未找到: {', '.join(missing)}"
    return msg or "未移除任何黑名单"


def _set_trace(tui: Tui, parts) -> str:
    """/trace player=123 只显示所有源中含该词的行 (无邻居); /trace 单独查看 / /trace off 取消."""
    if len(parts) < 2:
        return (f"当前 trace: {tui.timeline.trace_term or '(无)'}"
                f"  用法: /trace <词>  (或 /trace off)")
    arg = parts[1]
    if arg.lower() in ("off", "none", "all"):
        tui.timeline.trace_term = ""
        tui.timeline.set_mode(MODE_ALL)
        return "已取消 trace, 回全量模式"
    tui.timeline.trace_term = arg
    tui.timeline.set_mode(MODE_TRACE)
    return f"trace: 只显示含 {arg!r} 的行 (跨源纯净追踪)"


def _set_level(tui: Tui, parts) -> str:
    """/level ERROR 只保留 >= ERROR 的行; /level 单用显示当前; /level all 取消."""
    if len(parts) < 2:
        cur = tui.ruleset.min_level or "all"
        return f"当前级别过滤: {cur}  (用法: /level ERROR / WARN / all)"
    arg = parts[1].lower()
    if arg in ("all", "none", "off"):
        tui.ruleset.set_level_filter("")
        return "已取消级别过滤"
    try:
        tui.ruleset.set_level_filter(arg)
    except ValueError as exc:
        return f"设置失败: {exc}"

    from .levelparse import LEVEL_ORDER
    return f"级别过滤: 只保留 >= {arg.upper()} 的行"


def _less(tui: Tui) -> str:
    """/less: 用分页器查看整个滚动缓冲 —— 原生选择/复制/搜索全可用, q 返回 TUI.

    curses 全屏模式下终端选择被吞; 退出 curses 交给 less 后就是普通终端文本,
    拖选复制、less 内 / 搜索都行。
    """
    lines = [format_line(ln) for ln, _ in tui.timeline.ring]
    if not lines:
        return "缓冲区为空"
    path = "/tmp/logtail_view.txt"
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
    except OSError as exc:
        return f"写临时文件失败: {exc}"
    screen = tui.screen
    try:
        curses.endwin()                     # 退出 curses, 回到普通终端
    except curses.error:
        pass
    try:
        subprocess.run(["less", "-R", path], check=False)
    except FileNotFoundError:
        return f"系统没有 less; 缓冲已写到 {path}"
    finally:
        if screen is not None:
            try:
                screen.redrawwin()
                screen.refresh()            # 恢复 curses 画面
            except curses.error:
                pass
    return f"less 已退出 (共 {len(lines)} 行; 文件保留在 {path})"


def _list(tui: Tui) -> str:
    hl = tui.ruleset.list_highlights()
    bl = tui.ruleset.list_blacklist()
    parts_h = ", ".join(r.pattern for r in hl) or "(无)"
    parts_b = ", ".join(r.pattern for r in bl) or "(无)"
    mode = "context|{n}".format(n=tui.timeline.context_n) if tui.timeline.mode == MODE_CONTEXT else "all"
    lvl = tui.ruleset.min_level or "all"
    return (f"高亮: {parts_h}  |  黑名单: {parts_b}  |  级别: {lvl}  |  模式: {mode}  |  "
            f"暂停: {'是' if tui.paused else '否'}")


# ---------------------------------------------------------------------------
# /goto: 按时间跳转 (作用于环形缓冲内)
# ---------------------------------------------------------------------------
_GOTO_FULL = re.compile(
    r"^\s*(\d{4})-(\d{2})-(\d{2})[T ](\d{1,2}):(\d{2}):(\d{2})(?:[.,](\d{1,6}))?\s*$"
)
_GOTO_TIME = re.compile(
    r"^\s*(\d{1,2}):(\d{2}):(\d{2})(?:[.,](\d{1,6}))?\s*$"
)


def _goto_base_midnight(tui: Tui) -> float:
    """决定 /goto 纯时间所锚的日期: 优先缓冲首行的日期 (reader 已把该批锚到某天)."""
    items = tui.timeline.ring._items
    if items:
        return datetime.fromtimestamp(items[0][0].ts_seconds).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
    return datetime.now().replace(hour=0, minute=0, second=0,
                                  microsecond=0).timestamp()


def _parse_goto_spec(tui: Tui, spec: str):
    """把 '/goto HH:MM:SS[.ms]' 或 '/goto YYYY-MM-DD HH:MM:SS' 解析为 (epoch, 人读串)."""
    s = spec.strip()
    m = _GOTO_FULL.match(s)
    if m:
        y, mo, d, hh, mm, ss = (int(m.group(i)) for i in range(1, 7))
        frac = int((m.group(7) or "").ljust(6, "0")[:6] or 0)
        dt = datetime(y, mo, d, hh, mm, ss)
        return dt.timestamp() + frac / 1e6, s
    m = _GOTO_TIME.match(s)
    if m:
        hh, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3))
        frac = int((m.group(4) or "").ljust(6, "0")[:6] or 0)
        secs = hh * 3600 + mm * 60 + ss
        return _goto_base_midnight(tui) + secs + frac / 1e6, s
    return None


def _goto(tui: Tui, parts) -> str:
    if len(parts) < 2:
        return "/goto 用法: /goto HH:MM:SS 或 /goto YYYY-MM-DD HH:MM:SS (跳到缓冲内该时刻)"
    parsed = _parse_goto_spec(tui, parts[1])
    if parsed is None:
        return f"/goto 时间无效: {parts[1]!r}"
    target, shown = parsed
    items = tui.timeline.ring._items
    if not items:
        return "缓冲为空, 无行可跳"
    ts = [ln.ts_seconds for ln, _ in items]      # 仅命令时构建, 可接受
    idx = bisect.bisect_left(ts, target)
    if idx >= len(items):
        idx = len(items) - 1
    tui.view_anchor, tui.view_offset = items[idx][0], 0
    # 把它移到视口中部 (向上走半屏)
    visible = tui._vis()
    tui._scroll_by(visible, tui._term_w(), -((tui._log_h() or 20) // 2))
    return f"已跳到 {shown} (缓冲内第 {idx + 1} 行附近)"


# ---------------------------------------------------------------------------
# /source: 每源开关 (显示层过滤)
# ---------------------------------------------------------------------------
def _source_cmd(tui: Tui, parts) -> str:
    if len(parts) < 2:
        names = sorted({s.name for s in tui.cfg.sources})
        kept = []
        for n in names:
            st = "只看" if tui.only_source == n else (
                "隐藏" if n in tui.muted_sources else "显示")
            kept.append(f"{n}:{st}")
        return (f"源状态: {', '.join(kept)}  |  用法: /source <名> off|on|only | /source all\n"
                f"(off=只看不到 | only=只看它 | all=清除全部开关)")
    name = parts[1]
    if name == "all":
        tui.muted_sources.clear()
        tui.only_source = None
        return "已清除全部源开关"
    if len(parts) < 3:
        return f"用法: /source {name} off|on|only"
    action = parts[2].lower()
    known = {s.name for s in tui.cfg.sources}
    if name not in known:
        return f"源 {name} 不在当前配置里 (可用: {', '.join(sorted(known)) or '(无)'})"
    if action == "off":
        tui.muted_sources.add(name)
        if tui.only_source == name:
            tui.only_source = None
        return f"已隐藏源 {name}"
    if action == "on":
        tui.muted_sources.discard(name)
        return f"已显示源 {name}"
    if action == "only":
        tui.only_source = name
        tui.muted_sources.discard(name)
        return f"只显示源 {name}"
    return f"用法: /source {name} off|on|only"


def _save(tui: Tui) -> str:
    # 去掉可能有缩写的重复? 直接取原始 pattern 列表
    hl = [r.pattern for r in tui.ruleset.list_highlights()]
    bl = [r.pattern for r in tui.ruleset.list_blacklist()]
    try:
        path = save_config(tui.cfg.path, hl, bl)
    except ConfigError as exc:
        return f"保存失败: {exc}"
    return f"已保存到 {path} (keywords={hl}, blacklist={bl})"


def _reset(tui: Tui) -> str:
    # 高亮词/黑名单回到"配置文件当前值" (中途可能 /save 修改过, 故重新读盘)
    keywords, blacklist = tui.cfg.keywords, tui.cfg.blacklist
    try:
        fresh = load_config(tui.cfg.path)
        keywords, blacklist = fresh.keywords, fresh.blacklist
    except ConfigError:
        pass    # 读不到就退回启动时的内存值
    tui.ruleset.reset(keywords, blacklist)
    tui.cfg.keywords, tui.cfg.blacklist = keywords, blacklist
    # 显示回默认并清空缓冲
    tui.timeline.clear()
    tui.timeline.set_mode(MODE_ALL)
    tui.timeline.trace_term = ""
    tui.timeline.set_context_n(tui.cfg.context_n or DEFAULT_CONTEXT_N)
    tui.paused = False
    tui._pending.clear()
    tui.view_anchor, tui.view_offset = None, 0
    tui.search_active = False
    tui.search_idx = -1
    tui._hit_line = None
    # 重新从文件末尾跟踪
    tui.follower.reset()
    return (f"已重置: 高亮/黑名单回到配置文件值, 显示回全量, 重新跟踪日志 "
            f"(keywords={keywords}, blacklist={blacklist})")


_HELP = (
    "高亮/匹配: /keyword|/k 词 [词...]  添加(可多个, re: 前缀=正则, 大小写不敏感)"
    " | /remove|/rm 词 移除 | /clear|/clr 清空全部\n"
    "显示: -C|/context|/ctx N 上下文前后N行 | /all|--all 全量 | /pause /resume 暂停/恢复\n"
    "过滤: /blacklist|/bl 规则 [规则...] 加黑名单(支持 re:) | /unblacklist|/ubl 移除\n"
    "滚动: ↑↓逐行 PgUp/PgDn或Ctrl-B/Ctrl-F翻页 Home/g最顶 End/G最底 滚轮上/下 回车跳最新\n"
    "跳转/收窄: /goto HH:MM:SS 跳到缓冲内某时刻 | /source 名 off|on|only 每源开关 /source all 清除\n"
    "配置: /list 状态 | /save 写回配置(仅此命令落盘) | /reset 重读配置重开 | /help | /quit|/q|Ctrl+C\n"
    "复制: /less 分页器查看缓冲(原生选择/搜索, q返回; 部分终端 Shift+拖拽也可原生选中)"
)


def main(cfg: Config) -> int:
    return Tui(cfg).run()


# ---------------------------------------------------------------------------
# 显示宽度与折行工具
# ---------------------------------------------------------------------------

def char_width(ch: str) -> int:
    """返回单个字符的显示列宽: 中日韩/全角 = 2, 其余 = 1."""
    if ch in "\r\n\t":
        return 1
    # East Asian Wide / Fullwidth 视为宽字符
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def strip_nul(text: str) -> str:
    """去掉字符串里的 NUL 字节 (\\x00), 供进入 curses 渲染前调用.

    日志字节里可能出现 NUL (二进制垃圾/结构数据), 而 NUL 是合法 UTF-8 字符,
    会在 reader 的 decode(errors="replace") 中原样穿透进 LogLine.text。
    curses 的 addnstr 遇到内嵌 NUL 会抛 ValueError: embedded null character,
    让整个 TUI 崩溃。这里在渲染边界剥掉 NUL:
      - 只影响显示, 不修改 LogLine.text 数据源;
      - 因此 agent / --json / stdout 契约完全不受影响。
    """
    if "\x00" not in text:
        return text
    return text.replace("\x00", "")


def wrap_text(text: str, width: int) -> List[str]:
    """按列宽把 text 折行, 保证折出的每段显示宽度 <= width.

    按字符逐个累积列宽, 中文字符占 2 列, 因此中文行不会像按字符数切那样
    出现列溢出。
    """
    if width <= 0:
        return [text]
    out: List[str] = []
    cur = []
    cur_w = 0
    for ch in text:
        cw = char_width(ch)
        if cur and cur_w + cw > width:
            out.append("".join(cur))
            cur = []
            cur_w = 0
        cur.append(ch)
        cur_w += cw
    out.append("".join(cur))
    return out
