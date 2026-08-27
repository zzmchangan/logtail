# logtail 调试能力增强建议 · 第二批(易用性 / 集成 / 可信度)

> 承接《DEBUGGING_SUGGESTIONS.md》(Tier 1–3 偏"调试能力")。本文件是**第二轮**。
> 覆盖三个新象限:① 交互与配置的**易用性**;② VSCode 插件**集成路径**;
> ③ 跨进程合并视图的**可信度** + 工具扩展到**大规模/复杂配置**时的健壮性。

标注路径基于当前实现,供落地定位。

---

## A. 交互层易用性

### A1. 命令历史:↑/↓ 被滚动占用,打命令时无法取回上一条
**现状**:[tui.py:349-368](../logtail/tui.py#L349-L368) 的 `↑`/`↓` 只在输入框为空时做滚动;一旦在打命令,↑/↓ 返回 `None` 什么都不干。
→ **没有命令历史**,`/k timeout` 打错只能重打。

**建议**:输入框非空时把 `↑`/`↓` 改造成"上一条/下一条命令历史";滚动交给滚轮/PgUp。或单独 `Ctrl+P/Ctrl+N` 取历史。成本极低、天天用到。

### A2. 输入即过滤 —— 已评估并**砍掉**
在输入框敲裸词时实时 dim 掉不命中的行(做预览,回车才固化)。**结论:非必要**,不采纳。此项仅记录取舍。

### A3. `/help` 是一堵墙 → Tab 补全 + 按类别展示
[tui.py:616](../logtail/tui.py#L616) 的 `_HELP` 是全量文本墙。命令已有 15+ 条且还要加。
**建议**:输入 `/<` 时 Tab 补全命令名;`/help` 按类别(高亮/显示/过滤/滚动/配置)分组,而非堆一块。

### A4. 启动摘要 + "没读到日志"明确告警
某个 pattern 匹配到 **0 个文件**时,现在只看到一片空白,分不清"没日志"还是"配错了"。
**建议**:启动打一行摘要"跟踪 3 源 / 5 文件 / 回溯 200 行",并对零文件的源明确报"source `dungeon` 未匹配到任何文件"。
校验只查了目录存在([config.py:36](../logtail/config.py#L36)),**没查文件是否匹配**——这是坑。

### A5. source 级控制:隔离噪音进程
黑名单是**全局子串**,没法单独静音某进程。多进程场景下某源特别吵(如 heartbeat),想只看 scene/dungeon。
**建议**:`/source gateway off` 静音、`/source scene only` 只看它、`/source` 列出状态。比硬写全局黑名单更聚焦。

### A6. 按时间跳转 + 缓冲截断提示
- `/goto 11:23:45` 直接跳到那个时间点,而非一屏一屏滚。长会话回看时时间是第一坐标。
- 翻到缓冲最顶时,现在只停住,不知道更早的日志已被环形缓冲丢弃。建议顶部标"**已丢弃 N 行更早日志**"。

### A7. 复制当前行 / 导出
curses 里很难选中复制,而调试者常要把某条错误贴进 issue。
**建议**:`/copy <行号>` 或"复制当前行"快捷键,打到系统剪贴板(`wl-copy`/`xclip`)或追加到 `/tmp` 文件。

---

## B. 健壮性 / 配置易用性

### B1. `--encoding`:支持非 UTF-8 日志(GBK 等)
[reader.py:223](../logtail/reader.py#L223) 硬编码 UTF-8(`errors="replace"`)。很多国产系统日志是 GBK/GB2312,会直接乱码且**破坏排序**。
加全局 `--encoding`/配置项,一行改动解决一大类问题。

### B2. 配置校验加强
- 两个 source 同名 → 前缀列会撞,启动告警。
- pattern 过宽会匹配到大量文件,提示频率/体量。

### B3. 回溯触顶提示
[reader.py:141](../logtail/reader.py#L141) history 上限 1MB、[reader.py:173](../logtail/reader.py#L173) `--since` 上限 8MB。
超出就**静默截断**,用户不知道只回看了尾部。给一行"回溯已触顶,仅回看最近 X MB"。

---

## C. Agent / 自动化易用性

### C1. `--fail-if-empty`:让"零命中"成为可编程信号
`--count 0` 现在打印 0 但**退出码仍为 0**。脚本/CI 想断言"有没有报错?"得解析输出。
加 `--fail-if-empty`,零命中返回非 0,`if ! logtail ...` 即可判断。对嵌进自动化排查流程很关键。

### C2. monitor 输出加 source 切换分隔框
[agent.py:127](../logtail/agent.py#L127) monitor 把混合源连续打出来,source 切换时无标记。
当来源变化时打一行 `──[scene]──` 分隔,扫管道时清晰很多。

### C3. "取 X 字段 Top 值"现成组合
上轮 `--stats` 是雏形。更希望给一套现成的便捷用法:`--fields scene_id --top 10` 直接在时间窗内按 `scene_id` 聚合出频次 Top10,省去"grep+awk+sort"一长串。这才是工具的黏性。

---

## D. VSCode 插件集成路径

### 核心约束:界面层要重写,引擎层可复用
logtail 的 TUI 用 **curses**([tui.py](../logtail/tui.py))依赖终端转义,**放不进 VSCode Webview**。但真正值钱的是引擎层——`reader`(多线程 tail)/`rules`(过滤高亮)/`timeparse`(时间戳)/`timeline`(排序上下文),**这套 Python 代码原样复用**。不是"重写工具",是"引擎当后端、换一层 IDE 皮肤"。

### Level 1 —— 终端/输出面板(快速版,半天内)
```bash
python -m logtail --agent --mode monitor --config config.yaml --match ERROR -C 2
```
用 VSCode **Task 或 Output Channel** 接住 stdout 逐行渲染。优点:零架构改动、立刻在 IDE 看聚合日志。缺点:纯文本流,无交互过滤。

### Level 2 —— Webview 面板(真正的插件,价值最大)
把 Python 引擎 spawn 成后台子进程,经 **stdout 推 JSONL**(与第二批里 `--json` 产出打通),VSCode 插件用 `child_process` 逐行读、渲染进 Webview:
- 保留多源聚合、时间排序、颜色高亮、上下文模式、黑名单
- 新增 curses 做不到的:点击 `[source]` **直接打开那个日志文件跳到该行**;过滤/搜索是即点即用 UI(不用记 slash 命令);后台运行不占终端;`--json --fields` 把 `player`/`scene_id` 做成表格列
- 技术要点:扩展宿主 Node 允许 `child_process.spawn` 长驻、流式读 stdout,是标准做法,无障碍;Webview 经 `postMessage` 收发

### 需要补的一个小数据:记录来源文件路径
要做"点击跳文件",需让流出的每条记录带上**来自哪个文件**。现在 `LogLine`([models.py:24](../logtail/models.py#L24))只带 `source`(源名)不带路径——在 `--json` 里加 `"file"/"line"` 字段即可。

### 打包顾虑
引擎是 Python,插件机器需有 Python 3.9+。可接受就自然带;想免依赖就得用 TS 重写 tailing/过滤逻辑(reader/rules/timeparse),那才是真重写。**建议先走"带 Python 依赖"验证价值**。

### 建议顺序
先 **Level 1** 验证"聚合值不值得常驻 IDE",再上 **Level 2** 把 `--json` + Webview + 点击跳文件做出来。

---

## E. 合并视图的可信度(调试的地基)

### E1. 跨源乱序到达
README 自述"本批内稳定排序,近似全局有序",是**每批(120ms)排一次**([timeline.py:71](../logtail/timeline.py#L71)),非全局排。
后果:某进程 line 时间戳 `T=9.9`,因其轮询晚一拍在第 2 批才到,于是显示在 `T=10.0` 之后,两条本应相邻的因果链被 200ms 错位。
**建议**:
- 加 **hold-and-resort 窗口**:行先扣住 ~1s,窗口内重排再落屏,消除瞬时反转(代价 +1s 延迟,可配置)
- 或至少给**时钟偏差启发警告**:某 source 时间戳整体比另一 source 晚 N 秒时,提示"这俩进程时钟不一致,排序不可尽信"

### E2. (真实 bug)管道到 `head` 会打 traceback
README 鼓励 agent 模式"可管道给 grep/head"。但 `... --mode monitor | head -1` 时下游关闭读端,`print`/`flush` 抛 `BrokenPipeError`([agent.py:130-131](../logtail/agent.py#L130-L131) 没接),直接吐一屏 traceback。
**修复**:捕获 `BrokenPipeError` 静默退出(或设 `SIGPIPE` 忽略)。低成本、脚本里很常见。

### E3. 轮转时的回溯策略
[reader.py:120](../logtail/reader.py#L120) 检测到 inode 变化就**从字节 0 重读**。copytruncate/rename 轮转后,可能突然**吐出整个新文件历史内容**(而非只新增),若 `--history` 设了还从 0 读,回溯可能爆屏。
**建议**:轮转后仍按 `history/since` 起点定位,而非一刀切 0。

---

## F. 分布式追踪式的因果透视(新能力,不止是过滤)

### F1. 按 trace_id 做"跨源关联时间轴"
上批 #3"实体 trace"是**把含 `player=123` 的行挑出来**(过滤)。更强的版本是**透视**:
- 从结构化字段认出 `trace_id`/`request_id`/`session`
- 把它当作时间轴上的 span:gateway `enter` → scene `process` → dungeon `save` → `kick`
- 于是看到"这一条请求在多进程里怎么走、每段花多久",而非一条条散行

对"定位慢请求/跨进程链路"是降维打击。空间有限,curses 做不出,但 **Webview 插件里能做成 chart**。与 `--json --fields` 同一条线:先有结构化字段才能透视。是把 `--json` 与 Level-2 插件串起来的北极星功能。

---

## G. 配置与规模的扩展性

### G1. 单源多 pattern + 递归 glob
一个源只能一个 glob([config.py:78](../logtail/config.py#L78))。但一个进程通常写 `main.log`+`error.log`+`access.log`,逻辑上是"一个进程"。
**建议**:`patterns: ["*.log","err_*.log"]`,省去重复配 source、前缀不乱;并支持 `**` 递归 glob 匹配按日期建的目录树(`logs/2026/08/27/x.log`),配合离线 grep 更有用。

### G2. `--dry-run` / `--lint`(配完先验一把,不 tail)
现在验证配置只能真跑起来看 ([config.py:36](../logtail/config.py#L36))。
**建议**:`logtail --dry-run`:校验 config + 打印"每个源匹配到哪些文件、各多少行、总字节",**不启动尾巴**。配合 A4 启动摘要,先诊断再跑。

### G3. 跨天 `--range 2026-08-26..2026-08-27`
`--date` 只能看单天,一次 debug 常跨午夜。离线 grep/回溯支持一个范围,覆盖多天日志。

---

## H. 工具自身在"变复杂"时的健壮性

### H1. 正则安全性
用户会在 `/k`/`--match` 里贴 `re:` 正则,每条线在 `feed` 时跑一遍([timeline.py:73](../logtail/timeline.py#L73) / [rules.py:47](../logtail/rules.py#L47))。若贴个灾难性回溯正则,会**卡死 120ms 主循环**。
**建议**:加**单正则超时**或"匹配过慢则告警"兜底,比崩溃好。

### H2. 让渲染循环可测试(de-risk 后续功能)
TUI 几乎不可测(要真终端),[smoke_test.py](../tests/smoke_test.py) 只测到模块层。建议把渲染/输入循环**依赖注入虚拟 renderer/key 源**,使新的交互逻辑无需终端也能冒烟测试。等价于给工具做 CI。

---

## 第二批优先级建议(在 Tier 1–3 之外)

| 优先级 | 建议 | 理由 |
|---|---|---|
| P0 | E2 修 BrokenPipe | 真实 bug,README 宣传的管道用法挂了 |
| P0 | A1 命令历史 | 天天用到,改动最小 |
| P1 | A4/B3/B2 启动摘要+告警 | 解决"到底读到没读到",配置诊断 |
| P1 | E1 合并视图可信度 | 调试地基,跨源因果链可信的前提 |
| P2 | F1 trace 透视 | 与 `--json`/Level-2 插件联动的新价值高峰 |
| P2 | D VSCode Level 1→2 | 把工具嵌进日常 debug 流的产品化路径 |
| P3 | G/H 规模与健壮性 | 扩展体验与防护 |

若只想先做一件:**E2 修 BrokenPipe**。若想做"显著提升易用性":**A1 命令历史 + A4 启动摘要**。
