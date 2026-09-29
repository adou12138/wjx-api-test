# 声明式用例 Schema（替代手写 Python main）

用例从"写在 `main()` 里的 Python 代码"改为**声明式 JSON**，好处：非程序员可写、可版本化、
可复用、可被生成器批量产出（见 `auto_generation.md`）。

由 `scripts/case_generator.py` 产出、`scripts/case_runner.py` 执行。

## 顶层结构

```json
{
  "action": "1000107",
  "name": "创建AI主页",
  "endpoint": "https://...",
  "op_type": "创建",
  "is_intl": false,
  "coverage_plan": ["A","B","C","D","H","K","Y","AE","AB","AC"],
  "optional_plan": ["E","F","G","I","J","L","N","Q","V","W","AD"],
  "na_proofs": [{"dim":"AA","reason":"创建接口无列表数据，分页筛选不适用"}],
  "matched_patterns": [{"id":"P07","name":"空值被错误接受或错误拒绝","dimension":"B","cases":20}],
  "base_params": {"creater":"admin","title":"春季活动主页","page_type":0,"publish":true, "...": "..."},
  "cases": [ Case ]
}
```

- `coverage_plan`：**必测**维度，执行时自动装进 `COVERAGE_PLAN`，覆盖率门禁据此拦截缺口。
- `optional_plan`：建议覆盖的选测维度，**不进硬门禁**（避免选测项没用例刷出假警报）。
- `na_proofs`：不适用维度的自动证明（第 1.5 步要求"N/A 必须列出证明已判断"）。

## Case 字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | 是 | 用例编号，如 `B-MISS-html_content` |
| `name` | 是 | 用例名（现象口吻） |
| `dimension` | 是 | 维度编号（A/B/C/D/E/F/G/H/I/J/K/L、M–Z、AA/AE/AB/AC/AD、AF1/AF2/AF3），覆盖率统计用 |
| `category` | 是 | 分类，如 `参数校验-必填` |
| `action` | 是 | 接口 action |
| `params` | 是 | **业务参数**（公共参数 `encode/appid/action/ts/sign` 由 runner 自动补齐） |
| `expect.success` | 是 | true=应成功（走 `run`），false=应被拒绝（走 `assert_validation`，方向不可能写反） |
| `expect.err_keywords` | 否 | 期望命中的错误关键词；**异常用例必须给**，否则只验 result=false 会放行语义不准 |
| `expect.contract_keys` | 否 | 期望响应 data 必含字段；给了才做契约校验（避免浅用例） |
| `check` | 否 | 深度校验名，见下表 |
| `check_args` | 否 | 深度校验参数 |
| `priority` | 否 | P0/P1/P2 |
| `source` | 否 | 来源溯源，如 `pattern:P07` / `generator:required_missing` |
| `enabled` | 否 | 默认 true；false 则跳过但记入结果 |
| `needs` | 否 | `""` / `manual` / `ui_baseline` |
| `tier` | 否 | 用例分层：`business` / `probe` / `manual`，见下节 |
| `note` | 否 | 说明/证据 |

## 用例分层 `tier`

| 值 | 含义 | 典型来源 |
|---|---|---|
| `business` | 业务与语义驱动 | 往返验证、交叉验证、状态机、越权矩阵、AF 业务场景 |
| `probe` | 机械探测：「字段 × 固定算子」展开或载荷清单遍历 | `B-MISS-*` / `B-NULL-*` / `B-OVER-*` / `B-TYPE-*` / `B-LEN-*` / `G-*` |
| `manual` | 待人工补充的占位 | `TODO-*` / `needs=manual` |

**分层只影响报告统计口径，不影响执行与门禁**——probe 用例照常执行、照常判 PASS/FAIL、
照常计入维度覆盖率。分层的目的是让「业务覆盖了多少」这个真实数字能被单独看到：
机械展开会把用例总数与覆盖率撑高（实测某接口 127 条里 89 条是 probe，业务驱动仅 34 条），
若不分层，「用例数 127、维度全绿」会掩盖业务覆盖不足。

