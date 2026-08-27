# BUG 反馈 0001 —— `--since` 大窗口读取不完备：dump 完备性依赖 `--wait`，默认 2s 读不完

> 场景：坑 2 治本（二分定位）后，`--since` 能定位到窗口起点、也能读到窗口最新行，
> 但**读取完备性仍取决于 `--wait`（默认 2s）**——大文件 + 大窗口下 dump 在读完窗口历史行前
> 就提前返回，导致 `--match` 假阴性、`--count` 探爆发偏小。
> 状态：**已修**（信号驱动收集，见文末修复记录）。

## 一、复现（命令级，稳定）

在 scene main 474MB、当日满量日志环境下：

```bash
# 1) --match 假阴性：窗口内明明有 walk 行，默认 wait 却返回 0
logtail --config config.yaml --agent --since 2h --match 'walk' --wait 2  --count   # → 0
logtail --config config.yaml --agent --since 2h --match 'walk' --wait 8  --count   # → 2
logtail --config config.yaml --agent --since 2h --match 'walk' --wait 15 --count   # → 2

# 2) --count 探爆发偏小：窗口行数随 wait 增长
logtail --config config.yaml --agent --since 2h --count --wait 2   # → 2332
logtail --config config.yaml --agent --since 2h --count --wait 15  # → 4016
logtail --config config.yaml --agent --since 2h --count --wait 30  # → 4003 (收敛)
```

- 窗口内 **walk 行真实存在**（`--since 1h --match 'walk'` 可读到两条：`16:25:02 MovementWalk._move`、`16:30:48 LC_MoveComponentNew._walk`）。
- `--since 2h` 窗口（15:07–17:07）**覆盖这两条**，但默认 `--wait 2` 读到 0，`--wait 8` 才读到 2。
- 读取量对比：`--since 2h --focus scene` 的行数 `--wait 2`=259、`--wait 15`=2347（窗口全量）。

**结论**：默认 `--wait 2` 时，dump 只读了窗口开头的一小段就返回，窗口前部（含 16:25/16:30）被漏掉。

## 二、根因定位

[logtail/agent.py](logtail/agent.py) `dump()` 收集循环：

```python
deadline = time.monotonic() + wait          # wait 默认 2.0
while time.monotonic() < deadline:
    batch = follower.queue.drain()
    for ln in batch:
        seen.append(...)
    if seen:      idle = 0
    elif saw_batch: idle += 1
    else:         idle = 0
    if idle >= 20: break                      # ~1s 无新行提前返回
    time.sleep(0.05)
```

- 二分定位把读取起点 `start` 定到窗口起点（如 15:07），这一步是对的。
- 但从 `start` 读到当前文件尾，其行数在 474MB 文件 2h 窗口下高达 **2347 行（scene 一个源）**、聚合 **4000+ 行**。读取线程每 0.2s 轮询读增量塞队列。
- dump 的 `deadline = now + wait`（2s）**或** `idle>=20`（约 1s 无新行）先到即 break → **在读完历史窗口前就返回**。

真正的语义问题是：**`--since` 大窗口时，dump 应该等待"窗口内历史行全部读完"再返回，而不是用固定 `--wait` 或 idle 提前 break**。固定 `--wait` 只适合"实时跟随窗口"的小窗口场景（如 `--since 5m`）；大窗口下它把"读不完"伪装成了"没有"。

## 三、影响（假阴性 —— 工具最想根除的）

1. **`--since 大窗口 --match X` 返回空，会被误读为"窗口内没有 X"**——这是最危险的一种，因为 `--diagnose`/`--summary` 只能证明"源活着"，证明不了"窗口读全了"。
2. **`--since 大窗口 --count` 探爆发偏小**，低估真实错误量（2332 vs 4016）。
3. 结果**不稳定**：同一命令两次运行（文件增长、读取时机不同），`--wait 2` 下读到的行数不同（本项目实测 scene 一次 224、一次 259），进一步放大误判。

## 四、我上一轮结论的修正

我此前用"`--since 8h --count` = 34366、stderr 干净"作为**坑 2 治本生效**的证据——**这个证据不严谨**：34366 同样是 `--wait 2` 截断的不完备值。`--since 8h --wait 15 --count` 真实全量是 **217746**（约 6 倍差距）。所以坑 2 二分定位"能定位到窗口起点"是成立的，但**读取完备性并未解决**，只是从"8MB 够不到起点"变成了"默认 wait 读不完窗口"。

## 五、建议修复方向

- **dump 改为"读到窗口内历史行全部消费完再返回"**：`--since` 大窗口下，用一个专门信号（读取线程读完 `start→EOF` 且队列排空）代替固定 `deadline`/`idle`。
- 或**按窗口内文件量自适应 `--wait`**（窗口越大默认 wait 越大），但治本不如上面的"信号驱动"。
- 保留 `--wait` 作为"实时跟随窗口"（`--since 5m` 等小窗口、持续增量）的兜底上限，两者语义要区分清楚。

## 六、测试建议

- 集成测试构造 >8MB 大文件 + `--since` 大窗口 + 靠前窗口内的标记行：断言默认参数下能读到（而不是 `--wait 2` 漏掉）——与坑 2 现有"8MB 回归"测试互补，那个测的是**定位**，这个测的是**读全**。
- 守护"大窗口读全"关键行为，维持 CLAUDE.md 的文档/help/测试三处同步契约。

## 七、修复记录

采纳第五节"信号驱动"方案：

- **reader**：`_FileFollower.ever_caught_up`（每个文件首次把 offset 追到文件尾后置位）+ worker `_scans` 计数（≥2 轮扫描才可判就绪，给慢/抖动 dx 两次发现机会）+ `LogFollower.backlog_ready()`。
- **agent.dump 两阶段**：backlog 阶段等 `backlog_ready()`（只受 30s 硬上限 `DUMP_HARD_CAP` 约束，**不受 `--wait` 限制**）；backlog 完成后进入实时跟随期（`--wait` 秒或 ~1s 无新行提前返回）。中途发现新文件（dx 后到）会回到 backlog 阶段。硬上限内 backlog 未读完 → stderr `warning: 历史窗口读取未完成`。
- **`--wait` 语义重定义**：实时跟随时长，不是总时长。
- 测试：慢 dx 源（`sleep 2`）+ 大窗口 + 默认 `--wait`，断言窗口起点行必读到（先红后绿）；backlog 超时必警告（unit）。
- 真实验证：`--since 2h --match walk --count` 默认 wait 从 **0 → 3**；`--since 2h --count` 从 **2332 → 4036**（收敛值），~4.5s，stderr 干净。
