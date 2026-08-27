# logtail 调试能力增强建议

> 以"调试工作流"为视角,对 logtail 提出的改进建议。核心思路不变——
> **把分散日志合成一条时序流,再收窄到少量行**。当前短板集中在两处:
> ① 人只能"在彩色流里找",不能"跳";② 工具只懂"文本",不懂"级别 / 实体"。

标注的 `logtail/*.py` 路径基于当前实现,供落地时定位。

---

## Tier 1 —— 最能提升人类调试体验

### 1. 缓冲区内搜索 + 命中间跳转 (最值得做)

**现状**:只能 `/k timeout` 让所有命中行变色,没有"从上一条命中跳到下一条"的手段。
调试时通常是:看到可疑 → 想快速翻遍所有 `player=X` 的出现点。这需要 `less` 风格搜索:

- `/关键词` 在当前 scrollback 里增量搜索并定位到结果顶部
- `n` / `N` 跳到下一条 / 上一条命中
- 高亮当前命中行

**实现参考**:[tui.py:331](../logtail/tui.py#L331) 的 `_handle_key` 目前只处理方向键/滚轮/命令输入,无搜索态。
加一个"搜索模式"状态即可,复用 `Rule.matches` 匹配逻辑,天然支持子串与 `re:`。

> 高亮是"让你看见",搜索跳转是"让你到达某处"——这是人类调试者最强的诉求。

---

### 2. 日志级别感知 (level-aware)

**现状**:工具完全不解析级别,黑名单里写 `"DEBUG"` 纯粹是子串碰运气。
调试者以级别为第一坐标,建议:

- 从行首 token 解析 `TRACE|DEBUG|INFO|WARN|ERROR|FATAL`
- 新增 `/level ERROR,WARN` + `--level` 参数过滤(与黑名单/`--match` 叠加)
- 级别自动着色:ERROR 红、WARN 黄(无需手打高亮词)

**实现参考**:[rules.py](../logtail/rules.py) 的 `RuleSet` 目前只有 highlight/blacklist 两类,需加一个 level 维度;
级别解析可放 [timeparse.py](../logtail/timeparse.py) 附近的轻量解析器。

> 一个词就能把"我要的"从"我不要的"切开——这是 debug 的第一道口令。

---

### 3. 实体"追踪/跟随"模式 (trace by id)

**现状**:多进程调试最典型动作——"玩家 `123` 在 scene 被踢,我要看它在 gateway/scene/dungeon 的完整轨迹"。
现在得 `/k player=123` 再进 `-C` 上下文,但上下文模式会硬拖邻居行,不够纯净。

建议加一个专门的一等模式:

- 交互:`/trace player=123` → 只显示**所有源中**含该文本的行,按时间有序,不带邻居
- Agent:`--trace player=123` → 只输出这些纯净命中行(等价于 `--match` 但语义明确)

**实现参考**:这是 `--match` 的"全命中、零上下文"特例。[agent.py:96](../logtail/agent.py#L96) 已算出 `hit_idx`,
缺的只是"不展开邻居 + 不截断"的纯追踪分支。改动很小,但这是"跨进程定位因果"的王牌动作。

---

## Tier 2 —— Agent / 事后排查价值

### 4. 离线整目录 grep (不依赖实时 tail)

**现状**:工具只能跟随实时文件([reader.py:88](../logtail/reader.py#L88) 逐文件增量读)。
但大量调试是**事后**查案:"昨天 22:00 game 服报错,把那时所有 `ERROR` 捞出来"。
`--since` 只作用于实时尾巴;`--date` 配合 glob 也仍在"跟"而不是"扫"。

**建议**加批量模式:`--grep ERROR --date 2026-08-26`,扫描所有匹配历史文件、按时间序输出命中行 + 命中统计。
实现上不走 tail 语义,而是整文件一次读入 + `extract_timestamp` 排序列出。

> Post-mortem 是调试的一半,这一半目前完全没覆盖。

---

### 5. Agent 结构化输出 (JSONL) + 字段抽取

**现状**:README 把 Agent 当核心卖点,但喂给 AI 的是纯文本,AI 要自己抠字段、自己数。
建议 `--json` 输出结构化记录:

```json
{"ts":"11:20:01.228","source":"scene","level":"ERROR",
 "fields":{"player":"12345","scene_id":"901"}, "text":"kick timeout ..."}
```

再加 `--fields item_id,player` 只抽出关心的键值,让 AI 直接拿关键字段做关联、显著减少 token。

**实现参考**:[agent.py:24](../logtail/agent.py#L24) 的 `format_line` 增加一个 JSON 分支,
轻量级 KV 解析(`key=value` 或 a=/b=)抽取字段。

> 对 AI 而言,"结构化 + 少字段"比"原始文本 + 全文"更快也更准。

---

### 6. 统计 / 速率模式

**诉求**:"这段话是不是爆了?"、"哪些错误签名在涨?"。

`--stats` 给定时间窗内输出:按源计数、按级别计数、Top N 错误签名/字段值。
能直接回答"是不是它导致的上升",而不是人工扫。

---

## Tier 3 —— 打磨项

- **可配置 scrollback,默认加大**。[timeline.py:54](../logtail/timeline.py#L54) 写死 `maxlen=4000`,
  调试回溯时可能不够,建议敞口或 CLI 可调;再加 `/dump` 把当前缓冲写到文件,便于复现/贴 issue。
- **折叠连续重复行**("last message repeated N times"),日志风暴时保住可读性。
- **上下文窗口支持按时间**(`-C 5s`)而非仅按行数([timeline.py:111](../logtail/timeline.py#L111) 现在只按行)。
- **时区 / 时间戳**:[models.py:99](../logtail/models.py#L99) 用本地时区换算,但日志自带时间戳若来自其它时区,
  批内排序会错位;建议暴露时间戳正则配置,并对解析不到的行给出提示而非静默。
- **`--match` 支持多个 + 排除**:`--match ERROR --exclude heartbeat`,或 `--all-match`(AND 而非默认 OR)。
- **小坑**:agent `--since` 用的是 `time.time() - since`([agent.py:89](../logtail/agent.py#L89)),
  若用 `--date` 看历史日 + `--since`,所有历史时间戳都早于 cutoff,会被**全部清空**——建议明确语义或按日志自带时间戳修正。

---

## 建议优先级

| 优先级 | 建议 | 理由 |
|---|---|---|
| P0 | #1 缓冲区内搜索+jump | 改动最可控,直接提升"人"的体验,复用现有匹配逻辑 |
| P1 | #2 日志级别感知 | 一维把"要的"与"不要的"分开,配合着色极直观 |
| P1 | #3 实体 trace | 跨进程定位因果的王牌动作,改动小 |
| P2 | #4 离线 grep | 补上 post-mortem 空白 |
| P2 | #5/6 Agent 结构化 + 统计 | 让 AI 更准、更省 token |
| P3 | 打磨项 | 体验/健壮性提升 |

若只想先做一件事,建议 **P0 #1**(只动输入/渲染态,风险最小、见效最直接)。
