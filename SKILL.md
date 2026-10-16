---
name: "wjx-api-test"
version: "4.39.7"
agent_created: true
description: 对任意 HTTP/OpenAPI/REST/RPC 接口设计并执行自动化测试，不绑定具体接口（无硬编码 action/endpoint/字段）。四条并列路径：**契约驱动自动生成**（契约→适用性→缺陷模式→字段变异+安全载荷清单→声明式用例→执行+覆盖率门禁；契约支持 ShowDoc/OpenAPI 3.x/Swagger 2.0 的 JSON 与 YAML，含 $ref 与嵌套展开）、**七步深度流程**（判适用性、按 31 维度框架推导覆盖、收紧断言执行、调其他接口交叉验证、数据工厂与清理、CSV 台账）、**冒烟**（批量探活，PASS/FAIL/N/A/ERROR）、**文档合理性审查**（不发请求，只审参数说明/一致性/错误码/边界/依赖关系）。适配器支持 REST/GraphQL/gRPC。AF 业务场景 prompt 自动注入 L5 产品知识（检索失败自动降级为纯契约，不影响主流程）；配套接口解析在同级扫描落空时从 L5 知识库捞候选（只进清单不自动填，供人工 --read-action 指定）。含 31 类维度框架、高频缺陷模式清单、18 条实测陷阱。`--profile discovery` 发现档把探测广度限额（超长上限 / 安全载荷条数 / 字段族配额 / AF 条数）集中到一处开关，用于"尽量多发现问题"；**默认只在指定单一环境执行**；**国际版维度默认不适用**。当用户提到"接口测试""测一下这个 API""按维度设计用例""记录问题单""接口回归""接口缺陷""审查接口文档""校验文档合理性""全量冒烟""冒烟测试""所有接口跑一遍""批量探活""接口清单巡检""测试计划审阅""接口依赖梳理""这个接口依赖哪些接口""测试中发现依赖""前置条件未满足""请先调用某接口""这个失败是不是缺陷"时使用。
---

# wjx-api-test — 接口测试工作流

把一次接口测试从"零散调接口"变成"有设计、有断言、有问题台账"的规范化过程。
**执行顺序铁律：先判适用性，再做维度覆盖；先做断言分层，再做结果判定；先看契约与状态，再看业务结果。**

| 路径 | 用户典型说法 | 入口命令 | 主要产出 |
|---|---|---|---|
| **契约驱动自动生成**（推荐） | 「按这份契约测一下这个接口」「尽量多发现问题」 | `scripts/flow.py --contract <路径> --execute`（要"多发现"加 `--profile discovery`） | `generated_cases.json` + `api_test_report_full.json` + `api_test_issues.csv` + `plan.md` |
| **七步深度流程** | 「深挖这个接口」「按维度设计用例」 | 见下方「工作流程」+ `scripts/api_test_runner.py` | 自写用例 + 统一 16 列问题台账 |
| **冒烟（批量探活）** | 「把这些接口全跑一遍看哪些通」 | `scripts/smoke_runner.py --config <cfg> --cases <cases> --out-dir reports` | 结果 CSV（PASS/FAIL/N/A/ERROR）+ 问题 CSV + `smoke_summary.md` |
| **文档合理性审查** | 「审一下这份接口文档」「参数说明 / 错误码写得合理吗」 | **不发任何请求**，按 `references/doc_review.md` 核验 | `api_test_doc_review_report.md` + 9 列 `api_test_doc_issues.csv` |

> **两条路由歧义必须先判清**：① 「审文档」≠「执行测试」——前者**全程不发请求**，判错会把一次静态审查变成一轮真实调用；② 「一批接口跑一遍」走冒烟（广度优先），**不是**对每个接口套七步深度流程（深度优先），两者不互相替代。

> **适用范围**：本 skill **不绑定任何具体接口**，文档里的 Sojump / 1000107 / 1000108 仅是**示例**。测任何接口只需换三样：① 契约（知识库/文档载入）、② 环境配置（`references/env_config.md` 登记）、③ 用例里的 action 与参数——框架、维度、断言方法、台账格式**完全复用**。

> **范围边界（UI 视觉还原不在本 skill 内）**：布局、间距、样式、字体、颜色、响应式走查属视觉比对能力（设计稿比对、浏览器截图 / DOM 检查）。本 skill 对 UI 只有三项协作职责：
> 1. **供数**——用接口数据工厂构造 UI 用例所需状态数据（`references/framework.md` §11.8）；
> 2. **AB 口径**——接口返回值 vs 页面显示值的一致性，须先登记页面基线（`references/ui_baseline.md`）；
> 3. **失败归属**——UI 用例失败时发一次单接口请求：接口正常则问题在前端，同样失败则为后端缺陷。
>
> UI 功能自动化由 agent-browser / playwright 承接，视觉对比由 figma-ui-parity 承接（设计稿展示值可作 AB 基线来源）；接口侧字段/枚举变更时**主动提醒 UI 侧核对 AB 口径**，UI 走查发现的"页面可/接口不可"转 AB1–AB6 复现并记台账。

**不适用场景**：纯前端 UI 测试；性能压测（单独立项，J 维度只做轻量探测，UI 感知阈值见 `framework.md` §J9）；纯数据库 / SQL 测试。

## 快速开始

```bash
# 契约驱动：生成 → 执行 → 出报告（推荐入口）
python scripts/flow.py --contract <契约路径> --execute
python scripts/flow.py --action 1000107 --execute                  # 从知识库按 action 取契约
python scripts/flow.py --contract <路径>                           # 只生成用例，先审阅再跑
python scripts/flow.py --contract <路径> --plan-only --profile plan
python scripts/flow.py --contract <路径> --execute --profile discovery   # 发现档：放宽探测广度，尽量多发现问题
python scripts/flow.py --contract <路径> --execute --dry-run       # 列计划但不发请求
python scripts/flow.py --contract <路径> --execute --config <执行配置.json>
python scripts/flow.py --contract <路径> --execute --retest <回归基线.json>   # 复测四态判定（FIXED/STILL_FAILING/REGRESSED/…）
python scripts/case_generator.py --contract <路径> --read-action <读接口action> --list-action <列表action>
                                                                          # 显式指定配套接口：开启真往返 + 越权读面 + 交叉验证
python scripts/flow.py --contract <路径> --execute --adapter graphql        # GraphQL 接口（HTTP 200+errors 判失败）
python scripts/flow.py --contract <路径> --execute --adapter grpc           # gRPC 接口（需先 pip install grpcio grpcio-tools）

python scripts/dependency_scan.py --contract <路径>                        # 单独梳理契约显式声明的跨接口依赖
python scripts/plan_render.py --plan intelligent_test_plan.json --cases generated_cases.json --out plan.md   # 单独渲染人可读计划

python scripts/precondition_scan.py --prep --report <产物>/api_test_report_full.json    # 从执行报告筛出疑似前置失败，生成抽取提示词
python scripts/precondition_scan.py --merge facts.json --report <产物>/api_test_report_full.json --store runtime_dependencies.json
python scripts/precondition_scan.py --auto --report <产物>/api_test_report_full.json --store runtime_dependencies.json    # 规则直提：文案里「前置话术 + 邻近 action 号」明确时免 LLM 直接产候选（仍是候选，须确认）
                                                                          # 机械校验模型产出的事实，记为候选（不生效）
python scripts/precondition_scan.py --confirm-all --store runtime_dependencies.json     # 人工确认后转正式（此后才参与前置判定）
python scripts/precondition_scan.py --markdown --store runtime_dependencies.json        # 渲染成可写入知识库的「运行实证」节

python scripts/case_runner.py --cases cases.json --dry-run         # 声明式用例执行器
python scripts/case_runner.py --cases cases.json --only B,H

# 用例执行率对账：计划 vs 实际
#   rate = 严格真判定（只数 PASS/FAIL）；looseTotal = 宽松口径（任一 result 即计，含 SKIPPED/N/A）
#   口径提醒：这两个数都与 coverage_gate 的「触及」口径不同（后者认 PASS/FAIL/PRECONDITION_UNMET），
#   三者不可混用；差额由 skippedOnlyDimensions 点名——见 references/case-coverage.md
python scripts/case_coverage.py --plan cases.json --results <产物>/api_test_report_full.json --out case_coverage.json
python scripts/case_coverage.py --plan cases.json --results <产物>/api_test_report_full.json \
    --expect-categories AF --exempt-categories "AF:本轮未启用大模型链路" --min-rate 0.8 --enforce
                                                                          # 见 references/case-coverage.md

# 产物回读校验（交付前自检：编码/表头/列宽/编号/行数/证据链列）
python scripts/artifact_readback.py --dir .                # 自动识别目录内台账与报告
python scripts/artifact_readback.py --issues api_test_issues.csv --report api_test_report_full.json
python scripts/artifact_readback.py --dir . --json readback.json      # 机器可读，供门禁消费
python scripts/artifact_readback.py --dir . --strict                    # warning 也视为失败

# 业务场景补充（AF 维度族，默认关闭，需显式启用）
python scripts/business_scenarios.py --contract <契约路径> --prep --out prompt.md
python scripts/business_scenarios.py --contract <契约路径> --save --agent-output cases.json --out af_cases.json
python scripts/business_scenarios.py --merge --from af_cases.json --into generated_cases.json

# 语义层接入（可选：让大模型读契约产出事实，机械校验后并入）
python scripts/llm_semantics.py --contract <契约路径> --mode prep --out facts_prompt.md
python scripts/llm_semantics.py --contract <契约路径> --mode merge --facts facts.json --out contract_enriched.json --report verify.csv   # --out 仅供审阅
python scripts/llm_semantics.py --contract <契约路径> --mode compare --facts pass1.json --facts2 pass2.json --out stability.json --report cmp.csv  # 双次抽取稳定性比对
python scripts/llm_semantics.py --contract <契约路径> --mode combine --facts shard1.json shard2.json --out facts.json  # 分片抽取结果合并（仅并集，merge 才校验）
python scripts/flow.py --contract <契约路径> --llm-facts facts.json --plan-only    # 生成用例走这里：内部会重做同一套校验与并入
python scripts/smoke_runner.py --config <cfg> --cases <cases> [--hooks <hooks>] --out-dir reports
WJX_BASE_URL=https://实际地址/实际路径 python scripts/api_test_runner.py   # 深度流程（自写用例）
```

