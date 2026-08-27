# CLAUDE.md — AI 协作约束(本仓库)

修改本仓库代码的 AI(或人)必须遵守以下规则。违反 = 改动不可信。

## 铁律:改动必跑全量测试

**任何代码改动后,提交前必须跑通:**

```bash
bash tests/run_all.sh
```

约 2 分钟(单元 145 + 集成 31 + 模糊 12 + 冒烟 + agent 回归,共 210+ 断言)。
**全绿才允许 commit。有红必须修,不允许跳过、不允许删测试让它变绿。**

分调试某一层时可以单跑:
```bash
PYTHONPATH=. python3 -m unittest discover -s tests/unit          # 快, 28s
PYTHONPATH=. python3 -m unittest discover -s tests/integration  # CLI 端到端
PYTHONPATH=. python3 -m unittest discover -s tests/fuzz         # 固定种子可复现
```

## 架构红线(改坏任何一条 = 事故)

1. **非 agent 交互版(TUI, `logtail/tui.py`)是作者个人调试的核心工具。**
   改动它要格外保守:小步、可回退、跑完测试、真终端里验证过再提交。
2. **对外契约不可静默变更**(已有 agent 依赖它们):
   - stdout 只放日志正文/计数;错误、警告、诊断 JSON 只走 **stderr**
   - 退出码:`0`=成功(含 0 命中),`2`=配置/参数/正则错误,其他退出码都不允许出现
   - `--json` 每行字段固定为 `{ts, ts_seconds, source, level, text, seq}`
   - `--diagnose`/`--summary` 的 JSON 结构(`kind` 字段区分)
   - 行格式 `{时间戳} {来源:<12} {正文}`
   要改契约 = 新增字段可以,**删改已有字段/换位置必须先在文档声明并给迁移说明**。
3. **纯增量优先**:新功能加新 flag/新参数,默认值下行为与旧版完全一致。
   不做"顺手重构"、不做行为漂移。
4. **别猜因果,给尺子**(设计原则):工具不猜哪个进程是主角、不替用户排因果;
   锚点/健康/匹配量等 agent 依赖的语义必须显式可自校验(如 `--summary`/`--diagnose`)。

## 工作文件(别动、别提交)

- `config.yaml` / `config.ms.yaml`:作者的在用生产配置,含环境特定路径。
  不要修改、不要 `git add`(除非作者明确要求)。
- `DEBUGGING_SUGGESTIONS*.md`:评审记录,历史文档,按需追加不重写。

## 测试约定

- **零依赖**:只用 stdlib `unittest`,不引入 pytest 等新依赖。
- **确定性**:reader/timeline 涉及线程,用轮询+超时收集;模糊测试固定种子(SEED=20260827)。
- **修 bug 必须先有失败测试**(能复现的),再修产品代码——顺序不能反。
- 已知限制要写进测试注释(如 remove+新建复用 inode 的轮转盲区),不要默默绕过。

## 已知限制(不修,别"顺手修")

- GM 调时间(gmTimeOffset)导致的时间跳变:作者明确忽略。
- remove+立刻新建可能复用同 inode,轮转检测失效(与 GNU tail 一致;rename 轮转不受影响)。

## 提交规范

- 一批改动一个 commit,信息说清"改了什么 + 为什么 + 怎么验证的"。
- commit message 末尾带 `Co-Authored-By: Claude <noreply@anthropic.com>`(AI 改动时)。