生成侧由 `case_generator._tier_of()` 自动标注并写入产物 `tier_stats`；
`--print` 与落盘摘要都会打印分层构成，probe 多于 business 时给出告警。

## check 取值

| check | 含义 | 执行方式 |
|---|---|---|
| `contract_diff` | 响应字段双向 diff（缺失/冗余） | 已实现 |
| `structure_complete` | 复杂结构返回完整性（Y） | 已实现 |
| `field_naming` | 响应字段命名风格一致（K7） | 已实现 |
| `name_consistency` | 请求↔响应命名风格比对（K7） | 已实现 |
| `url_alive` | 响应 URL 真能打开（C4） | 已实现 |
| `error_root_cause` | 不同成因错误信息必须不同（H6） | 已实现 |
| `ui_parity` | 页面基线 vs 接口（AB） | 需页面基线文件；无基线时记覆盖缺口 |
| `http_semantics` / `roundtrip` / `render_degrade` / `uniqueness` / `pagination` / `terminal_repeat` | 对应维度深度校验 | 已实现 |
| `transport_contract` | 传输层契约（Content-Type / 可追踪标识） | 已实现（响应头由 `call()` 旁路记录） |
| `enum_exhaustive` | 文档枚举读写一致性（Top6） | 已实现（枚举全集随用例下发） |
| `concurrency` | 并发重复提交不产生重复数据（J2/AC5） | 已实现（线程池，次数受上限约束） |
| `rate_limit` | 限流可观测性（Top9） | 已实现（连发观测，次数受上限约束） |
| `idempotent` | 相同请求重复提交 N 次（AC） | 已实现；未声明唯一键时只记观察，不硬判 |
| `state_transition` | 状态转换矩阵实测（E）：逐个目标状态提交 + 回读 | 已实现；只硬判「接受但回读值被改写」，合法性留给人工 |
| `trace_id` | 请求追踪（V）：标识存在、非空、逐次不同 | 已实现；响应无可识别追踪字段时记观察，不判缺陷 |
| `export_credential` | 导出下载（M）：响应须给出可用的下载 URL / 文件 token | 已实现；凭据为空判高，字段名识别不到记观察 |
| `cross_tenant_read` | 越权读配对对照（A）：本企业可读为对照，再看跨企业能否读到 | 已实现；本企业也读不到时不归因，只记观察 |
| `client_variant` | 跨客户端兼容配对对照（L）：基线请求与各 UA 变体对比响应字段集合 | 已实现；基线失败即不归因。黑盒降级版，真跨 SDK 兼容仍需多端环境 |
| `cross_list` | 交叉验证·列表侧：资源应出现在独立列表接口（C1/§9.4.2） | 需 `list_action` |
| `count_delta` | 交叉验证·计数侧：创建后 +1 / 删除后 −1（§9.4.2） | 需 `count_action`；基线在主请求**发出前**采集 |
| `event_delivery` / `timezone` / `batch_partial` | 事件推送 / 时区口径 / 批量部分失败 | 需外部观测通道或页面值，标 `needs=manual` |

> **次数上限是硬约束**：`concurrency` / `rate_limit` 会真实发出多次请求，实际次数取
> `min(check_args.times, CONCURRENCY_MAX_TIMES | RATE_LIMIT_MAX_TIMES)`，配置写错不会打爆服务。

> **跳过语义（重要）**：`enabled=false` / `needs=manual` 的用例 **不会被静默丢弃**——
> 会以 `SKIPPED` 或 `N/A` 状态记入 results 并带上维度编号，在汇总里点名，
> 既满足"N/A 要列证明"，也不污染通过率；**跳过前还会记一条 `COV-*` 覆盖缺口入台账**，
> 否则同维度只要有别的用例跑过，门禁就会判"该维度已覆盖"，把这次漏跑彻底盖住。

