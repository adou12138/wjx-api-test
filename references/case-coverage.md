# 用例执行率闭环：计划 vs 实际（v4.32.1）

## 为什么需要

wjx 已有两道门禁，但都是**声明 vs 触及**的维度级对账。读实现核验后，真实剩下的缺口是两个：

| 缺口 | 机制 | 后果 |
|---|---|---|
| ① **AF 业务场景整类缺席无声** | `flow.py` 主链路**未接入** `business_scenarios`；AF 要人工单独跑再 `--merge`。`coverage_plan` 是**维度驱动**，AF1 不是声明维度 ⇒ 门禁根本不看它 | 唯一能测业务语义的一类用例可能 0 条；报告如常，看不出"业务场景压根没测" |
| ② **没有逐条对账、没有计划清单落盘** | 无权威计划清单产物；`--only` 直接改写 `cases`（`case_runner.py:1247`）不留声明 | "生成 N 条、判了 M 条""哪些用例一条没留痕"在报告里查不到 |

> **已实现、本模块不重复造的部分**：`coverage_gate()` 已经按 `EXECUTED_STATUSES` 过滤——
> **只有 `PASS`/`FAIL`/`PRECONDITION_UNMET` 才算触及维度**（`PRECONDITION_UNMET` 确实发了请求拿到响应，只是结论既非通过也非失败，见 `api_test_runner.py:143` 的 `EXECUTED_STATUSES`），`SKIPPED`/`N/A` 计入 `touched_only`；全被跳过的维度
> 会被点名「假覆盖」**并记成 `COV-*` 缺陷**（`api_test_runner.py:1793-1849`）。
> 本模块做的是把它从 **stdout 提示**升级为**可落盘、可进报告、可 `--enforce` 的产物**，
> 并补上它按设计不管的两块：**逐条**对账与**类别**对账。

## 用法

```bash
# 基本对账
python scripts/case_coverage.py \
  --plan ./parity-test/cases.json \
  --results ./parity-test/api_test_report_full.json \
  --out ./parity-test/case_coverage.json

# 门禁（收尾时跑，失败即 exit 1）
python scripts/case_coverage.py --plan ... --results ... \
  --expect-categories AF \
  --exempt-categories "AF:本轮未启用大模型链路" \
  --min-rate 0.8 --enforce
```

| 参数 | 说明 |
|---|---|
| `--plan` | `case_generator.py --out` 的用例文件（权威计划清单） |
| `--results` | `*_report_full.json`（含 `results` 数组，每条带 `case_no`/`status`/`reason`/`dimension`） |
| `--expect-categories` | 本轮**应覆盖**的用例类别，逗号分隔。匹配 `dimension` 或 `category`（前缀匹配，`AF` 可匹配 `AF1`）。AF 用例的形态：`dimension=AF1`、`category=业务场景-主业务流` |
| `--exempt-categories` | 显式豁免，格式 `"key:理由"`，逗号分隔。**必须给理由** |
| `--min-rate` | 严格执行率下限（`--enforce` 时低于则失败） |
| `--enforce` | 门禁模式 |

## 三层对账

| 层 | 内容 | 相对既有门禁的增量 |
|---|---|---|
| ① 逐条 | `results.case_no` 与 `plan.id` 对齐；计划里有、results 里没有的 → `silentLost`（当前 runner 每条都留痕，应为空；**这条断言的价值在于将来一旦有路径绕过留痕会立刻暴露**） | **纯增量**（既有门禁只做维度级对账，不逐条点名） |
| ② 执行状态 | 只有 `PASS`/`FAIL` 算已执行；`SKIPPED`/`N-A` 剥出单列且 **reason 必填**；`skippedOnlyDimensions` 把「假覆盖」维度落成产物 | 既有门禁已在**判定**上做对了，但只 stdout 打印、维度级；本模块让它**可落盘/可进报告/可 `--enforce`**，并给到**用例级**明细 |
| ③ 类别 | 声明的类别必须**至少有一条真实执行**才算覆盖（AF 用例全 SKIPPED 不算覆盖）；豁免必须显式且单列 | **纯增量**（既有门禁按维度声明设计，AF 这类"整类用例"根本不在其视野内） |

## 双口径：不改既有门禁语义

`executed.total/rate` 是**严格口径**（只有 PASS/FAIL），`executed.looseTotal` 是**宽松口径**（计划内有任何 result 即算触及，与既有 `coverage_gate` 一致）。两者差额集中在 `skippedOnlyDimensions`。

**为什么不改 `coverage_gate`**：它的判定本身已经正确（只有 PASS/FAIL 算触及 + 假覆盖记缺陷），要改的只是"把 stdout 提示变成产物"。本模块**不修改** `case_runner.py` / `api_test_runner.coverage_gate()` / 任何生成器；既有门禁结论完全不受影响（有单测锁这条不变式），两者是"判定 vs 度量"的关系，不是替代关系。

## 状态词表

| 状态 | 含义 | 计入 |
|---|---|---|
| `PASS` | 执行且符合预期 | 严格已执行 |
| `FAIL` | 执行且不符预期（= 缺陷） | 严格已执行 |
| `SKIPPED` | 跳过（未启用 / 缺配置 / 需人工），**reason 必填** | 未执行 |
| `N/A` | 不适用（如缺 `cross_appid` 无法归因），reason 必填 | 未执行 |
| 其他/空 | 非法 | 未执行 + 进 `unexplained`（门禁拦） |

## 同一用例多条记录（重跑 / 回归并进）

`results` 是**纯append 列表**（`case_runner` 每次执行都 append，无覆盖机制），所以同一用例
出现多条记录是**正常产物**。本模块按「**真判定优先**」取状态：只要有一条 `PASS`/`FAIL`
就算这条用例真跑过——不能因为首条是 `SKIPPED` 就判它未执行（首条写了 reason 时还不落
`unexplained`，等于完全静默地低估执行率）。这条口径与 `detect_flaky` 一致（它也是按多条
记录判"不稳定"）。

多记录情况落进 `multipleRecords`：`caseIds` 列出所有多记录用例，`flakyCaseIds` 列出终态
不一致的（不稳定）。**不静默**——重跑导致的"同一用例两个状态"必须能被看见。

## 边界

- **不改任何生成器/执行器**：只加对账层，纯增量；
- **SKIPPED 不是缺陷**：它是"没测到"的诚实记录；只有**未写原因**才是门禁失败；
- **豁免必须显式且单列**：不进 missing、不进分子也不进分母，但产物里可见；
- **计划外 id 只告警**：results 里出现 plan 之外的 ID（旧用例文件残留）不阻塞门禁；
- **不虚报执行率**：分母＝计划总数（`--exempt-categories` 只豁免"类别覆盖"判定，不改用例级分母；用例被跳过仍计入分母）。

## 相关文档

- 门禁总览与档位：`intelligence_policy.md`
- 覆盖率门禁（既有，维度级）：`coverage-gates.md` / `SKILL.md` 收尾门禁段
- 产物形状：`schemas/case-coverage.schema.json`