**契约格式**：ShowDoc Markdown（.md）、OpenAPI 3.x / Swagger 2.0（.json/.yaml/.yml，支持 `$ref` 解析与嵌套对象/数组递归展开，字段名用点路径如 `data.items[].name`）、极简 JSON 契约——按内容自动分派，无需指定。

**地址约定**：测试地址一律经 `WJX_BASE_URL` 传入（取自契约文档『接口地址』），**禁止写死默认域名**；未设置时运行器启动即报错。

## 个人参数配置（分享友好 · 包内零个人数据）

本 skill **不写死任何账号 / 子账号 / appid / 域名 / corpid**，全部外置，**版本控制内**只有 `*.example.json` 占位模板。

> **例外（分享前必查）**：本地工作区可存在真实值 `assets/personal_config.json`——它已被 `.gitignore` 排除、**不进 git**，但若把整个 skill 目录打包或复制给别人，它会一起被带走。分发前务必确认它不在分发物里。

- **配置文件（推荐）**：复制 `assets/personal_config.example.json` → 填真实值 → 放**包外** `~/.workbuddy/wjx-api-test/personal_config.json`（或用 `WJX_CONFIG` 指路径）；真实文件**不要入库、不要随 skill 分享**。
- **环境变量（CI / 临时切换）**：`WJX_BASE_URL` / `WJX_APPID` / `WJX_MAIN` / `WJX_SUB` / `WJX_CORPID` / `WJX_CROSS_APPID` / `WJX_SIGN`。
- **优先级**：环境变量 > `personal_config.json` > 报错——必需参数（地址 / appid / 主账号）缺失时启动即报错，**不带任何默认个人值跑测试**。逻辑集中在 `scripts/personal_config.py`，深度与冒烟共用同一来源。
- **多套环境（region 分组 cn / intl）**：支持 `envs` 数组 + `default_env`；**先按 `--region`/`WJX_REGION` 过滤，再按 `--env`/`WJX_ENV`/`default_env`/首个 选定**。跨环境参数**仅在显式开启异常场景测试时**可用（`WJX_CROSS_ENV` / `--cross-env`）。

> 完整规则（跨环境纪律、占位符写法、实例 ID 按环境独立、冒烟配置多环境）见 `references/personal_config.md`；配置项 ↔ 接口参数字段 ↔ 中文含义见 `assets/配置参数说明.md`。

## 防呆门禁（反静默失败 / 反漏测 / 反矛盾）

对"输入给错/漏了"已有启动级硬拦截（缺参数 / 路径不存在 / 适配层缺函数 / 占位符未解析一律报错退出）。在此之上，针对"做少了却静默产出完整报告""输入自相矛盾"内置四道运行期门禁：

1. **配置矛盾拦截**（`personal_config.config_consistency_check()`，深度与冒烟共用，启动阶段）——region 矛盾、设了 `WJX_CROSS_ENV` 但跨环境配置为空 → **fatal，exit(2)**；域名 vs region 启发式（如 `region=intl` 却用国内域名）→ **warn**（`WJX_STRICT_CONSISTENCY=1` 升级为拦截）。
2. **覆盖率门禁（反静默缺漏）**（`api_test_runner.coverage_gate()`，收尾阶段）——应测维度由 `applicability.py` 自动推导——**该自动推导只在「生成用例 → `case_runner`」链路成立**（`case_runner.py` 读用例文件里的 `coverage_plan`）；**七步深度流程里自写用例跑 `api_test_runner.py` 时需自行声明 `COVERAGE_PLAN`**，该路径未引入 `applicability`，未声明则门禁打印跳过并返回；**只有 `PASS`/`FAIL`/`PRECONDITION_UNMET` 才算"执行过"**（`PRECONDITION_UNMET` 确实发了请求拿到响应，只是结论既非通过也非失败；不计入会让"跑过但被前置挡住"的维度被误报成覆盖率缺口），`SKIPPED`/`N/A` 不计入已覆盖，杜绝"把没测当成测了"；缺口记 `COV-*`【缺陷(覆盖率缺口)】进台账并高亮，**逐条与维度两级产生**——已生成却未执行的用例在跳过前先记一条（`case_runner._skip_uncovered()`），收尾门禁再兜底整维度缺失，两级不可互相替代；`WJX_STRICT_COVERAGE=1` 升级为 exit(2)；**只锁必测**，选测放 `optional_plan` 不进硬门禁（防假警报）。

选测维度本轮零用例时，产物 `uncovered_optional_detail` 逐条给出原因，不只是维度字母：

| 原因 | 含义 | 该怎么读 |
|---|---|---|
| `no_feature` | 契约无该维度特征（未满足生成器触发条件） | 本接口确实无需测 |
| `response_side_only` | 有特征，但字段仅在响应侧，无法构造请求 | 需换思路，不能算覆盖完成 |
| `not_implemented` | 该维度无生成器实现 | **能力缺口，不是本接口无需测** |

只列字母时这三类长得一样，能力缺口会被读成「本接口无需测」并永久留在报告里，所以点名必须带原因。

**L 跨客户端兼容**走 `dim_generators.gen_client_variant_cases()`：靠改 `User-Agent`
模拟移动端 / PC 端 / 小程序三种客户端特征，与基线请求配对对比响应字段集合。服务端按 UA
分支返回不同字段集时，只有对应那端会解析失败，单端回归永远抓不到。这是**黑盒降级版**——
不同版本 SDK 的序列化差异与字段弃用兼容仍需多端环境，用例 note 里已写明，勿当作完整覆盖。

3. **约束对账门禁（反约束级静默缺失）**（`scripts/check_key.py`，生成期算出、执行期出声）——维度门禁只能回答「边界维度有没有用例跑过」，回答不了「契约声明了 `activity` 的类型，有没有一条 `wrong_type:activity` 用例」。生成器漏掉某字段的某个约束时，该维度仍被其他用例覆盖，维度门禁**看不出来**——这是静默缺口。每条用例带身份证 `check_key`：机械用例 → 约束型（`missing_required:name` / `wrong_type:age` / `above_maximum:title` / `below_minimum:code`），语义与人工用例 → 来源型（`semantic:xxx` / `manual:xxx`，同样纳入编号体系以保证可追溯）；门禁拿契约算出的**约束全集**逐条对账，漏一条即点名并给出契约原值。

   两条硬边界：① **来源型不进对账分母**——语义/人工用例不兑现字段约束，塞进分母会让门禁永远不可能通过，进而被人关掉；② **只出声、不进缺陷台账**——缺的是测试设计完整性而非接口缺陷，且单接口可达上百条（实测某接口 108 条），进台账会淹没真缺陷，完整清单留在产物 `check_key.missing` 供审计。

4. **浅用例 / 空转检测**（反"报告看似完整但覆盖浅"）——仅断言 `result=true` 且无契约/增强断言的 PASS → 记低危观察并在汇总告警（`shallow_passes`）；`assert_non_empty()` 包裹自定义断言循环，集合为空导致循环 0 次恒 PASS 会被判无效（陷阱 17，`check_enum_exhaustive()` 已内建）；冒烟侧当前**没有**案例级「空案例」告警，也不响应 `WJX_STRICT_COVERAGE=`（`smoke_runner.py` 无此环境变量分支，其 `empty` 统计是响应**空值字段**而非案例级）；升级拦截仅覆盖深度流程；

