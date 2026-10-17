# 智能测试策略

本文件是「自动生成流水线在什么条件下能做什么」的**策略契约**。执行入口
（`scripts/flow.py`）按本文件实现；改动策略时两边必须同步。

## 1. 输入优先级

正式契约优先于 ShowDoc，ShowDoc 优先于用户口述；历史问题用于风险排序，不单独作为硬断言；
字段名推断只进入探索性测试。

## 2. 置信度规则

- 高置信度（>=0.8）：可进入硬断言。
- 中低置信度（<0.8）：标记为推断项，只进入探索性测试或待确认清单。
- 缺少契约、环境或页面基线时，不伪造成功结果，输出 blocked/N/A 原因。
- 落库口径：每条 finding 的 `confidence` 由 `add_finding()` 写入，默认 0.8；
  台账 CSV 的「置信度」列即此值，不再恒空。

## 3. 输入降级阶梯（有什么用什么，缺什么降什么）

| 已有输入 | 允许的动作 | 禁止的动作 |
|---|---|---|
| 只有契约 | 生成智能计划 + 声明式用例（`--plan-only` / 默认不执行） | 调用真实接口 |
| 契约 + 环境 | 安全读操作 | 写操作 |
| 契约 + 环境 + 显式测试环境声明 | 配置范围内的测试数据写操作（创建/修改/发布/删除/清理） | 越出 `environment` 配置范围 |
| 契约 + 环境 + 页面基线 | AB（前端口径）逐字段比对 | 无基线时凭空填 `page_value` |
| 无法确认测试环境 | 只出计划 | 任何创建/修改/发布/删除/清理请求 |

## 4. 写操作门禁（真正生效的那道闸）

写操作**不是**"用户随口说能写就能写"，必须同时满足：

```
environment.type == "test"      且     environment.write_enabled == true
```

- 实现位置：`adapters/sojump.py` 的 `is_write_allowed(env)`；
- **调用位置：`scripts/flow.py` 的 `write_gate()`**——判定依据是契约推导出的
  `op_type` 命中 `WRITE_OPS`（创建/新增/修改/更新/删除/发布/下线/导入/订阅/清空）；
- 未放行时的行为：**不抛异常、不静默继续**——写入 stderr 说明缺什么、保留已生成的
  用例与智能计划，正常退出，提示补齐配置后加 `--execute` 重跑；
- "域名看起来像测试环境"**不构成授权**，必须在执行配置里显式声明。
- 非 sojump 适配器（`--adapter rest`）不套用本门禁，按 adapter 自己的契约处理
  （见 `references/adapter_contract.md`）。

## 5. 执行档位（profile）

| 档位 | 筛选用例规则 | 典型用途 |
|---|---|---|
| `plan` | 不执行，只出计划与用例 | 先审阅再跑 |
| `smoke` | 仅 `priority == "P0"` 且 `enabled != false` | 快速判活、改完先跑一遍 |
| `full` | 全部 `enabled != false` 的用例（默认） | 常规深度测试 |
| `exploratory` | 全部用例（含探索性/低置信度） | 探索期、追求发现率 |
| `discovery` | 全部用例（同 exploratory）**且生成期放宽限额** | 尽量多发现问题 |

档位来源优先级：命令行 `--profile` > 执行配置 `execution.profile` > 环境变量 `WJX_PROFILE` > `full`。

`discovery` 与其余档位的区别不在"筛哪些用例"，而在**生成期**：它在用例生成**之前**
激活 `scripts/discovery_profile.py`，把探测广度的限额整体放宽（超长用例上限、
安全载荷每字段每类条数、字段族配额、AF 业务场景条数）。档位必须在生成前解析，
晚于生成再设置等于没设。放宽的只是**探测广度**，断言口径与覆盖率门禁不变；
默认档（conservative）的限额与接入前逐项一致，不传档位时探测广度零变化（增量接入）。

## 6. 覆盖率门禁（反静默缺漏）

