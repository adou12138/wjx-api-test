# 契约驱动的自动用例生成（知识 → 用例）

## 为什么需要

skill 的知识（30 维度框架、19 条缺陷模式、适用性矩阵、断言 helper）原本**全部以文档形式存在**，
每次都要靠人/agent 手工「读文档 → 设计用例 → 手写 Python `main()`」。
覆盖率因此取决于当次注意力——这正是"做少了却静默产出完整报告"的结构性根源。

本模块把文档知识变成**可执行规则**，让「契约 → 用例」自动化：

```
契约 → 字段语义推断 → 适用性自动判定 → 缺陷模式匹配 → 字段级变异 → 声明式用例 → 执行 + 门禁
```

## 模块与职责

| 模块 | 职责 |
|---|---|
| `contract_parser.py` | 解析 ShowDoc Markdown / OpenAPI / JSON 契约 → 统一 Contract；从备注抽约束（最长 N 字符、默认值、枚举、禁用词） |
| `field_semantics.py` | 字段名+类型+备注 → 语义标签（time/url/id/status/enum/page/size/amount/html/count/i18n…）→ 建议断言 helper |
| `applicability.py` | 自动判业务域/操作类型/是否国际版 → 各维度必测/选测/不适用 → **自动产出 COVERAGE_PLAN 与 N/A 证明** |
| `pattern_engine.py` | 19 条缺陷模式编码为 `match(contract)` + `gen(contract)` 规则，自动匹配并生成对应用例 |
| `case_generator.py` | 编排以上 + 字段级变异（必填缺失/类型错/枚举越界/长度边界/内容注入）→ 声明式用例 JSON |
| `case_runner.py` | 执行声明式用例，复用 `api_test_runner` 全部断言 helper，并接通覆盖率门禁 |
| `flow.py` | 一条命令打通全流程 |
| `report_enhance.py` | 台账聚类去重 + curl 复现 + 按维度修复建议 |
| `pattern_feedback.py` | 缺陷回流成模式候选（`references/pattern_candidates.md` 待评审） |

## 用法

```bash
# 一条命令：从契约文档生成 + 执行 + 出增强报告
python scripts/flow.py --contract <契约路径> --execute

# 从知识库按 action 取契约
python scripts/flow.py --action 1000107 --execute

# 只生成不执行（先审阅再跑）
python scripts/flow.py --contract <路径>

# 分步
python scripts/case_generator.py --contract <路径> --out cases.json
python scripts/case_runner.py --cases cases.json --dry-run     # 看执行计划
python scripts/case_runner.py --cases cases.json --only B,H    # 只跑指定维度
```

## 覆盖率门禁如何被激活

`applicability.py` 自动推导必测维度 → 写入用例文件 `coverage_plan` →
`case_runner.py` 自动装进 `api_test_runner.COVERAGE_PLAN` → 收尾 `coverage_gate()` 比对
「声明必测维度」vs「实际执行触及维度」，缺口即记【缺陷(覆盖率缺口)】入台账。

缺口在两个粒度产生，缺一不可：

| 粒度 | 产生点 | 覆盖的情形 |
|---|---|---|
| 逐条 | `case_runner._skip_uncovered()` | 用例已生成却未执行（`needs=manual`、检查命中 `MANUAL_CHECKS`、无基线可比的 AB 项） |
| 维度 | `api_test_runner.coverage_gate()` | 整个必测维度没有任何 `PASS`/`FAIL` 用例 |

维度级只回答"这个维度有没有人跑过"：同维度只要有一条用例跑过，它就判"已覆盖"，
其余被跳过的检查全部静默——**局部漏跑只能靠逐条级留痕**。

设 `WJX_STRICT_COVERAGE=1` 可把缺口升级为 `exit(2)` 硬拦截。

> **只锁必测**：`coverage_plan` 只含必测维度；选测维度放 `optional_plan`（建议，不进硬门禁），
> 否则选测项没用例会刷出一堆假警报。

## 已知边界（诚实声明）

- 契约解析依赖文档规范：表格须含「参数/类型/说明/必需/备注」列；示例须为 ```json 块。
  解析不出来时会退化（字段为空），此时应改用声明式手写用例或人工补契约。
- 多数 `check` 已实现为可自动执行：`http_semantics` / `roundtrip` / `pagination` / `uniqueness` /
  `terminal_repeat` / `ui_parity` / `transport_contract` / `enum_exhaustive` / `concurrency` /
  `rate_limit`。后四者所需输入均已具备——响应头由 `call()` 旁路记录（`LAST_RESPONSE_HEADERS` /
  `call_meta()`）、枚举全集由契约解析随用例下发、并发用线程池、限流连发观测。
  发请求类检查的次数受硬上限约束（`CONCURRENCY_MAX_TIMES` / `RATE_LIMIT_MAX_TIMES`），
  防配置写错打爆服务。
- 留在 `MANUAL_CHECKS` 的只有三类**确实拿不到输入或观测通道**的检查：
  `event_delivery`（推送日志在接口响应之外）、`timezone`（需页面时间显示值对照）、
  `batch_partial`（单条接口不命中，属条件 N/A）。执行时跳过，但**跳过前先记 `COV-*` 覆盖缺口入台账**
  ——结果行上的 `SKIPPED` 不进台账，只有台账会被交付与复盘看到。
- 生成用例是**起点不是终点**：P0 用例可直接跑，仍标 `needs=manual` 的项须由测试人员补齐后再执行。