> 设计原则：对"skill 自身输入错误"用硬拦截；对"做少了 / 静默漏测 / 输入自相矛盾"用门禁把风险从"靠人记"变成"靠代码卡"。默认温和（告警 + 记缺陷），仅在显式设 `WJX_STRICT_*` 时升级为 exit(2)。

## 产物回读校验（脚本跑通 ≠ 产物可交付）

前面三道门禁管的是"测得够不够"，这一道管**"写出去的台账还是不是那张表"**。

陷阱 7 要求台账用 utf-8-sig、逗号分隔、含逗号/换行/引号的字段用引号包裹——但那曾经只是**文档里的一句约定**，写盘代码没有任何机制保证自己做到。少一个 BOM、某列忘了引号包裹、列定义改了而渲染层没跟上，Excel 里就是整张表错位，而**脚本 exit 0、报告"看起来完整"**，要等开发打开文件才暴露。

`scripts/artifact_readback.py` 把约定变成可执行检查：读回自己刚写的产物，验编码 BOM、表头与列定义一致（顺序也算）、每行列数、编号非空且唯一、台账行数 vs `report_full.json` 里 `findings` 数、证据链七列是否真落进了 CSV。

```bash
python scripts/artifact_readback.py --dir .            # 有error → exit 1
python scripts/artifact_readback.py --dir . --strict   # warning 也算失败（或 WJX_STRICT_READBACK=1）
python scripts/artifact_readback.py --dir . --json readback.json   # 供门禁消费
```

`flow.py --execute` **执行完自动跑一遍**（结果落 `artifact_readback.json`），不用手敲。默认只出声不阻断——台账已经写出来了，回读的作用是让交付方看见问题，而不是把跑完的一轮变成失败；`WJX_STRICT_READBACK=1` 才升级为 exit(2)。**必须排在 `report_enhance` 之后**：增强会重写 CSV，放在它前面校验的是即将被替换的文件。

**采集类字段由运行时自动填充**：`run()` 与 `assert_validation()` 都用局部包装把本次请求的 `http_code` 与响应头里的请求ID/TraceID（`X-Request-Id` / `X-Trace-Id` 等常见头名）交给台账，`call()` 返回值与 `_record_response_headers` 旁路缓存是数据来源。用局部包装而非在各分支逐一传参，是因为 `code` 只在函数入口拿得到，散着传必然漏，而漏掉的表现恰好是"那几列恒为空"——不报错的静默缺失。取不到就留空（空值是可见的"没采到"，填占位值反而是假的）。

**为什么单独一个脚本而不是塞进 `coverage_gate()`**：门禁跑在写 CSV **之前**（缺口要进台账），回读必须跑在写盘**之后**，两者时序相反、职责也不同（一个判"该测没测"，一个判"写出去的对不对"）。塞在一起会让写盘动作发生两次。

**报出问题该怎么读**：

| 码 | 级别 | 含义 | 典型成因 |
|---|---|---|---|
| `NO_BOM` / `DECODE` | error | 编码不对，Excel 会乱码 | 用 `encoding="utf-8"` 而非 `utf-8-sig` |
| `HEADER_MISMATCH` | error | 表头与 `assets/finding_columns.json` 不一致（含**顺序**不同） | 改了列定义但映射表没同步；或两套写盘路径表头不同源 |
| `ROW_WIDTH` | error | 某行列数与表头不符 | 列错位，或含逗号/换行的字段没被引号包裹 |
| `BLANK_ID` / `DUP_ID` | error | 编号为空或重复 | 空编号无法复盘引用；重复编号会让复测指向错条目 |
| `ROW_COUNT` | error | 行数与 findings 数不符 | 有 finding 静默丢失——台账与报告对不上 |
| `EV_COL_MISSING` | error | 证据链列缺失 | 台账无法支撑开发复现 |
| `EV_ALL_EMPTY` | error | **全部**记录的采集类证据列（请求ID/TraceID/证据路径）为空 | 采集链没接上，开发无法复现 |
| `EV_PART_EMPTY` / `EV_LEDGER_EMPTY` | warning | 部分记录无采集类证据 / 记录类列（置信度/归因/复测状态）未填 | 个别条目没采到，或归因环节跳步 |
| `NO_PAIRED_REPORT` | warning | 没找到同前缀的报告，行数这次核不了 | 台账与报告前缀不一致 |

**台账与报告按前缀配对，不按时间**：`X_issues.csv` ↔ `X_report_full.json`。一次运行常留下多个前缀的产物（`api_test_` / `chain_` / …），按最新时间挑会跨运行配错对，比出**根本不存在的"finding 静默丢失"**。找不到同前缀就降级为warning 并明说行数这次查不出来，不猜。

**覆盖缺口之外的单列**：零缺陷是合法结果（不是缺口），空台账不会被误报成问题；报告文件缺失只降级为 warning 而非 error——它只用于交叉核对行数，不该拦住"只有台账"的单份交付。

## 契约驱动的自动生成（推荐入口 · 解决"做少了"）

本 skill 的知识（31 维度框架 / 19 条缺陷模式清单 / 适用性矩阵 / 断言 helper）原本只是**文档**，每次靠人"读文档 → 设计用例 → 手写 Python"，覆盖率取决于当次注意力——这是"做少了却静默产出完整报告"的结构性根源。现把这些知识变成**可执行规则**：

```
契约 → 字段语义推断 → 适用性自动判定 → 19 条缺陷模式清单匹配（`pattern_engine.PATTERNS` 另含 UI1 与 AA 两条，共 21 条，不在清单内） → 字段级变异 + 安全载荷清单 → 声明式用例 → 执行 + 覆盖率门禁
```

**安全载荷清单**（G 维度）：`assets/security_payloads.json` 按字段语义标签/类型挑选载荷类别（SQL 注入 / XSS / 路径穿越 / 命令注入 / CRLF / **内容安全：广告导流 / 违法违规 / 辱骂 / 色情低俗**），每类每字段限量生成，断言方向为「应被拦截」——被原样接受即缺陷，被正确拦截记「符合预期」（陷阱3）。内容安全类目的 `payloads[0]` 为字面样本、`payloads[1]` 为同句**变体绕过**样本（符号穿插 / 空格分隔 / 全角 / 拼音混写）——字面拦得住、变体漏拦即真实缺口；类目可自行扩展，`applies_to` 写对即自动生效（生成器按 `applies_to` 匹配，无需改代码）；涉政敏感类不内置样本，需从团队/监管词库接入。

**字段限额按族保代表**（`field_semantics.pick_by_family`）：空值四态（P07 前 6 个可选字段）、
响应 URL（前 5 个）、UI 代理（前 2 个）等字段数限额，不再按位置取前 N，而是先让**每个字段族**
至少一个代表入选、配额有余再按原序补齐（族 = `llm_role` > 语义标签 > 去常见后缀的词干）。
族内字段共享后端校验分支，按位置取前 N 会把排在后面的整个族砍掉——限额要砍的是
「同类项重复测几次」，不是「某一类分支整个测不到」。

**门禁如何被激活**：`applicability.py` 自动推导必测维度 → 写入用例文件 → `case_runner` 自动装载 → 收尾 `coverage_gate()` 比对「声明必测维度」vs「实际触及维度」。

**三条默认开启的深度链路**（单接口就能跑满，不依赖其他接口的业务知识）：

- **越权矩阵（A）**：按「身份 × 操作面」铺开，而不是只给一条签名错误。身份轴跨企业 appid / 子账号 / 主账号对照 / 跨企业+主账号双重矛盾，操作面分写与读。读面用例是**配对对照**——先用本企业身份自证可读，再看跨企业身份能否读到；本企业也读不到时不判越权（那种拒绝多半是缺参数，判成"隔离生效"是假的通过）。身份一律用 `<跨企业appid>` / `<子账号>` / `<主账号>` 占位符承载，配置缺哪一项就点名跳过，绝不填空串冒充。
- **交叉验证（C1）**：主接口说成功不算数。`companion_scan.py` 解析配套接口（命令行 > 契约显式声明 > 同级目录扫描 > L5 知识库候选，扫描只在业务对象名对得上时才采用并写清置信度；知识库候选带模糊成分，只进候选清单不自动填，供人工 `--read-action` 指定），拿到读接口就做真往返，拿到列表接口就验「创建后资源出现在列表 / 总数 +1」。拿不到就留空并在待补清单点名，不拿主接口响应假装验过。计数基线在主请求**发出前**采集——check 内两次取数都已落在操作后，差值恒为 0。
- **状态机与幂等（E / AC）**：有状态字段就跑**转换矩阵实测**（逐个目标状态提交 + 回读，产出接受/拒绝事实表）；合法性是业务语义不猜，只硬判「接受了但回读值被改写」这一确定缺陷。幂等与并发重复提交不依赖唯一键也能测：创建类接口重复提交产生多个对象只记观察（业务本就允许多条），结果漂移才判缺陷。
- **零产出维度补齐（`dim_generators.py`）**：编码国际化 I（多字节 / emoji 代理对 / 全角 / RTL）、组合交叉 F（多字段同时异常）、请求追踪 V（标识非空且逐次不同）、批量 X（100 条与混入非法项）、导出下载 M（须给出可用下载凭据），以及按**字段特征触发**的业务域专项 N 层级 / Q 角色 / W 附件 / S 分类 / T 账号 / O 通知 / P 分享 / R 报告 / U 导入。三条约束：契约没有对应字段就不生成（造出来的是噪音，还会让"该维度已覆盖"变成假象）；预期说不清的（如顶级节点 parent 为空是否合法）一律 P2 + note 说明"业务允许则记符合预期"，不制造假缺陷；识别不到追踪/下载字段时记观察而非缺陷。