- 门禁的"应测维度"来自 `case_generator` 自动推导的 `coverage_plan`，不再依赖人手声明；
- 只锁**必测**维度；选测维度放 `optional_plan`，不进硬门禁（防假警报）；
- **只有 `PASS` / `FAIL` 才算"执行过"**：`SKIPPED` / `N/A` 不计入已覆盖，
  否则把"没测"当成"测了"；
- 缺口记为 `COV-` 前缀的【缺陷(覆盖率缺口)】进入台账并高亮，分两级产生：
  - **逐条级**（`case_runner._skip_uncovered()`）：用例已生成却未执行时，跳过前先记 `COV-<用例id>`；
  - **维度级**（`coverage_gate()`）：收尾比对声明必测维度与实际执行维度，兜底整维度缺失。
- `WJX_STRICT_COVERAGE=1` 时升级为 `exit(2)`。

> **为什么必须两级**：维度级门禁问的是"这个维度有没有人跑过"。只要同维度有一条用例跑过，
> 它就判"已覆盖"——其余被跳过的检查全部静默。因此"整维度颗粒无收"靠维度级兜底，
> "局部有检查没跑"只能靠逐条级留痕，两者不可互相替代。

## 7. 测试阶梯

L0 探活 → L1 契约和参数 → L2 字段与错误语义 → L3 状态、幂等、权限、回读 →
L4 并发、事件和页面基线 → L5 历史缺陷回归。

## 8. 计划审阅与显式依赖（执行前先看一眼）

`flow.py` 默认多产两份计划：`plan.md`（人可读，四段：用例总览 / 待确认问题 / 前置依赖与关系 / 风险与不适用凭据）与 `dependencies.json`（机器可读，含原文证据与行号）。`--plan-only` 同样产出。

`plan.md` 的渲染可独立重跑：`scripts/plan_render.py --plan intelligent_test_plan.json --cases generated_cases.json --contract-path <契约路径> --out plan.md`——`--contract-path` 仅把契约路径展示进计划里便于对照，不参与解析；省略 `--out` 则打印到 stdout。

**待确认问题**由既有素材汇总而成，不新增事实：`blocked`（阻塞项）、`inferred_items`（低置信度字段）、`dependency_unknowns`（依赖定不位）、`uncovered_optional_detail`（选测零产出原因）、`payload_skipped`（被排除的安全载荷）。执行前处理这页清单，比跑完再返工便宜。

**显式依赖的取数纪律**（`scripts/dependency_scan.py`，契约通道）：

1. 只认契约正文**显式声明**的前置接口，不做字段名相似性推断，不跨文档推断；
2. 每条依赖必须带 `evidence`，且是契约原文的**连续子串**；
3. 正文写了"要先调某接口"却给不出 action 号时进 `unknowns` 留痕，**不生成依赖、不生成用例**；
4. 请求示例里的 `"action": "<自身action>"` 是文档自身标识，必须排除，否则每条契约都会把自己当成依赖自己。

**为什么不自动补全**：实测 193 篇契约只有 1 篇显式声明依赖——契约普遍不写前置关系。少一条依赖只是少测一个场景；猜错会调一个语义不符的接口，把"前置条件没满足"伪装成"已按依赖链验证"，比不测更糟。因此该能力只提高召回下限，**不承诺召回上限**。

### 8.1 运行实证通道（`scripts/precondition_scan.py`）

契约不写，但**真实失败响应会写**。执行后 `flow.py` 会就地筛一遍失败响应：
命中原话术（"请先调用"/"需先"/"前置条件"等）的失败整理成抽取提示词 → 大模型抽事实 →
机械校验 → **候选**依赖 → 人工确认后转正式。

| 通道 | 来源 | 触发条件 | 何时过期 |
|---|---|---|---|
| 契约声明 | 契约正文白纸黑字 | 不需要执行 | 契约改了 |
| 运行实证 | 真实失败响应 | **必须先真的跑失败一次** | 接口行为改了 |

取数纪律（比契约通道更严，因为响应是自由文本、误报风险更高）：

