# 个人参数配置与多环境

本 skill **不写死任何账号 / 子账号 / appid / 域名 / corpid**。这些"个人参数"全部外置，
团队分享 skill 文件夹时不会泄露你的环境信息。包内只保留 `*.example.json` 占位模板。

## 1. 两种配置方式（运行时合并，环境变量优先于配置文件）

1. **配置文件（推荐，便于团队分发）**
   - 复制 `assets/personal_config.example.json` 为 `personal_config.json`，填入你的真实值；
   - 放到 **skill 包外** 的 `~/.workbuddy/wjx-api-test/personal_config.json`，
     或用环境变量 `WJX_CONFIG=/path/to/your/personal_config.json` 指定路径；
   - 该真实文件**不要入库 / 不要随 skill 分享**。
2. **环境变量（轻量，适合 CI / 临时切换）**

   | 变量 | 含义 |
   |---|---|
   | `WJX_BASE_URL` | 测试地址（域名+路径），取自接口文档『接口地址』 |
   | `WJX_APPID` | 当前环境统一 appid |
   | `WJX_MAIN` / `WJX_SUB` | 主账号 / 子账号 |
   | `WJX_CORPID` | 企业 corpid |
   | `WJX_CROSS_APPID` | 跨企业异常场景用 appid（仅在显式开启跨环境时使用） |
   | `WJX_SIGN` | 签名值（约定不校验时仍建议配置，便于落台账） |
   | `WJX_SYSID` / `WJX_ENV` / `WJX_REGION` / `WJX_CONFIG` / `WJX_CROSS_ENV` / `WJX_CROSS_CONFIG` / `WJX_CROSS_REGION` | 见下方各节 |

**加载规则**（集中在 `scripts/personal_config.py` 的 `load_personal_config()`，深度测试与冒烟测试
共用同一份逻辑，保证「个人参数单一来源」）：环境变量 > `personal_config.json` > 报错。
任何必需参数（地址 / appid / 主账号）缺失时，运行器在启动即明确报错并给出配置指引，
**不会带任何默认个人值跑测试**。

## 2. 多套环境（region 分组：国内版 / 国际版，多轮隔离测试）

`personal_config.json` 支持「多套环境结构」——顶层放：

- `envs` 数组：每组一个隔离环境，可带 `region` 字段（`cn`=国内版 / `intl`=国际版）；
- `default_env`（可选）：默认选中的环境名；
- 顶层（`envs` 之外）字段：视为所有环境共享的公共字段，仅在某个环境块未定义时回退。

envs 块数量不限（如国内版 2 套 + 国际版 2 套），模板不必一次写全、按需增删。
模板 `assets/personal_config.example.json` 已内置 cn 2 套 + intl 1 套示例占位
（`round1_local` / `round2_devtest` / `intl_1`）。

**运行时选择分两步**：

1. **先按 region 过滤**——命令行 `--region` 或 `WJX_REGION`（如 `cn` / `intl`）；
   跨 region 的参数不会进入本次运行；
2. **再按以下优先级选定环境**：命令行 `--env <环境name>`（冒烟）> 环境变量 `WJX_ENV` >
   顶层 `default_env` > 候选中第一个。

**兼容性**：旧的单环境扁平结构（无 `envs` 键）仍完全兼容。
真实凭据一律放 skill 包外，包内不得存在任何含真实值的配置文件。
字段级含义见 `assets/配置参数说明.md` 第五、六节。

## 3. 跨环境纪律（防参数串环境）

- 正常用例**只使用所选环境的参数**，未选中的环境参数完全不进入运行时；
- 仅**异常场景测试**（跨企业 / 跨租户 / 越权隔离验证等）允许显式跨环境：
  - 深度测试：设 `WJX_CROSS_ENV=<环境name>`（加载到 `CROSS_CFG`），可另配
    `WJX_CROSS_CONFIG`（指定其他配置文件）与 `WJX_CROSS_REGION`（region 过滤）；
  - 冒烟：用 `--cross-env <环境name>`（挂到 `ctx.cross_env`），也可设 `WJX_CROSS_ENV`；
- 显式加载的跨环境参数带 `_cross_env` 标记，便于在产物中审计；
- **未显式开启时，跨环境参数一律不可用。**

## 4. 冒烟配置同样支持多套环境（region 分组）

`scripts/smoke_runner.py` 加载 `smoke_config_*.json` 后：

1. 先自动把占位符替换为 `personal_config.json` / `WJX_*` 的真实值（`<域名>` 由 `base_url` 解析）；
2. 再按 **`--region` / `WJX_REGION` 过滤 + `--env` / `WJX_ENV` / 顶层 `default_env`
   选定环境并扁平化（`select_smoke_env()`）**。

冒烟配置与个人配置一样支持两种结构：

- **多套环境结构**：顶层 `envs` 数组，每组含各自的 `name` / `region` / `base_url` /
  `common_params` / `account` / `overrides`；顶层放 `method` / `timeout` / `qps_tiers` /
  判定关键词等共享字段。`smoke_config.example.json` 已内置 cn 2 套 + intl 1 套示例占位
  （真实环境值由包外 `personal_config.json` 注入）。
- **单环境扁平结构**（无 `envs` 键，旧配置完全兼容）。

**占位符两种写法**（都解析为同一字段，旧配置不失效）：

1. 简短形式：`<APPID>` / `<主账号>` / `<子账号>` / `<CORPID>` / `<SYSID>` / `<域名>`；
2. 自带中文接口参数含义的长串形式，如
   `<appid 应用ID：所有请求的公共参数 appid，唯一标识你的企业/应用>`（见 `smoke_config.example.json`）。

也支持 `--personal /path/to/personal_config.json` 显式指定。
冒烟配置里**不要写死**个人数据，只放占位符。

> **实例 ID 按环境独立**：各环境 overrides 里的 vid 类业务实例 ID 不通用，某环境生成的实例
> 在其他环境不存在，须按各环境分别重新生成（未生成前相关用例会被 na_keywords 判为 N/A）。

## 5. 分享给团队

直接把整个 skill 文件夹发给同事即可——里面只有占位符模板
（`personal_config.example.json` + `smoke_config.example.json`），同事按上面的方式填自己的
`personal_config.json` 或环境变量就能用。

> 每个配置项对应哪个接口参数、中文含义、取值示例：见 `assets/配置参数说明.md`
> （配置项 ↔ Sojump 接口参数字段 ↔ 中文名 一一对照）。