**已知边界**：确实拿不到输入或观测通道的检查（事件推送需推送日志、时区口径需页面时间显示值、批量部分失败需批量接口）标 `needs=manual`，执行时跳过、但**跳过前先记 `COV-*` 缺口进台账**——结果行上的 `SKIPPED` 不进台账，只有台账会被交付与复盘看到，不静默丢弃；生成用例是起点不是终点。模块清单见 `references/auto_generation.md`，降级阶梯与写操作门禁见 `references/intelligence_policy.md`。

### 发现档（discovery：集中放宽探测广度）

规则引擎默认把"探测广度"的限额拧在保守值上，而这些限额散落在生成链的不同阶段
（超长用例上限、安全载荷每字段每类条数、字段族配额、AF 业务场景条数）——想"多探一点"
要分别改多处，且容易只改一半。`scripts/discovery_profile.py` 把它们收成一个档位，
`--profile discovery`（或 `WJX_PROFILE=discovery`）一处生效：

| 限额 | 默认档 | 发现档 | 影响 |
|---|---|---|---|
| 超长用例上限 | 5000 | 20000 | 让 max_len 数万级的字段也能构造真正的边界用例 |
| 安全载荷/字段/类 | 2 | 4 | 每字段每类多测字面 + 变体绕过样本 |
| 空值四态字段数 | 6 | 12 | 更多字段进入空值 / 缺失探测 |
| 响应 URL 字段数 | 5 | 10 | 更多 URL 字段做"真能打开"校验 |
| UI 代理字段数 | 2 | 4 | 更多字段做 UI 代理参数探测 |
| AF 业务场景条数 | 8 | 20 | 业务语义用例也随"多探"放大 |
| 大字段单探 | 关 | 开 | 对超限大字段追加一条"大内容健壮性"探针 |

三条硬边界：① **只放宽探测广度，不改断言口径**——放宽后用例的预期方向不变，
否则假阳性暴涨；② **默认档的限额与接入前完全一致**——不传档位时探测广度零变化
（增量接入；生成逻辑自身的修复不在此承诺范围内）；③ **放宽产生的用例不享受任何豁免**——
照常进分层统计、照常计入覆盖率门禁、照常可能被判缺陷。"多探"是增加探测面，
不是放松判定标准。

**复合类型的类型错误（`wrong_type`）**：契约写 `null / boolean`、`integer / string(int32)`
时，`type_bad_for` 按类型 token 逐个尝试，选一个**对每个成员都非法**的值注入——
若注入的值恰好命中某个联合分支被判合法，就会得到"类型错误被接受"的假缺陷，
因此所有候选都至少被一个成员接受时宁可跳过（返回 None），不冒险造假缺陷。
生成器补上这部分用例后，约束对账门禁里对应的 `wrong_type` 缺口随之闭合。

## 用例分层与造值语义化（避免"看起来覆盖很好"）

自动生成会产出两类用例，混在一起看会**高估覆盖**：

| tier | 含义 | 典型来源 |
|---|---|---|
| `business` | 业务与语义驱动 | 往返验证、交叉验证、状态机、越权矩阵、AF 业务场景 |
| `probe` | 机械探测：字段 × 固定算子展开、载荷清单遍历 | `B-MISS-*` / `B-NULL-*` / `B-OVER-*` / `B-TYPE-*` / `B-LEN-*` / `G-*` |
| `manual` | 待人工补充的占位 | `TODO-*` / `needs=manual` |

机械探测有覆盖价值（确实探到了某类校验是否存在），但**不代表业务场景被测过**。产物里给 `tier_stats`，`--print` 与落盘摘要都打印分层构成，**probe 多于 business 时告警**——此时「用例数」与「维度覆盖率」都是被撑高的，真实业务覆盖以 business 条数为准。分层**不影响执行与门禁**：probe 用例照常执行、照常判 PASS/FAIL、照常计入覆盖率。

造值同样做了语义化，避免一批用例打在同一个分支上：

- **类型错误**不再所有 string 字段统一填 `12345`，而是按字段序轮流取数字 / 数组 / 对象。后端对这三类非法值的处理分支不同（隐式转字符串 vs 直接报类型错），统一填一个值会产生一批重复的 PASS。数值语义字段固定用数字——数字转数值字段的隐式转换最容易出问题。
- **长度边界**用业务形态内容填充（如 `Overlong title ` 重复）而非 `'x'*n`，顺带覆盖转义与截断。刻意用 ASCII：中文的「按字符 vs 按字节」口径不一致（100 汉字 = 300 字节），会让「恰好 max_len 应成功」在按字节校验的后端上必然失败，那是口径差异不是缺陷，混进来会污染边界结论。
- **安全载荷**只对「内容会被存储 / 回显 / 拼接 / 渲染」的字段注入。标识类字段（创建者、枚举、各类 id）注入无风险含义，只会得到重复 PASS。判定不了时按内容型处理（宁可多测，不可漏测安全），被跳过的字段记入产物 `payload_skipped` 留痕，**不静默消失**。

## 业务场景补充（AF 维度族 · 默认关闭）

自动生成走的是「字段 × 变异算子」的机械展开（`creater=12345`、空值四连、`title` 100+1 字符），
能保证**结构覆盖**，但产不出"用户实际怎么用"的业务场景——这类用例人类测试员根本不会写，
也发现不了业务逻辑问题。`AF` 维度族补的就是这一层：

| 子维度 | 含义 |
|---|---|
| `AF1` | 主业务流：典型用户操作路径 |
| `AF2` | 业务规则校验：业务语义上的合法性约束（**与 B 不重叠**——B 管技术参数校验，AF2 管业务规则，如"标题不得含品牌词"） |
| `AF3` | 业务组合场景：多字段组合产生的业务含义 |

三步走（`scripts/business_scenarios.py`）：`--prep` 出 prompt → LLM 生成 JSON →
`--save` 过门禁落盘 → `--merge` 并入主用例文件。**产物与 `case_schema.md` 完全一致，
不需要任何转换层，合并后 `case_runner` 直接可执行。**

`--prep` 出的 prompt 会自动附带从 L5 知识库检索到的产品行为参考（页面路径、按钮名、
开关名、已上线功能口径），并明确声明**与契约冲突时一律以契约为准**、知识库没提的
不臆造——让业务场景对齐真实产品而不是凭字段名想象。检索失败或知识库不可用时
自动降级为纯契约 prompt，主流程不受影响。

```bash
python scripts/business_scenarios.py --contract <契约路径> --prep --out prompt.md
python scripts/business_scenarios.py --contract <契约路径> --save --agent-output <LLM产出.json> --out af_cases.json
python scripts/business_scenarios.py --merge --from af_cases.json --into generated_cases.json
```

**门禁**：底座是 wjx 自带三层（覆盖率 / 浅用例 / 空转，由 `case_runner` 执行），AF 另有六项——

- 借自 req2testcase 三项：数量纪律（条数必须等于 `expected_case_count`，不多不少）、占位符检测（"验证操作结果""功能正常"这类空洞短语直接 reject）、去重率 >50% 拒绝接受；
- C7.1 改造一项：契约实体覆盖——实体来源从"需求文本"换成**契约解析出的字段名与枚举**，覆盖不足只告警不阻断；
- 结构化用例专用两项：`dimension` 必须属于 AF1/AF2/AF3；`success=false` 的用例必须给 `err_keywords`。

**条数上限**默认 8 条（`WJX_AF_MAX_CASES` 可调）——业务场景每条都可能真实发请求，写接口会落数据，必须有硬上限。

**默认关闭**：未启用时 `AF` 标「不适用」且不进硬门禁，不会产生"没启用却被判覆盖率缺口"的假警报；启用后 `AF` 列入必测并受门禁保护。

## 语义层接入（可选 · 让大模型读契约，不让它逐条写用例）