## 最小示例

```json
{
  "id": "B-MISS-html_content",
  "name": "必填缺失：html_content（应被拒绝）",
  "dimension": "B",
  "category": "参数校验-必填",
  "action": "1000107",
  "params": {"creater": "admin", "title": "t", "page_type": 0, "publish": false},
  "expect": {"success": false, "err_keywords": ["html_content"]},
  "priority": "P0",
  "source": "generator:required_missing"
}
```

## AF 业务场景用例

字段结构与上面完全一致（无需任何转换层），只有三点不同：

| 项 | 要求 |
|---|---|
| `dimension` | 必须是 `AF1` 主业务流 / `AF2` 业务规则校验 / `AF3` 业务组合场景 |
| `source` | 固定 `business_scenario`，便于在报告里与机械生成的用例分开统计 |
| `expect.err_keywords` | `success=false` 的用例**必须**给出期望命中的错误关键词 |

生成与门禁见 `scripts/business_scenarios.py`，规则说明见 `framework.md` §4 的 AF 行。

```json
{
  "id": "AF2-01",
  "name": "标题里出现「问卷星」三个字时创建被拒绝",
  "dimension": "AF2",
  "category": "业务场景-业务规则",
  "action": "1000107",
  "params": {"creater": "admin", "title": "问卷星活动主页", "page_type": 0, "publish": true},
  "expect": {"success": false, "err_keywords": ["title", "问卷星"]},
  "priority": "P0",
  "source": "business_scenario",
  "note": "契约明确标题不能包含「问卷星」，属业务规则而非参数格式校验"
}
```

## 语义层事实字段（可选，来自 `scripts/llm_semantics.py`）

契约经语义层接入后，字段与契约上会多出以下键。**未接入时这些键不存在，下游必须按"可能缺失"处理**——接入是增量不是替换。

事实并入后由 `scripts/llm_fact_cases.py` 兑付成用例（ID 统一 `LLM-` 前缀，用于分层归因）。兑付层
不认识的事实类型不产出用例；契约没说的一律生成 `needs=manual` 留痕而非自动断言。

| 位置 | 键 | 含义 |
|---|---|---|
| 字段 | `llm_role` | 业务角色，取值限定白名单（`llm_semantics.KNOWN_ROLES`）；不在白名单的事实会被拒收 |
| 字段 | `llm_constraints` | 业务约束：`alias_of` / `relative` / `is_url` / `fallback_chain` / `required_role_for_subaccount` 等 |
| 字段 | `llm_evidence` | 契约原文子串，可逐字回溯 |
| 字段 | `llm_confidence` | `explicit`（原文写明）/ `inferred`（由多个原文事实推出） |
| 字段 | `llm_implications` | 据此可设计的用例意图 |
| 契约 | `llm_relations` | 字段间关系：`alias` / `url_compose` / `fallback_chain` |
| 契约 | `llm_unknowns` | 契约未声明、禁止断言的项 |
| 契约 | `llm_verify_report` | 校验留痕：三态计数（`verified_count` 按值核验 / `unverified_count` 放行采信 / `evidence_only_count` 仅验证据）、`rejected` 拒收明细、`fact_utilization` 事实键消费情况 |

规则解析出的结构化约束（`max_len` / `enum` / `forbid` / `default`）**不被覆盖**——那部分规则解析得很好且零成本，语义层只做补充。

三态必须分开读：`verified_count` 才是「按契约值比对过」的条数，`evidence_only_count` 只说明
字段存在且 evidence 是原文子串，**不代表其约束有契约依据**。`fact_utilization` 的 `archived`
列出「抽取到了但没有消费方」的键，它们不构成兑现。

`alias` 关系经 verify 判明方向后，merge 会把方向回填到别名侧字段的 `llm_constraints.alias_of`
（显式声明优先，不覆盖）——模型只在 relations 里声明别名时，别名用例不会丢。
