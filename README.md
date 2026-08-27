# logtail — 多文件实时日志聚合查看工具

在单个终端视图里实时聚合跟踪多个日志文件, 提供接近单进程调试的多进程日志观察体验。
支持黑名单过滤、交互式关键词高亮(多色)、上下文聚焦(前后 N 行)、暂停/恢复、历史回溯、
配置持久化(`/save`) 与重开(`/reset`)。

## 运行环境
- 纯本地运行, 日志在本机磁盘上, 不涉及远程。
- 支持 Linux / macOS。
- Python 3.9+, 依赖 PyYAML (`pip install -r requirements.txt` 或 `pip install pyyaml`)。

## 快速开始
```bash
# 1. 复制示例配置并修改日志源
cp config.example.yaml config.yaml
vim config.yaml

# 2. 运行
python -m logtail --config config.yaml

# 带历史回溯 + 上下文窗口
python -m logtail --config config.yaml --history 50 -C 5
```

## 配置文件
```yaml
log_sources:
  - name: gateway                 # 输出前缀, 标识来源
    path: /data/logs/gateway/
    pattern: "*.log"
  - name: scene
    dx: "dx log SceneServer 131189"    # 可选: 自动发现路径, 优先于 glob
  - name: dungeon
    path: /data/logs/{date}/           # 日期占位符
    pattern: "SceneServer-*_{date}_*.log"

blacklist:                    # 命中则丢弃 (大小写不敏感子串匹即或 re: 正则)
  - "heartbeat"
  - "DEBUG"

keywords:                     # 初始高亮词 (大小写不敏感; re: 前缀=正则)
  - "item_id"
  - "timeout"
```

**路径发现**:
- **日期占位符**: `path` / `pattern` 中的 `{date}`(`YYYY-MM-DD`)、`{YYYY}`、`{MM}`、`{DD}`
  启动时自动用当天日期填充。看历史某天用 `--date 2026-08-26`。
- **`dx` 自动发现**: 配了 `dx` 后不再 glob, 而是运行 `dx <cmd>` 拿返回的具体文件路径
  (每行一个), 配合按日期/服务拆分的日志最省事, 无需每次手工改。

## AI Agent 模式 (非交互)
给 AI 修 bug 用——核心价值是**聚合日志 + 过滤日志**:把分散在不同进程/目录的文件聚合成一条时间有序的流,再过滤到少量行,AI 就能在**一个聚焦视图**里快速定位 bug。
```bash
# 一次性: 收集最近 50 行 (经黑名单, 只留含 ERROR 的), 打印后退出
python -m logtail --agent --config config.yaml --lines 50 --match ERROR

# 命中 ERROR 的每条行, 连带其前后各 2 行一起输出 (AI 看因果)
python -m logtail --agent --config config.yaml --match ERROR -C 2

# 只保留正则命中的最近 8 行
python -m logtail --agent --config config.yaml --lines 8 --match 're:player=\d+'

# 只看最近 5 分钟 (按日志自带时间戳过滤, 支持 30s/5m/1h) —— 交互与 agent 均可用
python -m logtail --config config.yaml --since 5m
python -m logtail --agent --config config.yaml --match ERROR --since 5m

# 只保留 >= ERROR 级别 + 只输出命中行数 (快速判断是否爆发)
python -m logtail --agent --config config.yaml --level ERROR --count --since 5m

# 实体追踪: 跨源只显示含 player=123 的纯净命中行 (无邻居)
python -m logtail --agent --config config.yaml --trace player=123

# 多词(OR) + 排除: 命中 timeout 或 crash, 且排除 heartbeat
python -m logtail --agent --config config.yaml --match 'timeout crash' --exclude heartbeat

# 同源上下文: 命中行连带"同进程"前后各 2 行 (跳过其它进程, 看单进程因果链)
python -m logtail --agent --config config.yaml --match ERROR --ctx-same 2 --since 5m

# 结构化输出 (NDJSON): 每行一个 JSON 对象, 供编程级加工 (仍走同一过滤管线)
python -m logtail --agent --config config.yaml --match ERROR --json --lines 20

# 健康检查: 只探测源是否被发现, 不 tail 不读正文 (agent 先验证再信 "0 命中")
python -m logtail --diagnose --config config.yaml

# 单源聚焦: 只看 scene 这个进程的行 (按配置里的源名筛, dx/glob 源均有效)
python -m logtail --agent --config config.yaml --focus scene --since 5m

# 关联键: 跨进程跟一条逻辑链路 (同一 id 不同写法也能串起来)
python -m logtail --agent --config config.yaml --correlate player=123 --since 5m

# 持续监控: 把过滤后的日志持续打 stdout (Ctrl+C 停止)
python -m logtail --agent --mode monitor --config config.yaml --match ERROR
```
- 前缀与交互版一致 (`[时间戳] 来源 正文`); Agent 能看出日志来自哪个进程。
- `--match` 复用关键词写法: 裸词=子串, `re:`=正则, 大小写不敏感; 逗号/空格分隔多词 = OR。`--exclude` 命中即剔除。
  ⚠️ **`--match "a|b"` 的 `|` 是字面量不是正则 OR**——多词 OR 用空格/逗号分隔, 或 `re:(a|b|c)`。裸词是子串, 也会撞无关文本(如 `Dragon` 命中账号名 `dragon2`), 要词边界就上 `re:`。