规则抽取（`field_semantics.py` 的字段语义标签）能稳定拿下**结构化约束**——长度、枚举、禁用词、默认值，
零成本且可复现。但它读不出业务语义：契约白纸黑字写着"子账户必须有系统管理员或问卷管理员权限"，
规则看到的只是一个叫 `creater` 的字符串字段，越权矩阵只能靠硬编码 appid 去猜。

语义层补的就是这一层（`scripts/llm_semantics.py`）：

```bash
python scripts/llm_semantics.py --contract <契约路径> --mode prep --out facts_prompt.md   # 出提示词（自带可引用的原文摘录）
# 大模型按提示词产出事实 JSON
python scripts/llm_semantics.py --contract <契约路径> --mode merge --facts facts.json \
       --out contract_enriched.json --report verify.csv        # 校验后并入；--out 仅供审阅
python scripts/flow.py --contract <契约路径> --llm-facts facts.json --plan-only            # 生成用例时消费
```

超长契约 prep 自动按段落**分片**：原文超过 `--excerpt-limit` 时切成多片，每片生成独立提示词
（`facts_prompt_shard01.md`…），字段清单按「字段名出现在该片原文中」过滤，片内明示
「只对本片字段抽取」。每片交给大模型各产出一份事实 JSON 后，用 `--mode combine` 机械合并
（字段冲突保留先到并告警、unknowns 去重），再 `--mode merge` 做全套校验并入。

**为什么放在这一层而不是让大模型逐条生成用例**：事实层的错误**可以机械校验**——字段名不在契约里、
evidence 不是契约原文子串、数值与解析结果不符，全是字符串与集合运算，不需要模型也不需要人工；
用例层的错误要等执行才知道。把大模型放在能被校验的位置，它最大的弱点就被管住了。

**五条硬约束**：

- 每条事实必须带 `evidence`，且必须是契约原文的**连续子串**——不是子串直接 BLOCKED；
- 关系（`relations`）与字段同等严格：没有 evidence、或 evidence 不在原文里，同样 BLOCKED。
  关系是跨字段推断，字段名各自都合法，唯一能证明「两者确实有关」的就是原文；
- 字段名只能取自解析出的字段清单，臆造字段直接 BLOCKED；`alias_of` 必须指向清单里**真实存在**的字段，
  否则会产出一条针对不存在关系的等价性断言；
- 与解析结果不符的（长度漂移、枚举膨胀、必填性写反）整条丢弃，不做部分保留——半对半错的事实比没有更危险；
- 契约没说的一律进 `unknowns` 并标 `must_not_assert`，禁止猜测。猜出来的事实会变成假缺陷，比不写更糟。

**声称的值必须能在 evidence 里找到**。写了 `max_len=100`，原文里就该有 `100`；写了
`enum` 含 7，原文里就该有 7。找不到说明这个值是从字段名或行业惯例推出来的，采信它就会产出
契约里根本不存在的边界用例。这条对「契约没解析出该约束」的情形尤其关键：契约无值**不等于**
可以放行，**必须有据**才放行。

**校验结论分三态**，因为它们对应的可信度不同：

| 结论 | 含义 |
|---|---|
| `PASS_VERIFIED` | 至少一项约束与契约值**比对一致** |
| `PASS_UNVERIFIED` | 提交了可比对约束但契约无值，且该值 evidence 有据 → 放行但记账 |
| `PASS_EVIDENCE_ONLY` | **没有提交任何可机械比对的约束**，只校验了字段存在与 evidence 是原文子串 |

第三态不能省。只记两态时，那些只写了 `role` 而没提交任何可比对约束的字段会与「比对一致」同态——
一份典型契约里真带可比对约束的往往只有三五个字段，报告里的「核验通过 N 条」会被读成 N 项都验过值。
三态在 `--report` 与产物的 `llm_verify_report` 里分开列。

**校验拦不住的，报告必须说清楚**：evidence 校验只能证明「这段话在原文里」，证明不了
「这段话与它声称的约束有关」——`evidence="签名"` 配 `algorithm="unknown"` 照样通过。所以报告同时
给出 `fact_utilization`：哪些事实键真有消费方、哪些只是存档（`llm_implications`、`relative`、
`is_url` 等当前不驱动生成）。这类「抽取到了但没人读」的键不算兑现。

**摘录截断会自动分片**：契约超过 `--excerpt-limit`（默认 6000 字符）时，prep 不再截断丢弃，
而是按段落边界切成多片（见上）；单片模式下若被 `--excerpt-limit` 截断，提示词会写明
「摘录已截断、覆盖到第几字符」，要求模型把摘录区外的字段放进 `unknowns` 而不是凭字段名推断——
截断区外没有可校验的 evidence。