1. **规则先筛**：文案必须命中白名单话术才进模型——任何校验失败都送进去会造大量误报；
2. **模型只抽事实**：`evidence` 必须是该条响应原文的**连续子串**，不过即整条丢弃；
3. **定不位就不记**：说不出 7 位 action 号时只进 `unknowns` 留痕，不生成依赖；
4. **候选不生效**：只有 `confirmed` 才参与前置判定。命中后该用例记 `PRECONDITION_UNMET`，
   既不算失败也不算通过，单列点名且不进缺陷台账。

**命令行**（`scripts/precondition_scan.py`，各阶段可分开跑）：

```bash
python scripts/precondition_scan.py --prep --report <产物>/api_test_report_full.json --prompt-out prompt.md
python scripts/precondition_scan.py --auto --report <产物>/api_test_report_full.json --known-actions 1000107,1000108 --store runtime_dependencies.json
python scripts/precondition_scan.py --merge facts.json --report <产物>/api_test_report_full.json --store runtime_dependencies.json
python scripts/precondition_scan.py --manual deps.json --store runtime_dependencies.json
python scripts/precondition_scan.py --confirm-all --store runtime_dependencies.json
python scripts/precondition_scan.py --markdown --date 2026-10-17 --store runtime_dependencies.json
```

| 参数 | 说明 |
|---|---|
| `--report` | 执行报告 `*_report_full.json`（失败响应来源） |
| `--prep` | 生成抽取提示词 |
| `--prompt-out` | 提示词落盘路径（不传则打印到 stdout） |
| `--merge` | 并入大模型产出的事实 JSON |
| `--manual` | 人工登记的依赖 JSON（与 `--merge` 同格式）——跳过模型，但仍走同一套机械校验后并入 |
| `--store` | 依赖记录落盘路径（默认 `runtime_dependencies.json`）；`--merge`/`--manual`/`--auto`/`--confirm*` 时写盘 |
| `--known-actions` | 已知 action 全集（逗号分隔）；**给出后才对 action 号做存在性校验**，不给出时跳过该校验（仍按话术 + 邻近 action 号提取候选，误报风险更高） |
| `--auto` | 规则直提：话术 + 邻近 action 号明确时免 LLM 直接产候选（仍是候选，须确认） |
| `--confirm` / `--confirm-all` | 确认指定候选（`case_no` 或 `target_action`）/ 确认全部候选，转正式 |
| `--markdown` | 渲染成可写入知识库的「运行实证」节 |
| `--date` | 发现日期，写入依赖记录的 `generated_at` 与 `--markdown` 的渲染结果 |

`flow.py --execute` 侧用 `--runtime-deps <路径>`（默认 `runtime_dependencies.json`）指定上述产出的依赖记录，**只采信 `status=confirmed` 的记录做前置判定**；文件不存在则不做任何前置判定，行为与不传时一致。

**为什么确认这道闸不能省**：前置判定会让一条失败从缺陷台账里消失。判错的代价是真缺陷被
静默吞掉、外观却完全合理。所以 `attribution_engine.match_precondition` 对未确认的依赖
一律不采信，并按 `subject_action` 限定作用范围——一个接口的依赖不得吞掉另一个接口的失败。

**两条通道的关系**：互补而非替代。契约通道保下限（不写就没有），运行通道提上限
（用例没构造出前置场景就永远发现不了）。合并时以运行实证优先。

**风险因子 `cross_api_dependency` 的判据是"真的有显式依赖"**，不是"关系列表非空"：后者由 `relationship_inference` 依据接口名的 create/update/get 字样产出，度量的是"本接口自身是写入类"，与依赖无关，且会让大部分契约白拿 10 分、并在计划里制造"0 条依赖却计跨接口依赖 10 分"的自相矛盾。

相关文件：异常归因见 `references/attribution_rules.md`；适配器责任见
`references/adapter_contract.md`；缺陷模式见 `references/defect_patterns.md`。