- `--case-sensitive`: **精确匹配开关**——本次运行所有文本匹配(裸词/`re:` 正则/黑名单/correlate 抽取)区分大小写, 解决"Dragon 撞 dragon2"这类误命中。**默认不敏感**(`--match ERROR` 能命中 `[Error]`, 黑名单 `DEBUG` 能滤 `[Debug]`——这两个场景别加此开关); 查级别词时也别加, 否则撞不到。
- `-C N` 结合 `--match`: 每条命中行连带**前后各 N 行**。**`-C 5s`/`-C 1m` 按时间窗只在交互版可用** (`/context 5s` 或 TUI 内 `-C 5s`); agent 的 `-C` 只接受**行数**。
- `--since 5m`: 只看最近一段时间(按日志自带时间戳), 跳过早期杂音; 以最新日志为参考, 历史日 `--date` 也能用。
- `--level ERROR`: 只保留 >= 该级别的行; `--trace <词>`: 跨源纯净命中行(无邻居)。
- `--ctx-same N`: **同源上下文**——命中行连同"同进程"前后各 N 行 (跳过其它进程)。与 `-C`(全局时间邻居)互补: `-C` 看系统面, `--ctx-same` 看单进程因果。
- `--json`: 每行输出一个 JSON 对象 (`ts`/`ts_seconds`/`source`/`level`/`text`/`seq`), 供编程级加工; 仍走同一套过滤管线。
- `--diagnose`: **只做发现健康检查**(不 tail 不读正文), 输出 JSON: 每源 `files`/`discovered`/`dx_error`/`latest_ts`。
- `--focus <源名>`: **单源聚焦**——只输出指定来源的行(按配置里的源名筛, dx/glob 源均有效), 与 `--ctx-same` 互补看单进程。**精确匹配、大小写敏感**; 未知/typo 源名 fail-fast exit 2 并列出可用源名(防静默 0 行假阴性)。
- **管道纪律**: `--agent ... | head -N` 当输出量超过 head 消费量时, python 被 SIGPIPE 杀, **观察到的 exit 码会偏离 0/2 契约**(如 120/141)。要截断用工具自身的 `--lines N`; 判定成败靠工具本身的退出码 + `--summary`, 别在有 `| head` 的管道里看 `$?`。
- **`--since` 仅支持单单位**: `30s/5m/1h/90m` 可; 复合(`1h30m`)、小数(`1.5h`)、裸数字(`90`)会被 exit 2 拒绝(错误信息会明示)。
- `--correlate <key>=<value>`: **关联键**——跨进程按共享标识对齐: 用 `correlation_keys` 配置(或内置预设 `player`/`scene`/`session`)的正则从每行**抽取** id、**归一化**(去空白/前导零)后比对, 只留匹配行, 全局时间排序。解决"同一 id 在不同进程打印成 `player=123`/`RoleId:123`/`guid:123` 串不起来"的问题。未定义 key 回退字面子串(同 `--trace`)。
- `--discover-keys`: **关联键发现**——采样窗口, 对一批候选 key(player/scene/session/uid/request/call/order/instance + 配置里的)跑抽取, stdout 输出 JSON: 每个 key 的 `lines_with_key`/`distinct_values`/`sources`/`sample_values`。**与 correlate 同视野(含黑名单/级别过滤)**——报的数字就是 `--correlate` 实际能看到的。挑选标准: 多源出现 + distinct 高 = 好的跨服关联键; 覆盖满但 distinct=1 = 全服常量无区分度。实战发现: 本集群 scene 实例 id 主要在 `[Debug]` 行上, 主配置(含 DEBUG 黑名单)下只能看到 4 行——追 scene 链路加 `--allow debug` 临时放行即可(实测 4 行 -> 91 行), 不必切配置。
- `--anchor <epoch>` / `--at "YYYY-MM-DD HH:MM:SS"`: **钉死窗口**——把 `--since` 窗口定在 `[anchor-since, anchor]`, 不随最新日志滑动(需与 `--since` 同用, 否则 exit 2;两者互斥)。**跨次 `--count` 可比**: 回归实验"改前 vs 改后"用同一个 anchor 两次对比; epoch 锚点取上一次 `--summary` 的 `latest_ts`, 人读时间用 `--at`(本地时区)。追加的新行被上界夹掉, 早前行不滑出。
- `--keep head|tail`: **截断保留端**(默认 tail 最新)——链路起点在窗口头部时(如登录认证段), `--lines` 尾部保留会被后面的刷屏段吃掉, `--keep head` 保留头部。**超限必提示**: 截断发生时 stderr 打 `hint: 命中 X 条(共 Y 行), 只输出 Z 条`——"看到的不一定是全部"显式化。
- `--allow <词>[,<词>...]`: **临时豁免黑名单项**(按项原文大小写不敏感)——`--allow debug` 不切双 config 就能看 `[Debug]` 行(查微服务/追 scene 链路); 豁免词不在黑名单里会 stderr 提示(防 typo 静默无效)。