**降级**：不传 `--llm-facts` 时纯规则链路照常工作，行为与接入前完全一致——接入是增量不是替换。
接入失败（文件不可读、JSON 损坏、顶层不是对象）时**降级并出声**，不静默当作「这次没接」；
模型把 JSON 包在 ```json 代码块里或前后加了解释，会被自动提取，不白丢一次抽取结果。

**抽取稳定性可度量**：`--mode compare` 对同一契约的**两份**抽取结果做关键事实比对
（role / max_len / enum / alias_of / required / 关系集合 / unknowns 集合），报告一致率与分歧清单，
`confidence=explicit` 的分歧单独标出——explicit 层的分歧比 inferred 层严重，应优先人工复核。
一致性高不代表事实正确（两次一致地错仍可能，正确性靠 merge 的机械校验）；一致性低则说明
该契约的事实抽取不可靠，驱动硬断言的事实需人工复核后再用。

**并入与兑付是两件事**：并入由 `llm_semantics.py` 负责（回答「有没有」），兑付成用例由
`scripts/llm_fact_cases.py` 负责（回答「用不用」）。分开是为了能单独度量兑付率——事实并进了
却没人消费，表面上"接好了"实际没兑现，是最容易自欺的一环。

并入后字段上带 `llm_role` / `llm_constraints` / `llm_evidence`；契约级带 `llm_relations` /
`llm_unknowns`。并入采用**整体替换**语义：本次没有事实的字段会清掉上一轮残留，避免同一契约
被并入两次后留下「新关系 + 旧字段事实」的混合态。各类事实的兑付方式：

| 事实 | 兑付成 |
|---|---|
| 权限约束（如子账号需某角色） | 越权矩阵多出 `A08`「无权限子账号应被拒绝」——规则层看着字段名做不出来 |
| `fallback_chain` 取值兜底链 | 目标字段未传时的取值来源与默认值用例；relation 级与字段级两种表达，同一对字段只兑付一次 |
| `alias` 兼容别名 | 只走别名 / 只走主字段两条路径。方向由 verify 机械判定（role=alias 的一侧、或 `alias_of` 互证），判不出转 unknowns 留痕——**不按 between 数组顺序猜**；判明方向后若字段没写 `alias_of`，merge 兜底回填 |
| `forbid` 禁用词 | 传入禁用词应被拒绝（仅 evidence 里真有该词的；惯例推的词不兑付） |
| `default` 默认值 | 字段未传应落到声明默认值（仅 evidence 有据；回读比对落值与文档是否漂移） |
| `publish_flag` 开关字段 | 契约声明的默认值自动断言；另一取值契约未说可否显式传入 → 只留痕 |
| 状态字段枚举在契约外 | 留痕点名，禁止编造枚举值去断言状态流转 |
| `url_compose` 域名 + 相对路径 | 留痕给拼接公式；两个字段都不是绝对 URL，单字段断言必误报 |
| 语义角色（`llm_role`） | 字段名猜不出 token 时照样激活业务域维度，见下 |

业务域维度（N/Q/W/S/T/O/P/R/U）默认按字段名 token 命中。`dim_generators.ROLE_DIM_MAP` 补了
第二条命中口径：**字段名不含 token、但语义层把某个请求字段判成了等价角色**（如 `creater` 判为
`operator_identity`）时照样激活。映射表刻意开得很小，宁可少映射——映射过宽会让维度在无关接口上
被激活。语义激活的用例 `source` 带 `(llm_role)` 标记、note 里写明契约依据，因为契约未必明说
服务端会校验该引用，这类断言比 token 直接命中弱一档。只认请求字段：响应侧字段无法构造请求。

兑付层同样守 must_not_assert：契约没说的只生成 `needs=manual` 留痕，不做自动断言；推断出的
关系（`confidence=inferred`）在 note 里标明是推断，不引作契约原文。校验结果随产物落盘可审计。

## 计划审阅与依赖梳理（执行前先看一眼）

`flow.py` 除 `generated_cases.json` 外，默认多产两份计划，`--plan-only` 同样产出——那正是它最有用的场合：每轮都要付执行成本（写接口会落数据），跑完上百条再发现场景设计不对很浪费，先扫一页计划便宜得多。

| 产物 | 作用 |
|---|---|
| `plan.md` | 人可读计划，四段：用例总览 / 待确认问题 / 前置依赖与关系 / 风险与不适用凭据 |
| `dependencies.json` | 机器可读的显式跨接口依赖，含原文证据与行号；同时进 `intelligent_test_plan.json` 的 `dependencies` 字段 |

**依赖取数纪律**：只认契约正文**显式声明**的前置接口（如「请先调用修改问卷状态接口1000102，传入state=2」），**不做字段名相似性推断**；每条依赖必须带**契约原文连续子串**作证据；正文写了"要先调某接口"却给不出 action 号时进 `unknowns` 留痕，**不生成依赖、也不生成用例**——定不位就不调，猜错比不测更糟。契约没声明依赖时计划会写明"未显式声明"，并提示这**不等于本接口没有前置条件**。

**为什么不自动补全依赖**：契约很少写前置关系（实测 193 篇契约只有 1 篇显式声明）。相比按接口名猜出一条链，少一条依赖只是少测一个场景；猜错会调一个语义不符的接口，把"前置条件没满足"伪装成"已按依赖链验证"，比不测更糟。因此本能力**只提高召回下限，不承诺召回上限**。

依赖的原文证据同样进 `plan.md`，便于人工核对声明是否被正确理解。

### 运行实证通道（在测试中发现依赖）

契约常常不写前置关系，但**真实失败响应会写**。所以除读契约外，执行后还会就地筛一遍失败响应：
命中「请先调用 / 需先 / 前置条件」这类话术的失败被整理成抽取提示词，由大模型抽事实、
机械校验后成为**候选**依赖；人工确认后才转正式。

| 通道 | 来源 | 触发条件 | 何时过期 |
|---|---|---|---|
| 契约声明 | 契约正文白纸黑字 | 不需要执行 | 契约改了 |
| 运行实证 | 真实失败响应里的提示 | **必须先真的跑失败一次** | 接口行为改了 |

两条通道互补而不是替代：契约通道**保下限**（不写就没有，实测 193 篇只命中 1 篇），
运行通道**提上限**（用例没构造出前置场景就永远失败不了、也就发现不了）。
合并时**以运行实证优先**——真实证据强于文档声明。

**确认这道闸不能省**：命中后该用例的状态改为 `PRECONDITION_UNMET`，**既不算失败也不算通过**，
在报告里单列点名，且**不进缺陷台账**（记 observation 而非 issue）。也就是说它会让一条失败
从缺陷台账里消失——判错的代价是一个真缺陷被静默吞掉、外观还完全合理。因此候选依赖
一律不参与判定，只有 `confirmed` 才生效，跨主体也不误吞（`attribution_engine` 对此有硬校验）。
没有运行实证记录时，行为与接入前完全一致。

**执行前自动就位依赖契约**：加载到 confirmed 依赖后，执行前按 `target_action` 解析目标接口
契约并落盘 `runtime_dep_contracts.json`——先在**主契约同级目录**找（"契约集放一起"是推荐用法，
本地命中就不查库），再按 action 从知识库定位，每条记 `origin`（local/kb）；都取不到的进
`missing` 点名留痕。Agent 构造前置请求时直接用清单里的路径，不必再手动 `kb_load_contract`；
缺契约的依赖按「缺料点名」处理，不猜不伪造、不阻断执行。

## 执行范围约定（重要 · 默认只在指定环境测）

> **核心：测试只在用户当前指定的那一个环境进行，不需要多环境对比。**

- 用户说"测这个接口"时，默认就是在他给的那个环境（域名 / 路径 / appid / 账号）上把维度覆盖跑全，**不要求**再去其它环境跑一遍；**环境登记只登记当前要测的那一个**。
- **ENV 台账 / 多环境差异对比不是默认必做步骤**，仅在 ① 用户明确要求，或 ② 过程中确实观察到与"指定环境"有关的环境性差异（如某环境 404 / 权限不足而指定环境正常、限流文案）时才做。
- 不要为了"显得覆盖完整"主动多环境探活——那会把部署/授权差异误判为接口缺陷。若真出现，按陷阱 5 记 ENV 台账，但它是**可选**产物、不是交付门槛。

## 执行前的确认检查点

门禁管的是「做少了 / 做错了会被代码拦住」，检查点管的是「**不该由 agent 自行推定的决策**」。本表是**决策点索引**——只回答「哪些决定必须由用户拍板、什么时候问」，规则细节以各节为准（第 0 步环境、第 6 步清理、发现档、业务场景、跨环境对比）。

每一项都必须用 `AskUserQuestion` 取得**本次**明确选择后才可继续，**不得沿用上一次运行或历史对话里的选择**：

| 决策点 | 何时必问 | 不问就执行的后果 |
|---|---|---|
| **目标环境** | 每次开始前 | 误测生产环境 |
| **写操作授权** | 用例涉及创建 / 修改 / 删除 / 状态变更 | 污染数据；删除不可逆（`smoke_runner.py` 无默认保护） |
| **发现档 discovery** | 用户说「尽量多发现问题」但未明说放宽时 | 用例与写请求倍增，压被测服务 |
| **AF 业务场景启用** | 启用前（默认关闭） | 每条都可能真实发请求 |
| **数据清理授权** | 执行 `teardown` / 清理前 | 误删非本次创建的资源 |
| **跨环境对比** | 仅用户明确要求时（非默认） | 环境差异误判为接口缺陷 |

运行实证依赖另有一道独立闸门：`precondition_scan` 产出的候选依赖**只有 `--confirm` 转为 `confirmed` 后才参与前置判定**。原因是命中后用例状态改为 `PRECONDITION_UNMET`、**会从缺陷台账里消失**——判错等于静默吞掉一个真缺陷，且外观完全合理，所以候选一律不生效。

## 工作流程（七步落地）

### 第 0 步：登记当前指定的环境
只登记**用户当前要测的那一个环境**：从接口文档『接口地址』取 **BaseURL（禁止写死默认域名）**、路径、encode、appid、sign（是否校验）、主/子账号，写入 `references/env_config.md`（多域名指向同一后端时合并记录）。**地址经 `WJX_BASE_URL` 传入、不写死进脚本**。

### 第 0.5 步：从知识库载入接口契约（可选，推荐）

> **适用范围**：`scripts/kb_load_contract.py` 仅适用于含 `project/showdoc_api_docs/` 目录结构的 L5 知识库。若不是此结构（纯文件目录 / Confluence / Swagger），跳过此步，直接让用户粘贴契约文档。

`python scripts/kb_load_contract.py --kb <KB_ROOT> --action 1000107`（或 `--keyword "AI主页"`；`--out contract.md` 落盘）。`--kb` 可省略，依次回退 `KB_PATH`、skill 同级目录的 `qa-req2testcase-generator/user_knowledge`。**加 `--endpoint` 从契约「接口地址」字段自动抽出真实 URL（打印 `ENDPOINT: <url>`），第 5 步的 `WJX_BASE_URL` 必须用它**。匹配策略：先按文件名 `[1000107]` 精确匹配，再用 `_vector_index/meta.json` 兜底模糊检索（纯标准库）；检索为空则回退"用户粘贴契约"。

### 第 1 步：梳理接口契约
提取动作 / 路径 / 方法、必填与可选参数、类型、枚举取值、长度/数值边界、响应结构（成功 data 字段、失败 result/errormsg），写入 `references/framework.md` §9.1 接口信息表模板。标清鉴权模型（签名/Token/AppID/子账号）与状态机（创建→发布→暂停→更新→删除）。

### 第 1 步（审查模式变体）：接口文档合理性审查

> 当用户意图是「审查 / 校验接口文档是否合理」而非「执行接口测试」时，**进入文档审查模式：不发起真实调用，只审文档原文**。结论复用 31 维度框架，问题记入文档审查台账（9 列，见 `references/doc_review.md`，与执行侧统一 16 列台账不同源），并与后续测试衔接。

审查六个面：① **字段参数说明**（请求+响应逐字段：名称 K7 / 类型 B,K / 必填 B1 / 取值范围 B2,B3 / 默认值 B9 / 单位 K / 示例 K9,B 是否齐备准确）；② **前后一致性**（请求↔响应字段名、多处描述、跨接口命名风格 K7、向前兼容 L2）；③ **错误码与错误信息**（完整性、码↔文案映射 H9、HTTP 状态码与业务码区分、根因不掩盖 H6、未声明码）；④ **边界条件**（空值/超长/越界/特殊字符、分页 AA、并发、时间边界）；⑤ **字段间依赖关系**（条件必填、互斥、联动、组合约束——未说明即记「依赖关系缺失」，高优先级）；⑥ **歧义/缺失/不合理**（模糊措辞、空白项、示例与说明冲突）。

输出 `api_test_doc_review_report.md` + `api_test_doc_issues.csv`。**纯文档缺陷**反馈文档 owner；**文档与实现不一致**记「需求确认」转测试并作为后续优先验证项。四种契约来源与维度映射见 `references/doc_review.md`。

### 第 1.5 步：接口适用性判断（必做，决定哪些维度适用）
加载 `references/applicability_matrix.md`，按五步法判断：① 识别业务域 → ② 识别操作类型（查询/创建/修改/删除/导出/导入/订阅/配置）→ ③ **判断是否国际版场景** → ④ 从 §4 矩阵筛选适用维度 → ⑤ 不支持的功能标注 **N/A**（不计入通过率，但必须列出证明已判断）。

> **核心原则**：不是每个接口都支持所有功能——创建资源接口不适用"导出下载"（M），查询接口不适用"状态机"（E）；强行套用只会产生误报。操作类型对应表见 §1，反例见 §2，N/A 规范见 §3，**国际版判定见 §0（默认国内版，国际版维度全部标 N/A）**。
>
> **易漏提醒**：**AB（前端-接口口径一致性）不体现在操作类型里**——只要接口在前端有对应操作就必须测，纯接口视角测不出前后端不一致（历史缺陷占比约 8.4%，第二高发）。测 AB 前先拿**页面基线**（人工页面操作 / 浏览器自动化抓取 / 设计稿三选一），无基线的 AB 项只能记缺口，**不得凭空填预期值**。

### 第 2 步：按 31 维度设计覆盖（A–L 通用 + M–Z 业务域专项 + AA–AE 通用补充）
加载 `references/framework.md`，把接口套入 31 大类，逐类判定覆盖状态（强/部分/缺口/排除/**N/A**），用 §4 覆盖率矩阵记录。

- **M–Z 专项**只在接口属于对应业务域时测（第 1.5 步判断）；**AA–AE 补充**按接口形态判定：AA 分页（列表接口必测）、AB 前端口径（有页面的接口必测）、AC 幂等唯一性（写接口必测）、AD 事件回调（触发事件的接口必测）、**AE 认证/传输补充（认证生命周期、请求头与传输契约、mTLS、重放时间窗；`applicability.py` 六种操作类型的必测元组均含 AE，不适用时的默认理由见其 `NA_REASON["AE"]`）**；**国际版维度默认全部标 N/A**。AF 为业务场景维度族，默认关闭、启用后必测（见下文「业务场景补充」一节），不计入上述 31 类。
- **优先补齐"假通过"与"假失败"两类盲区**：C 字段语义真值（更新后回读）、E 状态机、H 错误语义具体性（含 H6 根因不掩盖）、G 安全（含内容安全）、**B8 过度校验（合法输入被误拒）**。
- 用例用 §3 三要素模板描述（输入要点 / 观测点 / 预期行为 / 优先级 / 依赖环境 / 可自动化）。

### 第 2.5 步：过一遍高频缺陷模式清单
加载 `references/defect_patterns.md`，按 12 条 Top 模式逐条确认是否可能命中，命中的转成用例挂到对应维度下——补齐"框架想到了、但没想到具体怎么坏"的盲区。**回归复测时改过「回归复测专项」**：先对照三类套件自误报陷阱（过期期望 / 空转假通过 / 硬编码 finding）。

### 第 3 步：收紧断言 + 辅助验证（最关键，决定能否测出问题）

- 异常用例**不要只看 `result=false`**，必须断言命中文档化的**错误关键词**（`err_exact`）；未配 `err_exact` 的用例即使被拒绝也判「待确认（高危）」而非 PASS——拒绝原因可能是未登录/缺参数而非目标校验生效。成功用例校验响应 **data 契约字段与类型**，不只看 `result=true`。
- 所有请求校验 **HTTP 状态码**：5xx 一律记问题（即使业务"正常"）；4xx 需校验**状态码语义**（400/401/403/404/409/429）与业务错误码一致（§9.7）。**401（未认证）与 403（越权）混用即为缺陷**。
- **过度校验也要断言**：文档标注为可选/可空的字段，传 `null`/空串/不传 → **应成功**，被拒即记缺陷。最高发触发点是"**读接口返回的结构直接回写给写接口**"。用 `assert_no_over_validation()`，见陷阱 14。
- 连接/网络异常自动重试（次数由 `RETRY_COUNT` 控制），重试后仍失败归 **ENV/网络问题**，不计入业务缺陷。
- **调用其他接口辅助验证**（核心能力）：创建/修改/删除/状态变更后，调独立查询/列表/统计接口交叉验证（矩阵见 §9.4.2）——创建后验详情字段一致 + 列表出现 + 计数 +1；修改后验字段生效且未传字段保留原值；删除后验详情不存在 + 列表消失 + 计数 -1；状态变更后验详情状态字段 + 公开访问接口生效；导出后验下载链接可访问；批量操作后验成功项生效、失败项不变。三层次：存在性 → 字段一致性 → 副作用完整性，**至少做到第 2 层**；辅助接口失败先归因（环境/同步延迟/权限/真实缺陷，见 §9.4.4），不直接判主接口缺陷。helper：`verify_by_query()` / `verify_by_list()` / `verify_absent()` / `verify_count_delta()` / `chain_verify()`。
- **按维度选用断言 helper**（`framework.md` §2 场景族末尾标注）：AA `check_pagination()`、AB `check_ui_parity()`（`page_value` 来自页面基线登记表，禁止凭空填）、AC `check_uniqueness()`、AD `check_event_delivery()`、**AE `check_transport_contract()`（请求头与传输契约、认证生命周期；AE 为全操作类型必测，不得因文档未写而跳过）**、H9 `check_error_code_mapping()`、命名 `check_field_naming()`、国际化 `check_error_i18n()`/`check_currency()`/`check_datetime_tz()`（后两者仅国际版）、HTTP 语义 `check_http_semantics()`。**适用维度必选对应 helper**。

### 第 4 步：问题记录（CSV，现象化）
按 `references/issue_format.md` 的**统一 16 列**记录（基础 9 列 + 证据链 7 列：置信度 / HTTP状态码 / 请求ID / TraceID / 证据路径 / 归因 / 复测状态）。**维度列填触发该问题的维度编号**（如 `B8`/`AA3`/`H9`），便于按维度统计缺陷产出、反哺高频模式清单。描述用**现象口吻**（"写了脚本也能保存"而非"未做 XSS 过滤"）。安全拦截类记「符合预期」，二者都**不是缺陷**；仅在确实做了多环境对比且观察到差异时，才记「环境风险(ENV)」。runner 自动导出 `api_test_issues.csv`（utf-8-sig 逗号分隔，Excel 不空白）。

> **铁律——请求 Body 必须含公共参数**：问题单「请求Body」列记录**真实完整报文**，必须含 `encode`/`appid`/`action`/`sign`/`ts`（正/反例见 `issue_format.md`），开发要能原样复现。运行器 `add_finding()` 已对含 `action` 的报文自动补齐；**独立脚本必须自行补齐**（可调用运行器导出的 `complete_request_body()`），否则就是不完整台账（陷阱 18）。

### 第 5 步：在指定环境执行
在 runner `main()` 编写用例（参考现有示例骨架：创建→更新→异常→安全→鉴权），运行 `python scripts/api_test_runner.py`。**测试地址从契约的实际『接口地址』取**，用环境变量传入：`WJX_BASE_URL=https://实际地址/实际路径 python scripts/api_test_runner.py`。runner 不内置任何默认域名或路径。**只在当前指定环境跑**；与"指定环境"无关的环境性异常（连接失败、限流）先归 ENV/网络问题、重试隔离，不计入业务缺陷。

### 第 6 步：复盘、收敛结论与数据清理
① 核对"是否还有假通过/假失败盲区"（§6 清单，含过度校验与前后端一致性）；② 待确认项逐条给结论（缺陷 / 符合预期 / 已确认非问题 / ENV）；③ 输出回归报告（用例数 / 通过 / 失败 / 问题数）；④ **清理测试数据**（§11 规范）：统一前缀 `TEST_AUTO_<用例号>_<随机串>`、工厂函数构造前置数据、`try/finally` 中调 `teardown()`（另有崩溃安全的 `register_cleanup()` / `run_cleanup_manifest()` / `--cleanup-only`）、每轮按前缀扫描孤儿残留、并发线程各用独立资源**禁止共享同一 ID**（否则产生"数据串了"的假缺陷）、不可删资源记入"环境残留清单"。

**警惕两类误判**：把"被正确拦截"当缺陷；被防重提交/限流污染成"不稳定误拦"（复测时正文加随机串 + 间隔 ≥0.4s 绕过）。

## 关键陷阱速查（实测血泪，务必遵守）

> 每条背后的实测案例与正确做法见 **`references/traps.md`**；设计用例前、复测前、结果归因时读一遍。

1. 不要只验 `result=true`——必须用独立查询接口回读验证数据真落库。
2. 异常用例必须命中错误关键词（`err_exact`）；未配置时被拒也判「待确认」不判 PASS（`assert_validation` 配了却未命中则判 FAIL）。
3. 安全拦截 ≠ 缺陷（`<script>` 被拦记「符合预期」）。
4. 内容安全 ≠ 代码安全：接口常防可执行代码但**不防违规词**，是真实缺口（N1/N2 类）。
5. 多环境差异记 ENV、不擅判——但**多环境对比本身不是必做步骤**。
6. 别被限流 / 防重提交污染结论（复测加随机串 + 间隔 ≥0.4s）。
7. CSV 用 utf-8-sig 逗号分隔、含特殊字符用双引号包裹，否则 Excel 错位；交付前用 `artifact_readback.py` 回读校验。
8. 连接异常重试后仍失败 → ENV，不计业务缺陷。
9. 测试地址必须取自接口文档『接口地址』，禁止写死默认域名。
10. 不是每个接口都支持所有功能——先判适用性，不支持标 N/A，不强行套用。
11. 辅助验证要用**独立读接口**，不要用写接口验证写接口。
12. 并发/性能(J) 必须先探明并尊重服务端限流阈值，否则把限流误判成"并发串数据"。
13. 参数校验必须断言"**应被拒绝**"——严禁把非法输入配成 `expect_success=True`（会掩盖缺陷）。
14. 反向盲区：**过度校验**——可空字段传 `null`/空串被拒即为缺陷，别归因为"需求如此"。
15. 纯接口视角测不出前后端不一致（AB），必须以**页面基线**为预期值。
16. 错误信息要验"根因没被掩盖"——不同成因必须给不同错误信息，且不留未渲染占位。
17. 回归复测时**套件自身是最大误报源**（过期期望 / 空转假通过 / 硬编码 finding）。
18. 问题单「请求Body」必须含公共参数 `encode`/`appid`/`action`/`sign`/`ts`。