配置 `correlation_keys`(每个 key 一组正则, 第一个命中即取):
```yaml
correlation_keys:
  - name: player
    extract:
      - "Guid[:=] *(\\d+)"        # [Player Guid:1276679028765 ...]
      - "roleId[:=] *(\\d+)"
      - "player[:=] *(\\d+)"
```

**关联键自报**(诚实提醒): `--summary` 的 JSON 含 `correlate: {key, value, lines_total, lines_with_key, matched}`。`lines_with_key==0` 会另打 stderr 警告(正则写歪/该 id 不存在); `lines_with_key` 接近 `lines_total` 说明该 key **没有区分度**(如某 token 是全服常量, 每行都有)——换更精确的 key。跨进程能串起来的前提是那个 id 在多个进程都真实出现; 只在单进程出现时 correlate 退化为单进程故事, 不亏但别指望跨进程。
- `--count`: 只输出命中行数 (经 黑名单/级别/match/exclude 过滤后), 快速判断这段时是否爆发。
- 黑名单**始终生效**; 不带 `--match`/`-C` 时输出黑名单过滤后的全部最近 N 行。
- Agent 修 bug 的用法: 先 `--match` 收窄到错误行 → `-C` 补上下文 → `--since` 限时间段 → `re:` 正则再收窄 → 拿到极少量却完整的线索。

### 三层读取模型 (用对的关键)
参数分三层,**各自独立的旋钮**,想通这个就不会出现"match 了明明存在的行却空":