## 冒烟模式（批量探活，与七步流程并列）

> 用户要「把一批 / 全部接口跑一遍，看哪些通」时**走冒烟模式**，不套七步深度流程。分工、案例字段、判定规则、清理授权见 `references/smoke_testing.md`。

**执行三步**：① 备齐三份资产——`--config` 环境配置（地址模板 / 公共参数 / 账号 / QPS 档位 / 判定关键词）、`--cases` 案例清单（按**依赖顺序**排列）、`--hooks` 业务适配层（可选，只放无法用数据表达的动作）；② 先空跑再实跑——`--dry-run` 确认参数渲染无异常 → `--only <单个action>` 试连通性 → 再全量；③ `python scripts/smoke_runner.py --config <cfg> --cases <cases> [--hooks <hooks>] --out-dir reports`。问卷星 48 接口可直接复用 `assets/` 下参考实例。

**判定与产出**：PASS / FAIL / N/A（环境限制）/ ERROR（网络异常）四级，**服务端 5xx 一律判 FAIL**，不会被 N/A 关键词吞掉；HTTP 200 里的服务端内部错误（内部错误 / 系统错误 / traceback 等）优先于 `na_keywords` 判定。FAIL 与 ERROR 进问题台账（统一 16 列，走统一归因——环境/限流归"环境"类、其余记"缺陷"，并自动写**脱敏**证据包），N/A 单独列清单不计缺陷。结果 CSV 记录**真实完整请求报文**（含公共参数）与响应报文各一列；请求/响应体默认脱敏（`sign` 打码），外发报告**禁止** `--no-redact`。产物：结果 CSV + 问题 CSV + report JSON + summary MD。`--feedback` 可把本轮「缺陷」回流为模式候选（进 `references/pattern_candidates.md` 待评审池）。

**衔接**：冒烟圈出的 FAIL 项转七步深度流程定位根因；冒烟不做深度断言，深度流程不做批量探活，两者不互相替代。

**清理纪律**：**适配层的收尾清理默认不执行删除**——`assets/smoke_hooks_wjx.py:95` 判 `teardown_enabled`，默认 `false` 时只列出待清理清单、不删数据；配置 `true` 后才真正删，且只清理本次运行登记的资源，开启前必须确认目标为测试环境。**注意这只约束"收尾清理"**：案例清单里显式的删除类案例仍会照常执行，删除前必须先取得授权（见「执行前的确认检查点」）。

## 资源与能力索引

**方法论与规范**（`references/`）：`framework.md`（31 维度：A–L + M–Z + AA–AE，+ §9 断言/验证矩阵 + §11 数据管理）· `applicability_matrix.md` · `defect_patterns.md` · `doc_review.md` · `smoke_testing.md` · `ui_baseline.md` · `env_config.md` · `issue_format.md` · `case_schema.md` · `auto_generation.md` · `personal_config.md` · `intelligence_policy.md` · `attribution_rules.md` · `adapter_contract.md` · `real_smoke_delivery.md` · `pattern_candidates.md` · `case-coverage.md`（用例执行率对账：rate 严格 / looseTotal 宽松双口径） · `traps.md`

**执行资产**（`scripts/`、`adapters/`）：入口 `flow.py` · `api_test_runner.py`（运行器模板，渲染 `FINDING_CSV_COLS` 台账行）· `smoke_runner.py` · `case_runner.py` · `kb_load_contract.py` · `personal_config.py` · `discovery_profile.py`（发现档：集中管理探测广度限额，一处开关）；生成链 `contract_parser.py`（ShowDoc/OpenAPI 3.x/Swagger 2.0，JSON+YAML，$ref 与嵌套递归展开）· `contract_model.py` · `field_semantics.py` · `applicability.py` · `pattern_engine.py` · `case_generator.py` · `business_scenarios.py`（AF 业务场景补充生成 + 六项门禁 + prompt 自动注入 L5 产品知识）· `companion_scan.py`（配套接口解析：越权读面 / 真往返 / 列表与计数交叉验证的入口；同级扫描落空时从 L5 知识库捞候选，只进清单不自动填）· `dim_generators.py`（零产出维度补齐：编码国际化 / 组合交叉 / 请求追踪 / 批量 / 导出下载 / 按字段特征触发的业务域专项）· `llm_semantics.py`（语义层接入：大模型读契约产出事实、机械校验后并入，可选且不传时零影响）· `llm_fact_cases.py`（语义层兑付：把并入的事实转成用例，没并入就一条不产）；闭环 `assertion_engine.py` · `attribution_engine.py` · `artifact_readback.py`（产物回读校验：写盘后读回验编码/表头/列宽/编号/行数/证据链列）· `evidence_bundle.py` · `minimizer.py` · `regression.py` · `asset_registry.py` · `relationship_inference.py` · `risk_engine.py` · `plan_builder.py` · `report_enhance.py` · `pattern_feedback.py` · `bug_pattern_pool.py`（缺陷模式池：`--merge` 把历轮 `report_full.json` 的缺陷按指纹累积、跨轮计 hits，`--rank` 出风险 Top、`--list` 汇总；**只累积与排序，不自动生成用例**——从一个历史缺陷推断不出修复后的正确行为，自动生成会产生错误断言或与既有生成器重复）· `ui_baseline.py`；适配器 `adapters/base.py` · `adapters/rest.py` · `adapters/sojump.py`（公共参数、鉴权、写操作门禁）· `adapters/graphql.py`（HTTP 200+errors 判失败，反假通过）· `adapters/grpc.py`（懒加载 grpcio，缺依赖实例化即报错）；结构约束 `schemas/contract_model.schema.json` · `schemas/test_plan.schema.json` · `schemas/execution_result.schema.json` · `schemas/finding.schema.json` · `schemas/regression_baseline.schema.json`

**配置与实例资产**（`assets/`）：`finding_columns.json`（**台账列口径唯一来源**，深度流程与冒烟共用；改列名/增删列改这里，渲染层 `finding_row` 按它逐列取值，缺登记的表头在加载期就点名，不静默渲染成空值）· 占位模板 `personal_config.example.json` · `intelligent_config.example.json`（`flow.py --config`：`environment` 写门禁 / `execution` 档位与限流 / `inputs` 契约与基线）· `ui_baseline.example.json`（`template: true`，加载器主动跳过，防示范值造假缺陷）· `smoke_config.example.json` · `security_payloads.json`（G 维度安全载荷清单，case_generator 按语义标签选用）· `bug_patterns.json`（缺陷模式池数据文件，`bug_pattern_pool.py --merge` 累积，冷启动为空、随轮次增长）；问卷星参考实例 `smoke_config_wjx.json` · `smoke_cases_wjx.json` · `smoke_hooks_wjx.py`；说明与模板 `配置参数说明.md`（配置项 ↔ 接口参数 ↔ 中文含义）· `issue_template.csv`

> **真实凭据存放纪律**：个人真实配置统一放**包外** `~/.workbuddy/wjx-api-test/personal_config.json`（或 `WJX_CONFIG` 指定路径）；包内只保留 `*.example.json` 占位模板。真实环境实例（含页面基线）同样放包外并用参数显式指定。