| 层 | 参数 | 决定什么 |
|---|---|---|
| ① 采集量 | `--since`(**优先**, 时间戳二分定位、覆盖全文件; 二分失败退化尾部 8MB 扫描并 stderr 警告) / `--history` / `--lines`(无 since 时) | **读多少**进内存 |
| ② 过滤 | `--match`/`--exclude`/`--level`/`--focus`/`--correlate` | 读到的行里**留哪些** |
| ③ 输出量 | `--lines` | **打出几条** (仅正文条数上限; `--count` 不受它限) |

常见误区:把 `--lines` 当"读多少行"。想深挖某个旧行 → **加大读取量**(大 `--lines` 或大 `--history`, 或去掉 `--since`),不是加大输出。另注意 `--since` 一旦给出,`--lines`/`--history` 不再决定回溯量。

**de-noise 优先级**(聚合流本身可能是新刷屏): `--focus`(单源) > `--level ERROR` > `--match`。超长刷屏行(如 SceneMgrStats)优先 focus 掉。

### 输出契约 (AI / 脚本接入必读)
- **stdout 只放日志正文**, 错误/提示走 **stderr**。所以可直接 `python -m logtail --agent ... 2>/dev/null` 只取日志; 管道给 grep/head 也不会混入报错。
- **退出码**: `0`=成功 (含 0 条命中); `2`=配置/参数/正则错误。
- **每行格式**: `{时间戳} {来源:<12} {正文}`。时间戳优先用日志自带原文 (含括号), 无则回退 `[HH:MM:SS.mmm]`; 来源列宽固定 12, 不足补空格。
- **排序**: agent 是**一次性收集后全局排序** (按 `时间戳, 序列号`), 同一窗口内跨文件严格按时间排列, 适合做因果推断 (交互版是"每批排", 仅近似有序)。
- **无时间戳的行**: `ts_key` 退化为**到达时刻**, 时间不可靠, 会落在窗口末尾附近 —— 推断时要留意。
- **`--wait` (默认 2s)**: **实时跟随时长**, 不是总时长。dump 分两阶段: 先**信号驱动**等各源把历史窗口(`--since`/`--history` 定位的起点→文件尾)全部读完(不受 `--wait` 限制,只受 30s 硬上限约束), backlog 完成后再跟随 `--wait` 秒收新行(或约 1s 无新行提前返回)。硬上限内 backlog 没读完会 stderr 打 `warning: 历史窗口读取未完成`——**看到这条就别把空结果当结论**。
- **`--lines` vs `--history`**: agent 收集量 = `max(lines, history)`, 输出取最后 `lines` 条。
- **`--since` (dump)**: 以**窗口内最新一条日志**的时间戳为参考 (`最新 - since`), 而非 wall-clock —— 所以配合历史 `--date` 扫描也不会被清空。
- **`--summary`**: 把"发现诊断"(JSON)打到 **stderr**。**空结果/`--count 0` 时先看 stderr**: 有 `warning:` 或 JSON 里 `total_files==0` / 某源 `discovered:false` / `dx_error` 非空 → 是"**源没被发现**", 不是"没错误" (避免 'exit 0 + 空输出' 假阴性); 都正常 `files>0` 才可信。stdout 仍只放日志/计数。**`backlog_complete: false`** 表示硬上限内历史窗口没读完——count 偏小勿当结论。给了 `--anchor` 时 summary 也会报出 `anchor` 字段(跨次可比的凭据)。
- **`--json` 契约**: 每行一个 JSON 对象 `{"ts","ts_seconds","source","level","text","seq"}`。`ts` 供人读/对齐窗口, `ts_seconds` 为 epoch 秒供排序比较, `seq` 供确定性重放; `--count`/`--summary` 不受影响(仍只在各自位置)。
- **`--summary` 锚点**: JSON 里含 `latest_ts`——`--since` 实际锚定的"最新一条日志时间戳"。agent 据它自校验"这个窗口对齐的是哪个时间", 尤其注意 GM 调时间(multiTimeOffset)会让日志时间≠墙钟, 别用墙钟去对。
- **`--diagnose`**: 独立健康检查(不 tail), JSON 到 stdout。**拿到空结果先跑它**: 若某源 `discovered:false`/`dx_error` 非空 → 源没被找到, 别下"无错误"结论。判"源活着"看 `files>0 且 discovered=true`; `latest_ts` 在活跃写入的文件上可能**瞬态为 null**(尾部块恰好无完整时间戳行), 重跑即恢复, 别单依赖它。
- **`--since` 的优先级与定位**: 给了 `--since` 就**按时间戳定位采集起点**(`--lines`/`--history` 不再决定回溯量)。主路径是**时间戳二分定位**——日志按行追加、时间戳单调不减时,几十次 seek 即可定位任意久远窗口的起点,**不受文件大小限制**(474MB 也能回看 2h)。二分失败(无时间戳行/非单调/超长行)才退化到**尾部 8MB 扫描兜底**,此时 stderr 打 `warning: ... 退化为尾部 8MB 扫描`——窗口前部旧行读不到,`--match` 可能漏掉窗口边缘命中。
- **`--count` 的统计范围**: 统计**全部读取量**(受上面 8MB cap 影响), **不受 `--lines` 限制**——`--lines` 只是正文输出条数上限。即 `count = 窗口内真实命中数`, `lines = 你这次想看几条`。
- **`--date` 的生效范围**: 只对含 `{date}`/`{YYYY}` 等占位符的源生效(path/pattern/**dx 命令里写了 `{date}`**)。dx 命令不含占位符时给 `--date` 无效(仍读当天), stderr 会打 warning 明示。dx 源看历史某天需 dx 命令本身支持日期参数。
- **实时日志做对照实验不可靠**: `--since` 锚定"最新一条日志时间戳", 每次调用间隔几秒窗口就漂移。确定性验证: `--match X --lines 100000 --count` 固定大读取量, 或 glob 源 + `--date` 锁历史。

### AI 排查工作流与坑（心得体会）

**工作流铁律：先看代码，再调 config。** 排查前先读代码，初步确认涉及哪些服务器 / 关键字，再打开对应源——不要一上来就全量调日志。配置分两份：
- `config.yaml`（主配置）：游戏大服用 dx 自动发现（scene/scenemgr/guild/match），微服务用 glob（team，在 `/ms/`）。默认只开开发常用服（scene/scenemgr/guild/match/team）；auction/public/http/activity/relation/bar 为低频服**注释**，涉及对应 bug 时取消注释。
- `config.ms.yaml`（微服务专用）：**去掉了 `DEBUG` 黑名单**。因为 Team/Bar 等微服务日志几乎全为 `[Debug]` 级，主配置的黑名单 `"DEBUG"`（大小写不敏感）会把它们全滤掉、`--focus team` 显示为空（不是没日志）。查微服务时用 `--config config.ms.yaml` 切过来。

**一个连贯的排查闭环**（probe → narrow → expand → conclude），每环都有便宜、可组合的工具：
- **probe**：`--count --since 5m` 探是否爆发；`--diagnose` 确认源活着。
- **narrow**：`--match <词>` 或 `--correlate player=<id>` 收窄到实体。
- **expand**：`--ctx-same N` 追**单进程因果**（比 `-C` 更能还原"这条错误背后的故事"，因为 `-C` 是全局时间邻居、会混进别的进程的行）。
- **conclude**：`--json` 编程级加工 + `--summary` 看锚点/自检。

**两条还不兴写进别处的坑**：
- `--source NAME:目录:pattern` 的 PATH 是**目录不是文件**——直连单文件要用 glob 目录 + pattern；要用 dx 源只能改 config。
- `--source` 临时加的源**同样受主配置黑名单约束**：主 config 的 `"DEBUG"` 黑名单会把微服务 `[Debug]` 行全滤掉，`--focus bar` 显示为空不是"源没发现"。查微服务切 `config.ms.yaml`。
- `--lines` 要 ≥ 目标行跨度，太小读不到周期性行（如 `GCInfo`）——这类行每隔几秒一条，`--lines 30` 会只取到最近 30 条而把更早的漏掉。

**游戏服务器专属认知**：跨进程跟一条逻辑链路，前提是那个 id 真的出现在多服日志里；playerId 偏 Scene 侧。若想跨服追请求级链路，需游戏在日志里注入稳定的 callId / requestId。

## 交互命令
| 命令 | 缩写 | 作用 |
|---|---|---|
| `/keyword <词> [<词>...]` | `/k` | 添加高亮词 (可一次多个; `re:` 前缀=正则) |
| `/clear` | `/clr` | 清除所有高亮词 |
| `/remove <词> [<词>...]` | `/rm` | 移除指定高亮词 (可一次多个) |
| `-C N` | `/context N`、`/ctx N` | 切换上下文模式; N 为行数, 也支持 `-C 5s`/`-C 1m` 按时间 |
| `/all` | `--all` | 切回全量显示模式 |
| `/level ERROR` | —— | 只保留 >= 该级别的行 (可用 `all` 取消) |
| `/trace <词>` | —— | 只显示所有源中含该词的纯净行 (无邻居; `off`/`all` 取消) |
| `/pause` / `/resume` | —— | 暂停/恢复输出 (恢复时补回错过的行) |
| `/blacklist <规则> [<规则>...]` | `/bl` | 临时添加黑名单 (可一次多个) |
| `/unblacklist <规则> [<规则>...]` | `/ubl` | 移除黑名单 (可一次多个) |
| `/list` | —— | 显示当前高亮词、黑名单、级别、模式 |
| `/save` | —— | **把当前高亮词 & 黑名单写回配置文件** (仅此命令落盘; 保留注释与其余字段) |
| `/reset` | —— | **重读配置文件**后重置规则+清空缓冲+重新跟踪 (中途 `/save` 过也能正确回退) |
| `/help` | `/?` | 内联帮助 |
| `/quit` | Ctrl+C | 退出 |

> **搜索**: `/关键词` 在缓冲区内搜索并跳到第一条命中, `n`/`N` 跳下一条/上一条 (支持 `re:` 正则)。**`ESC` 退出搜索**、清除命中标记。搜索跳转只是"到达", 不是过滤。
> **一次加多个**: `/k timeout player ERROR` 就同时加 3 个高亮词;`/clr`、`/rm a b`、`/bl x y`、`/ctx 5` 同理。

> **持久化语义**: 运行时的 `/keyword`、`/blacklist` 等增删**不自动保存**;
> 只有 `/save` 才把当前高亮词与黑名单写回配置。`/save` 采用**文本级替换**,只改 `blacklist:`/`keywords:` 两个键下的列表项,`log_sources`、注释、其他格式**全部原样保留**。
> `/reset` 会**重新读取配置文件**(而非用启动时的内存值),所以中途 `/save` 过也能正确回退到文件里的最新状态。

> **匹配规则**: 裸词 = 大小写不敏感子串匹配; `re:` 前缀 = 正则表达式 (同样大小写不敏感)。
> **黑名单同样支持 `re:`** 正则 (如 `/bl re:player=\d+`、配置里 `blacklist: [re:heartbeat.*err]`)。
> **级别自动着色**: ERROR/FATAL 红、WARN 黄、INFO 青、DEBUG/TRACE 蓝 (无需手动加高亮词)。

## Debug 工作流建议
- **排查服务器启动报错**: 服务器已经跑起来了、日志文件很大, 别再从头翻。直接回看启动窗口:
  ```bash
  python -m logtail --config config.yaml --since 5m
  ```
  启动后日志按时间戳窗口展示"最近 5 分钟", 一眼看到启动瞬间报的错。窗口内用 `↑` 回放。
- **只看错误行**: 启动后按 `-C 5` 切上下文, 或先配置 `keywords: [ERROR, Fail]` 让错误行着色突出。
- **黑名单去噪**: 把 heartbeat/timer_tick/keepalive 这类刷屏词加进 `blacklist`, 只剩有效日志。
- **给 AI 定位**: 用 `--agent` 让 AI 拿过滤后少量日志 (见上文), 配合 `--match ERROR -C 2 --since 10m` 拿到"错误+上下文+时间段"的完整线索。
- **多进程链路**: `dx` 配置把多个进程聚合成一条时间有序流, 跨进程因果一目了然。

## 滚动与定位
- **自动跟随底部 (默认)**: 新日志实时滚动。一旦你**向上滚**,视口即**冻结**,新日志追加不再把窗口拽回底部——方便向上翻看历史不被刷屏打断。
- **键位**:
  - `↑`/`↓` 逐行
  - `PgUp`/`PgDn` 翻页
  - `Home` 跳到最顶
  - `End` 或 **`G`** 跳回最底并恢复自动跟随
  - **`g`** 跳到最顶(输入框为空时)
  - **`Enter`(回车)** 执行命令并**自动跳到最新、解除冻结**
  - **鼠标滚轮**: 上滚回看历史,下滚前进;滚到底自动解除冻结
- 状态栏出现 `FREEZE` 表示已冻结;滚到底 (或按 `End`/`G`) 后消失、恢复跟随。

## 显示模式
- **全量模式 (默认)**: 显示所有通过黑名单的行, 高亮词着色突出。
- **上下文模式** (`-C N`): 只显示含高亮词的行, 连同其前后各 N 行; 窗口外的行显示但不高亮 (弱化)。

## 示例: 排查 bug 的典型流程
1. 启动工具, 日志实时滚动。
2. 操作游戏, 观察终端日志。
3. 发现可疑: 输入 `/k timeout` 加高亮。
4. 日志量仍大: 输入 `-C 5` 只看高亮词上下文。
5. 定位到具体错误行, 找到更精确关键词, 加新的高亮词, 调整窗口。
6. 输入 `/all` 回全量, 完整追踪链路。

## 开发 & 测试

**全量测试(推荐, 一条命令)**:
```bash
bash tests/run_all.sh
```

**分层测试**(全部零依赖, stdlib unittest):
| 层 | 命令 | 覆盖 |
|---|---|---|
| 单元 (145 个) | `PYTHONPATH=. python3 -m unittest discover -s tests/unit` | timeparse/levelparse/rules/correlate/models/config(含 /save 回写)/timeline(上下文·时间窗·搜索·trace)/reader(tail·轮转·history·since·dx·probe)/agent(dump 全分支) |
| 集成 (31 个) | `PYTHONPATH=. python3 -m unittest discover -s tests/integration` | CLI 子进程端到端: 全 flag 矩阵、stdout/stderr/退出码契约、monitor(含 SIGINT、BrokenPipe)、--diagnose |
| 模糊 (12 个) | `PYTHONPATH=. python3 -m unittest discover -s tests/fuzz` | 固定种子随机输入: 不崩溃/不变量(normalize 幂等、裸词 iff 子串、行数守恒、count==行数) |
| 冒烟 | `PYTHONPATH=. python3 tests/smoke_test.py` | 模块级快速冒烟 |
| Agent 回归 | `PYTHONPATH=. python3 tests/selftest_agent.py` | 确定性夹具端到端 (22 项断言) |

```bash
# 生成日志夹具 (真实文件, 供运行时产生流)
bash tests/make_fixtures.sh /tmp/lt_logs
```

在真实终端里跑一次看交互效果:
```bash
python -m logtail -s gateway:/tmp/lt_logs/gateway:*.log \
                  -s logic:/tmp/lt_logs/logic:*.log \
                  -s scene:/tmp/lt_logs/scene:scene_*.log
```
边运行时由另一终端向这些文件追加行/做轮转, 观察聚合与命令效果。

## 性能与约束
- 每个日志源一个后台线程轮询 (200ms), 主线程每 120ms 排空排序。
- 时序采用"本批内稳定排序", 延迟 < 200ms, 近似全局有序 (不追求严格全局排序)。
- 内存由滚动缓冲上限封顶, 不随运行时间无限增长。
- 黑名单在采集阶段应用, 高亮在显示阶段应用, 高亮不过滤日志。
