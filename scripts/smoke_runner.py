#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
smoke_runner.py — 通用接口冒烟引擎（数据驱动，零业务知识）

定位：广度优先。把一批接口按依赖顺序全跑一遍，只回答「每个接口基本调用通不通」，
不做深度维度断言（那是深度测试模式的职责）。

本引擎不含任何具体接口的知识：不硬编码 action、字段名、域名。
一切业务相关内容通过三份外部资产注入：
  --config <json>  环境配置：地址模板 / 公共参数 / 账号 / QPS 档位 / 判定关键词 / 覆盖参数
  --cases  <json>  案例清单：action / 名称 / 模块 / 端点 / qps / na_hints / 清理登记
  --hooks  <py>    业务适配层（可选）：bootstrap / build_params / post_process / teardown

判定四级：
  PASS   业务成功（result=true）
  FAIL   业务失败且不可归因于环境限制 —— 记入问题台账
  N/A    环境限制 / 未开通 / 已废弃 / 前置不满足 —— 不记缺陷
  ERROR  网络或环境异常（连接失败、超时、被限流）—— 不记缺陷，重试后仍失败才落 ERROR

产物（写入 --out-dir，默认 ./reports）：
  smoke_results_<ts>.csv   全部结果（11 列）
  smoke_issues_<ts>.csv    问题台账（基础 9 列 + 证据链 7 列，共 16 列，口径同 api_test_runner.FINDING_CSV_COLS）
  smoke_report_<ts>.json   完整结构化结果
  smoke_summary_<ts>.md    人类可读汇总

用法：
  python smoke_runner.py --config config.json --cases cases.json --hooks hooks.py
  python smoke_runner.py --config config.json --cases cases.json --dry-run
  python smoke_runner.py --config config.json --cases cases.json --only 1000001,1000002
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import random
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

PLACEHOLDER_RE = re.compile(r"\$\{([a-zA-Z_][\w.:-]*)\}")

# 个人参数配置加载（与 api_test_runner 共用，单一来源）。
# 冒烟配置（smoke_config_*.json）里的占位符（<appid 应用ID：...> 等自带中文含义形式；也兼容 <APPID> 等简短形式）
# 占位符统一从 personal_config.json / WJX_* 环境变量解析，避免冒烟模块重复写死个人数据。
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from personal_config import load_personal_config, resolve_placeholders, config_consistency_check
except Exception:  # 独立运行兜底：无个人配置时保留冒烟配置里的占位符原样
    def load_personal_config(path=None):
        return {}
    def resolve_placeholders(cfg, personal):
        return cfg

# 智能闭环模块：问题台账统一走归因 + 脱敏证据（与深度测试同一套规则）
import attribution_engine as _attr
import evidence_bundle as _evid

# 台账列口径的单一来源在 finding_columns.py（api_test_runner 也从它取同一份），
# 避免"同一份台账两套表头"。该模块是纯标准库、不加载个人配置，
# 故即便 api_test_runner 不可用也能取到同一列口径，不再另抄一份本地字面量。
from finding_columns import load_finding_cols as _load_finding_cols
SMOKE_FINDING_COLS, _cols_warn = _load_finding_cols()
if _cols_warn:
    print("⚠️ 台账列口径：%s" % _cols_warn)

# 公共参数拼装规则的单一出处（与深度流程 api_test_runner 共用同一份）。
import common_params

# 产物脱敏开关：默认开启。结果 CSV / 问题台账 / report JSON 都要外发给开发，
# 明文 sign / token / cookie / password 等于直接泄露凭据，故默认打码
# （只打码敏感值，保留字段名与结构，开发仍能原样复现）。
# 仅内部调试可用 --no-redact 关闭；外发报告不得关闭。
_REDACT = True


# 服务端内部错误关键词：HTTP 200 + 这些文案同样是真缺陷，优先级高于 na_keywords。
# 可在冒烟配置里用 server_error_keywords 覆盖。
DEFAULT_SERVER_ERROR_KEYWORDS = (
    "内部错误", "系统错误", "系统异常", "服务异常", "服务器异常", "未知异常",
    "internal error", "server error", "unhandled exception", "traceback",
)


def set_redact(enabled: bool) -> None:
    global _REDACT
    _REDACT = bool(enabled)
    if not _REDACT:
        print("⚠️ 已关闭产物脱敏（--no-redact）：产物含明文凭据，禁止外发。")


def mask(text):
    """对落盘文本脱敏：保留键名与结构，仅把敏感值替换为 ***。"""
    if not _REDACT or not text:
        return text
    try:
        return _evid.redact(text)
    except Exception:
        return text


# ---------------------------------------------------------------- 上下文

class Ctx:
    """冒烟上下文：承载跨接口传递的数据（vid / jid / uid / corpid 等）。

    业务字段由 hooks 自由读写，引擎只要求能属性/键值双访问。
    """

    def __init__(self, run_id: str, overrides: dict | None = None):
        self.run_id = run_id
        self.overrides: dict = dict(overrides or {})
        self.cleanup: list[dict] = []
        # 本次运行新建的资源 ID，由业务适配层登记，teardown 决定是否清理
        self.created_vids: list = []
        self._d: dict = {}

    def get(self, key, default=None):
        if key in self._d:
            return self._d[key]
        # 点号路径遍历：${ctx.account.main} -> _d["account"]["main"]（字面整键优先）
        if "." in key:
            cur = self._d
            for part in key.split("."):
                if isinstance(cur, dict) and part in cur:
                    cur = cur[part]
                else:
                    return default
            return cur
        return default

    def set(self, key, value):
        self._d[key] = value

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._d.get(name)

    def __setattr__(self, name, value):
        if name in ("run_id", "overrides", "cleanup", "created_vids", "_d"):
            object.__setattr__(self, name, value)
        else:
            self._d[name] = value

    def as_dict(self) -> dict:
        return dict(self._d)


def render(value, ctx: Ctx, cfg: dict):
    """递归渲染参数模板里的 ${...} 占位符。

    支持：${ctx.键} 上下文值、${env.键} 覆盖参数、${run_id}、${ts}、
          ${uuid8}/${uuid10}、${rand:min-max} 随机整数。
    """
    if isinstance(value, str):
        def sub(m):
            token = m.group(1)
            if token.startswith("ctx."):
                return _s(ctx.get(token[4:]))
            if token.startswith("env."):
                return _s(ctx.overrides.get(token[4:]))
            if token == "run_id":
                return ctx.run_id
            if token == "ts":
                return str(int(time.time()))
            if token == "uuid8":
                return uuid.uuid4().hex[:8]
            if token == "uuid10":
                return uuid.uuid4().hex[:10]
            if token.startswith("rand:"):
                lo, _, hi = token[5:].partition("-")
                try:
                    return str(random.randint(int(lo), int(hi)))
                except ValueError:
                    return m.group(0)
            return m.group(0)
        return PLACEHOLDER_RE.sub(sub, value)
    if isinstance(value, dict):
        return {k: render(v, ctx, cfg) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, ctx, cfg) for v in value]
    return value


def _s(v) -> str:
    return "" if v is None else str(v)


def get_path(data, path: str):
    """按点号路径取值，支持列表下标（如 data.items.0.id）。取不到返回 None。"""
    cur = data
    for part in str(path).split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def eval_skip(expr: str | None, ctx: Ctx) -> bool:
    """评估 skip_if 表达式，为真则跳过该用例（判 N/A）。

    仅支持三种简单形式，够用且不给案例文件引入脚本能力：
      ctx.键          —— 键为真值则跳过
      !ctx.键         —— 键为假值则跳过
      ctx.键 == 值    —— 相等则跳过
    """
    if not expr:
        return False
    expr = expr.strip()
    neg = False
    if expr.startswith("!"):
        neg = True
        expr = expr[1:].strip()
    def _val(token: str):
        if token.startswith("ctx."):
            return ctx.get(token[4:])
        if token.startswith("env."):
            return ctx.overrides.get(token[4:])
        return None

    if "==" in expr:
        left, _, right = expr.partition("==")
        left, right = left.strip(), right.strip().strip("'\"")
        hit = _s(_val(left)) == right
    else:
        hit = bool(_val(expr))
    return (not hit) if neg else hit


def apply_extracts(extracts: dict | None, ctx: Ctx, parsed) -> None:
    """把响应里的值提取进上下文，供后续依赖接口使用。"""
    if not extracts or not isinstance(parsed, dict):
        return
    for out_key, path in extracts.items():
        val = get_path(parsed, path)
        if val is not None:
            prev = ctx.get(out_key)
            ctx.set(out_key, val if not isinstance(prev, int) or not isinstance(val, str) else type(prev)(val))


def run_pre(step: dict, cfg: dict, ctx: Ctx, call, dry: bool) -> list[dict]:
    """执行前置步骤：为依赖接口准备上下文（如先创建部门再修改部门）。

    retry_until 用于「创建后需等待才能查到」的最终一致性场景：
    轮询直到指定路径有值，或达到 max 次为止。

    【审查修复·非静默】此前本函数对「连接失败 / 响应非 dict」**完全静默**：
    parsed 不是 dict 时既不进 isinstance 分支、也不落任何记录，循环空转结束
    后直接返回——调用方（run 主循环）看不出前置到底跑没跑通，只能看到后续用例
    因缺配置被跳过，排查时第一现场（前置为什么没成功）已经丢了。
    改为返回结构化留痕记录 [{step, reason, attempts, http, resp}]，
    由调用方并入结果行；同时对每次异常响应写 stderr，不让「前置没跑通」无声消失。

    返回值语义：空列表 = 前置成功（或 dry-run 未执行）；非空 = 未能成功，需留痕。
    """
    if dry:
        return []
    action = str(step.get("action"))
    params = render(step.get("params") or {}, ctx, cfg)
    retry = step.get("retry_until") or {}
    tries = max(1, int(retry.get("max") or 1))
    for i in range(tries):
        http, parsed, _raw = call(action, params, step.get("endpoint"))
        if isinstance(parsed, dict):
            if retry and get_path(parsed, retry.get("path") or "") is not None:
                apply_extracts(step.get("extracts"), ctx, parsed)
                return []
            if not retry and parsed.get("result"):
                apply_extracts(step.get("extracts"), ctx, parsed)
                return []
            # 【W40】失败响应不得 apply_extracts：失败体里的空值/错误结构写入 ctx
            # 会污染所有下游依赖用例（拿到 "id": null 之类假值继续跑，错误面目全非）。
            # 【二轮审查修复】retry 场景下「path 未就绪」不是终局——继续轮询剩余次数
            # （retry_until 的唯一用途就是等最终一致性：创建后需等待才能查到）。
            # 立即 return 会让 retry_until 整类失效、前置误报失败。仅在不重试或
            # 已到最后一次时才按失败收场。
            is_last = i >= tries - 1
            if not retry or is_last:
                # 两种收场原因不同，留痕要能分辨：非 retry 场景是业务失败；
                # retry 场景是轮询用尽仍未就绪（最终一致性没等到）。
                reason = ("retry_until 轮询 %d 次仍未就绪（path=%s）"
                          % (tries, retry.get("path") or "")
                          if retry else "前置业务失败（result=false）")
                print("[pre] %s 前置失败（%s），"
                      "跳过 extracts，依赖本步骤的用例将按缺配置跳过" % (action, reason))
                return [{"step": step, "reason": reason,
                         "attempts": i + 1, "http": http,
                         "resp": _s(parsed.get("errormsg") or parsed.get("errmsg") or "")[:200]}]
        else:
            # 连接失败 / 网关返回非 JSON：此前此处直接静默空转，前置为何没成功无处可查。
            # 留痕 + stderr，且**不 apply_extracts**（同上，失败体不可信）。
            is_last = i >= tries - 1
            if not retry or is_last:
                sys.stderr.write(
                    "[pre] 前置 %s 响应非 dict（连接失败或响应非 JSON，HTTP=%s），"
                    "前置未成功：依赖本步骤的用例将按缺配置跳过\n" % (action, http))
                return [{"step": step, "reason": "前置连接失败或响应非 JSON",
                         "attempts": i + 1, "http": http, "resp": _s(_raw)[:200]}]
        if i < tries - 1:
            time.sleep(float(retry.get("sleep") or 0.5))
    # 走到这里逻辑上不可达（最后一次迭代的两个分支都 return），保留 return 只是
    # 兜住未来改动；真要在此留痕请先确认不会与上面两条重复记账。



# ---------------------------------------------------------------- HTTP

def build_ssl(verify: bool) -> ssl.SSLContext:
    # 与深度流程 api_test_runner 统一口径：环境变量 WJX_VERIFY_TLS=1 强制启用证书校验，
    # 覆盖配置里的 tls_verify（两层开关，env 优先，避免同一件事两套配法各配各的）。
    verify = bool(verify) or os.environ.get("WJX_VERIFY_TLS") == "1"
    c = ssl.create_default_context()
    if not verify:
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE
    return c


def http_post(url: str, payload: dict, timeout: int, ctx_ssl: ssl.SSLContext):
    """发送 JSON POST，返回 (http_code, parsed, raw_text)。

    网络层异常以 http_code=0 表示，由上层归类为 ERROR。
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx_ssl) as r:
            raw = r.read().decode("utf-8", "replace")
            code = r.getcode()
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        code = e.code
    except Exception as e:
        return 0, None, f"{type(e).__name__}: {e}"
    try:
        parsed = json.loads(raw)
    except Exception:
        return code, None, raw
    return code, parsed, raw


# ---------------------------------------------------------------- 判定

def classify(http_code: int, parsed, raw: str, case: dict, cfg: dict) -> tuple[str, str]:
    """四级判定，返回 (判定, 错误信息)。

    顺序：网络异常(ERROR) → 业务成功(PASS) → N/A 关键词(N/A) → 其余(FAIL)。
    N/A 判定的意义：环境限制不等于缺陷，避免污染问题台账。
    说明：冒烟只做请求-响应型基本可用性；流式/长连接接口应在专项或深度测试中单列。"""
    if http_code == 0 or not isinstance(parsed, dict):
        return "ERROR", (raw or "")[:300]

    result = parsed.get("result")
    errmsg = _s(parsed.get("errmsg") or parsed.get("message") or parsed.get("msg") or "")

    if result is True:
        return "PASS", ""

    # 服务端 5xx 是真缺陷，不得被 na_keywords 吞成 N/A（如"内部错误"）
    if http_code >= 500:
        return "FAIL", errmsg or f"HTTP {http_code} 服务端错误"

    # HTTP 200 但文案是服务端内部错误：同样是真缺陷。
    # 只守 5xx 是不够的——单 POST JSON 网关常把内部异常包在 200 里，一旦 "内部错误"
    # 落在 na_keywords 中就会被静默判 N/A，真缺陷被持续吞掉。
    server_error_words = cfg.get("server_error_keywords") or DEFAULT_SERVER_ERROR_KEYWORDS
    for w in server_error_words:
        if w and str(w).lower() in errmsg.lower():
            return "FAIL", errmsg or w

    na_words = list(cfg.get("na_keywords") or []) + list(case.get("na_hints") or [])
    for w in na_words:
        if w and w in errmsg:
            return "N/A", errmsg

    env_words = cfg.get("env_keywords") or []
    for w in env_words:
        if w and w in (raw or ""):
            return "ERROR", errmsg or w

    return "FAIL", errmsg or f"result={result}"


# ---------------------------------------------------------------- 加载

def load_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def select_smoke_env(cfg: dict, env: str | None = None, region: str | None = None) -> dict:
    """多环境冒烟配置：若 cfg 含 envs 数组（每组一个隔离环境，可带 region 分组如 cn/intl），
    先按 region 过滤（--region > 环境变量 WJX_REGION，跨 region 参数不会进入本次运行），
    再按 --env > 环境变量 WJX_ENV > 顶层 default_env > 候选中第一个 选择其一，
    以顶层共享字段为底、所选环境块覆盖，返回扁平化后的新配置 dict（不修改入参）。
    环境块内用 name 作选择键；顶层（envs/default_env 之外）字段视为各环境共享。
    无 envs 键的旧单环境配置原样返回，完全兼容。
    """
    envs = cfg.get("envs")
    if not isinstance(envs, list) or not envs:
        return cfg
    region = region or os.environ.get("WJX_REGION")
    if region:
        matched = [e for e in envs if isinstance(e, dict) and e.get("region") == region]
        if matched:
            envs = matched
        else:
            known = sorted({str(e.get("region")) for e in envs if isinstance(e, dict) and e.get("region")})
            print("警告：配置里没有 region=%s 的环境（现有 region：%s），改为在全部环境中选择。" % (region, ", ".join(known) or "未标注"))
    sel = env or os.environ.get("WJX_ENV") or cfg.get("default_env")
    chosen = None
    if sel is not None:
        for _e in envs:
            if isinstance(_e, dict) and _e.get("name") == sel:
                chosen = _e
                break
        if chosen is None:
            try:
                chosen = envs[int(sel)]
            except (ValueError, IndexError, TypeError):
                pass
    if chosen is None:
        chosen = envs[0]
    flat = {k: v for k, v in cfg.items() if k not in ("envs", "default_env")}
    if isinstance(chosen, dict):
        flat.update(chosen)
    return flat


def load_hooks(path: str | None):
    """动态加载业务适配层模块。未指定则返回一个空壳。"""
    class _Empty:
        @staticmethod
        def bootstrap(cfg, ctx, call): pass
        @staticmethod
        def build_params(action, cfg, ctx, call): return None
        @staticmethod
        def post_process(action, cfg, ctx, parsed): pass
        @staticmethod
        def teardown(cfg, ctx, call): pass

    if not path:
        return _Empty()
    spec = importlib.util.spec_from_file_location("smoke_hooks", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["smoke_hooks"] = mod
    spec.loader.exec_module(mod)
    if not all(hasattr(mod, n) for n in ("bootstrap", "build_params", "post_process", "teardown")):
        raise SystemExit(f"适配层缺少必需函数（bootstrap/build_params/post_process/teardown）：{path}")
    return mod


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser(description="通用接口冒烟引擎")
    ap.add_argument("--config", required=True, help="环境配置 JSON")
    ap.add_argument("--cases", required=True, help="案例清单 JSON")
    ap.add_argument("--hooks", help="业务适配层 PY（可选）")
    ap.add_argument("--personal", help="个人参数配置 JSON（默认 WJX_CONFIG 或 ~/.workbuddy/wjx-api-test/personal_config.json）；用于解析冒烟配置里的占位符（<appid 应用ID：...> 等自带中文含义形式，或 <APPID> 等简短形式）")
    ap.add_argument("--env", help="多环境配置里要使用的环境 name（如 round1_local / round2_devtest）；也可改用环境变量 WJX_ENV。不指定时取 default_env 或候选中第一个")
    ap.add_argument("--region", help="按 region 过滤环境（如 cn=国内版 / intl=国际版），先筛 region 再选环境；也可改用环境变量 WJX_REGION")
    ap.add_argument("--cross-env", help="【异常场景专用】额外加载另一环境（按 name）的参数挂到 ctx.cross_env，仅供跨企业/跨租户等异常用例使用；正常用例仍只用所选环境参数，不设置则完全不加载其他环境")
    ap.add_argument("--out-dir", default="reports", help="产物目录")
    ap.add_argument("--only", help="只跑指定 action，逗号分隔")
    ap.add_argument("--dry-run", action="store_true", help="只渲染参数不发起调用")
    ap.add_argument("--no-redact", action="store_true",
                    help="关闭产物脱敏（仅内部调试；产物将含明文 sign/token，禁止外发）")
    ap.add_argument("--timeout", type=int, help="覆盖配置里的超时秒数")
    ap.add_argument("--feedback", action="store_true",
                    help="运行结束后把本轮「缺陷」类问题回流为缺陷模式候选（pattern_feedback → references/pattern_candidates.md 待评审池）")
    args = ap.parse_args()

    set_redact(not (args.no_redact or os.environ.get("WJX_NO_REDACT") == "1"))

    cfg = load_json(args.config)
    # 解析个人参数占位符：冒烟配置里的 <appid 应用ID：...> 等自带中文含义形式（或 <APPID> 等简短形式）
    # 统一从 personal_config.json / WJX_* 环境变量取值，避免冒烟模块重复写死个人数据。
    personal = load_personal_config(args.personal, env=args.env, region=args.region)
    cfg = resolve_placeholders(cfg, personal)
    # 多环境冒烟配置：先按 region（--region/WJX_REGION）过滤，再按 --env/WJX_ENV/default_env 选定并扁平化。
    # 保留全量配置副本供 --cross-env 显式加载其他环境（仅异常场景用例可取用）。
    cfg_all = cfg
    cfg = select_smoke_env(cfg, args.env, region=args.region)
    cross_env_requested = args.cross_env or os.environ.get("WJX_CROSS_ENV")
    cases_doc = load_json(args.cases)
    hooks = load_hooks(args.hooks)

    cases = cases_doc.get("cases") or []
    if args.only:
        wanted = {a.strip() for a in args.only.split(",") if a.strip()}
        cases = [c for c in cases if str(c.get("action")) in wanted]

    timeout = args.timeout or int(cfg.get("timeout") or 60)
    ctx_ssl = build_ssl(bool(cfg.get("tls_verify")))
    base_tpl = cfg.get("base_url") or ""
    common = cfg.get("common_params") or {}
    common_gen = cfg.get("common_param_generators") or {}
    qps_tiers = {str(k): float(v) for k, v in (cfg.get("qps_tiers") or {}).items()}

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    ctx = Ctx(run_id=run_id, overrides=cfg.get("overrides") or {})
    ctx.account = cfg.get("account") or {}
    # 跨环境参数：仅当显式 --cross-env / WJX_CROSS_ENV 指定时加载，挂到 ctx.cross_env
    # 供异常场景用例（跨企业/跨租户/越权隔离等）取用；正常用例不得读取。
    ctx.cross_env = None
    if cross_env_requested:
        cross_cfg = select_smoke_env(cfg_all, cross_env_requested, region=args.region)
        cross_cfg["_cross_env"] = cross_env_requested
        ctx.cross_env = cross_cfg
        print(f"  [cross-env] 异常场景跨环境参数已显式加载：{cross_env_requested}（base_url={cross_cfg.get('base_url','')}）")

    # ===== 防呆门禁：配置矛盾拦截（启动阶段，不让错误参数静默跑完全程） =====
    _consistency = config_consistency_check(
        personal,
        cross_env_requested=bool(cross_env_requested),
        cross_cfg=ctx.cross_env,
    )
    _fatal = [m for _lvl, m in _consistency if _lvl == "fatal"]
    _warn = [m for _lvl, m in _consistency if _lvl == "warn"]
    if _fatal:
        for m in _fatal:
            sys.stderr.write("错误（配置矛盾，禁止运行）：%s\n" % m)
        sys.exit(2)
    if _warn:
        print("⚠️ 配置一致性警告：")
        for m in _warn:
            print("   - %s" % m)
        if os.environ.get("WJX_STRICT_CONSISTENCY") == "1":
            sys.exit(2)

    def build_payload(action: str, extra: dict | None = None) -> dict:
        """按统一规则构造**完整**请求报文：公共参数 + 动态生成参数 + action + 业务参数。

        规则本体在 common_params.build_request_payload（与深度流程同源）。
        抽成独立函数是为了让 dry-run 与真实调用共用同一条构造路径——
        dry-run 曾绕过 call()，导致「请求Body」列空、而「响应Body」列填的却是请求参数。"""
        generated = {}
        for k, v in (common_gen or {}).items():
            if v == "unix_ts":
                generated[k] = common_params.generate_ts()
        # 公共参数之外的其它透传键（含动态生成键）与业务参数合并后交给统一构造器
        passthrough = {k: v for k, v in common.items()
                       if k not in ("encode", "appid", "sign", "ts")}
        passthrough.update(generated)
        passthrough.update(render(extra or {}, ctx, cfg))
        return common_params.build_request_payload(
            action,
            encode=common.get("encode"),
            appid=common.get("appid"),
            sign=common.get("sign"),
            ts_value=generated.get("ts", common.get("ts")),
            action_param=cfg.get("action_param") or "action",
            extra=passthrough,
        )

    def call(action: str, extra: dict | None = None, endpoint: str | None = None):
        """统一调用入口，同时供 hooks 复用（前置调用、清理调用）。

        每次调用把完整请求负载记录到 ctx.last_request，供结果行落「请求Body」列——
        台账必须记录真实请求报文（含公共参数），不得填响应报文。"""
        ep = endpoint or cfg.get("default_endpoint") or ""
        url = base_tpl.replace("{endpoint}", ep)
        payload = build_payload(action, extra)
        ctx.last_request = dict(payload)
        if args.dry_run:
            return 0, None, json.dumps(payload, ensure_ascii=False)
        return http_post(url, payload, timeout, ctx_ssl)

    results: list[dict] = []
    # 【审查修复·非静默】前置未成功的结构化留痕（run_pre 返回），落 JSON 产物。
    pre_not_run: list[dict] = []
    started = time.time()

    print(f"=== 冒烟开始 · {cfg.get('name') or cases_doc.get('environment') or ''} "
          f"· 共 {len(cases)} 个接口 ===")

    if not args.dry_run:
        hooks.bootstrap(cfg, ctx, call)

    for idx, case in enumerate(cases, 1):
        action = str(case.get("action"))
        name = case.get("name") or action
        qps = str(case.get("qps") or "")
        gap = qps_tiers.get(qps)
        if gap:
            time.sleep(gap)

        for step in case.get("pre") or []:
            # 【审查修复·非静默】前置未成功必须留痕：记进本用例的结果行错误信息，
            # 并进结构化 pre_not_run（落 JSON 产物）。此前 run_pre 对连接失败/
            # 非 dict 响应完全静默，前置为何没跑通在报告里查无实据。
            for rec in run_pre(step, cfg, ctx, call, args.dry_run):
                pre_not_run.append({
                    "序号": idx, "action": action, "名称": name,
                    "前置action": str(step.get("action")),
                    "原因": rec.get("reason"), "尝试次数": rec.get("attempts"),
                    "HTTP": rec.get("http"), "响应": rec.get("resp"),
                })
                print(f"[{idx:>2}/{len(cases)}] ! PRE   {action}  {name}  "
                      f"前置未成功：{rec.get('reason')}")

        # generate：为本用例生成一次性标识并存入上下文，供本例及后续依赖接口复用
        # （如子账号名、成员 userid）。已存在则不覆盖，保证重跑幂等。
        for g_key, g_tpl in (case.get("generate") or {}).items():
            if ctx.get(g_key) is None:
                ctx.set(g_key, render(g_tpl, ctx, cfg))

        # skip_if 必须在 pre / generate 之后评估：
        # 有些用例的前置步骤正是为了满足该条件（如先提交答卷拿到 jid 才能改分）
        if eval_skip(case.get("skip_if"), ctx):
            print(f"[{idx:>2}/{len(cases)}] -  N/A  {action}  {name}  (前置条件不满足，跳过)")
            results.append({
                "序号": idx, "action": action, "名称": name,
                "模块": case.get("module") or "", "端点": case.get("endpoint") or "",
                "HTTP": 0, "判定": "N/A", "耗时ms": 0,
                "错误信息": f"skip_if 命中：{case.get('skip_if')}", "请求Body": "", "响应Body": "",
            })
            continue

        params = hooks.build_params(action, cfg, ctx, call)
        # 适配层返回 None 或空字典都表示「对参数没有补充意见」，回落到案例的声明式参数
        if not params:
            params = render(case.get("params") or {}, ctx, cfg)

        t0 = time.time()
        if args.dry_run:
            # 走同一条构造路径，把**完整**请求报文（含公共参数）落进 ctx，
            # 空跑自检才能看出"真正会发什么"；响应体留空并标注未调用，避免与请求错位。
            ctx.last_request = build_payload(action, params)
            code, parsed, raw = 0, None, ""
            verdict, errmsg = "N/A", "dry-run 未发起调用"
        else:
            code, parsed, raw = call(action, params, case.get("endpoint"))
            verdict, errmsg = classify(code, parsed, raw, case, cfg)
            if verdict == "ERROR" and int(cfg.get("error_retry") or 0) > 0:
                for _ in range(int(cfg["error_retry"])):
                    time.sleep(float(cfg.get("error_retry_sleep") or 1.0))
                    code, parsed, raw = call(action, params, case.get("endpoint"))
                    verdict, errmsg = classify(code, parsed, raw, case, cfg)
                    if verdict != "ERROR":
                        break
            hooks.post_process(action, cfg, ctx, parsed)
            apply_extracts(case.get("extracts"), ctx, parsed)

        cost = int((time.time() - t0) * 1000)
        req_body = json.dumps(getattr(ctx, "last_request", None) or {}, ensure_ascii=False)
        row = {
            "序号": idx, "action": action, "名称": name,
            "模块": case.get("module") or "", "端点": case.get("endpoint") or "",
            "HTTP": code, "判定": verdict, "耗时ms": cost,
            "错误信息": errmsg,
            "请求Body": mask(req_body)[:1000],
            "响应Body": mask(_s(raw))[:1000],
        }
        results.append(row)
        flag = {"PASS": "  PASS", "FAIL": "× FAIL", "N/A": "-  N/A", "ERROR": "! ERROR"}[verdict]
        print(f"[{idx:>2}/{len(cases)}] {flag}  {action}  {name}  ({cost}ms) {errmsg[:60]}")

        if case.get("cleanup") and verdict == "PASS":
            ctx.cleanup.append({"action": action, "params": render(case["cleanup"], ctx, cfg)})

    if not args.dry_run:
        hooks.teardown(cfg, ctx, call)

    write_reports(results, cases_doc, cfg, run_id, args.out_dir, time.time() - started,
                  pre_not_run=pre_not_run)

    if args.feedback:
        # 缺陷回流：把台账里的「缺陷」类问题提炼为模式候选（先进待评审池，不直接改正式清单）
        import glob as _glob
        pattern_feedback = __import__("pattern_feedback")
        issues_csvs = sorted(_glob.glob(str(Path(args.out_dir) / f"smoke_issues_{run_id}.csv")))
        for ics in issues_csvs:
            items = pattern_feedback.mine_from_csv(ics)
            if items:
                pattern_feedback.append_candidates(items)
            else:
                print("缺陷回流：本轮无缺陷类问题，无候选登记。")
    return 0


# ---------------------------------------------------------------- 产物

def ui_hints(parsed):
    """接口侧 UI 线索（探活级，不新增请求）：声明字段空值 / URL 字段格式异常。

    错版往往始于接口给了前端"没用的值"：字段存在但为 null/空串 → 空白区域；
    URL 字段不是合法链接 → 裂图。冒烟只从现有响应提取信号，不增加任何请求。"""
    if not isinstance(parsed, dict):
        return ""
    data = parsed.get("data")
    if not isinstance(data, dict) or not data:
        return ""
    urlish = ("url", "link", "img", "image", "pic", "photo", "avatar", "cover",
              "logo", "file", "attach", "download", "qr", "banner")
    empty = [k for k, v in data.items() if v in (None, "", [], {})]
    bad_url = [k for k, v in data.items()
               if any(h in str(k).lower() for h in urlish)
               and isinstance(v, str) and v
               and not v.startswith(("http://", "https://", "/"))]
    parts = []
    if empty:
        parts.append("空值字段:%d(%s)" % (len(empty), ",".join(str(k) for k in empty[:5])))
    if bad_url:
        parts.append("URL格式异常:%s" % ",".join(str(k) for k in bad_url[:5]))
    return "; ".join(parts)


def write_reports(results, cases_doc, cfg, run_id, out_dir, elapsed, pre_not_run=None) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # UI 线索附进"错误信息"列：批量探活也能给出错版前因的第一手信号
    for r in results:
        try:
            hints = ui_hints(json.loads(r.get("响应Body") or "{}"))
        except Exception:
            hints = ""
        if hints:
            prev = str(r.get("错误信息") or "")
            r["错误信息"] = (prev + "；UI线索: " + hints) if prev else ("UI线索: " + hints)

    counts: dict[str, int] = {}
    for r in results:
        counts[r["判定"]] = counts.get(r["判定"], 0) + 1
    total = len(results)

    # 1) 结果 CSV（utf-8-sig，Excel 直接打开不乱码）
    p_res = out / f"smoke_results_{run_id}.csv"
    cols = ["序号", "action", "名称", "模块", "端点", "HTTP", "判定", "耗时ms", "错误信息", "请求Body", "响应Body"]
    with open(p_res, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(results)

    # 2) 问题台账 CSV（列口径与深度流程共用 api_test_runner.FINDING_CSV_COLS，
    #    避免"同一份台账两套表头"——曾经冒烟少写 请求ID/TraceID，两边对不齐)
    p_iss = out / f"smoke_issues_{run_id}.csv"
    icols = list(SMOKE_FINDING_COLS)
    with open(p_iss, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=icols)
        w.writeheader()
        n = 0
        retry_n = int(cfg.get("error_retry") or 0)
        # 【W8 反吞】N/A 判定的用例此前完全不进台账——na_keywords 子串命中就把真失败
        # 无痕吞掉（外观完全合理）。改为每条 N/A 落一条 low 台账（可人工翻案），
        # N/A 语义（不计缺陷、不阻塞）不变，但「被谁吞的」必须留痕。
        for r in [x for x in results if x["判定"] == "N/A"]:
            n += 1
            w.writerow({
                "编号": f"SMK-{n:03d}", "类型": "待确认", "严重程度": "低", "状态": "待确认",
                "维度": "冒烟-环境限制（N/A 留痕）",
                "问题描述": (f"[action {r['action']}] {r['名称']} 判 N/A（命中 na_keywords）"
                             f"：{r['错误信息']}——若实际为业务失败请人工翻案"),
                "请求Body": r["请求Body"], "预期结果": "接口正常返回 result=true",
                "实际结果": f"HTTP {r['HTTP']} · {r['错误信息']}",
                "置信度": 0.5, "HTTP状态码": r["HTTP"],
                "请求ID": "", "TraceID": "", "证据路径": "", "归因": "N/A_KEYWORD",
                "复测状态": "待复测",
            })
        for r in results:
            if r["判定"] not in ("FAIL", "ERROR"):
                continue
            n += 1
            verdict = r["判定"]
            # 归因：先排除环境/限流因素，再判业务缺陷（与 attribution_rules.md 一致）
            att = _attr.attribute(
                error=r["错误信息"] if verdict == "ERROR" else None,
                response=r.get("响应Body") or "",
                assertion_ok=False,
                contract_confidence=1.0,
                retries=retry_n if verdict == "ERROR" else 0,
            )
            cat = att["category"]
            if verdict == "ERROR" or cat in ("ENV", "RATE_LIMIT"):
                itype, severity, status = "环境", "中", "待确认"
                desc = (f"[action {r['action']}] {r['名称']} 调用异常（网络/限流/超时）"
                        if verdict == "ERROR" else
                        f"[action {r['action']}] {r['名称']} 冒烟调用失败（归因:{cat}）：{r['错误信息']}")
                expected = "接口可稳定访问"
            elif cat == "CONSISTENCY_PENDING":
                itype, severity, status = "待确认", "低", "待确认"
                desc = f"[action {r['action']}] {r['名称']} 冒烟调用失败（疑似同步延迟）：{r['错误信息']}"
                expected = "接口正常返回 result=true"
            else:
                itype, severity, status = "缺陷", "高", "待修复"
                desc = f"[action {r['action']}] {r['名称']} 冒烟调用失败：{r['错误信息']}"
                expected = "接口正常返回 result=true"
            # 证据包：脱敏后的请求/响应原文，路径写入台账供复现
            try:
                ev_path = _evid.write_bundle(
                    str(out / "evidence"), run_id, "SMK-%03d_%s" % (n, r["action"]),
                    {"action": r["action"], "name": r["名称"], "verdict": verdict,
                     "http": r["HTTP"], "request": r["请求Body"], "response": r.get("响应Body") or "",
                     "error": r["错误信息"], "attribution": att},
                )
            except Exception:
                ev_path = ""
            w.writerow({
                "编号": f"SMK-{n:03d}", "类型": itype, "严重程度": severity, "状态": status,
                "维度": "冒烟-基本可用性" if verdict == "FAIL" else "冒烟-环境稳定性",
                "问题描述": desc,
                "请求Body": r["请求Body"], "预期结果": expected,
                "实际结果": f"HTTP {r['HTTP']} · {r['错误信息']}",
                "置信度": att.get("confidence"), "HTTP状态码": r["HTTP"],
                # 请求ID/TraceID 冒烟无采集渠道，留空但**列必须在**：
                # 缺列会让冒烟台账与深度台账表头对不齐，消费方按列位取值就整体错位
                "请求ID": "", "TraceID": "",
                "证据路径": ev_path, "归因": f"{cat}:{att.get('reason','')}",
                "复测状态": "待复测",
            })

    # 3) 完整 JSON
    p_json = out / f"smoke_report_{run_id}.json"
    with open(p_json, "w", encoding="utf-8") as f:
        json.dump({
            "run_id": run_id,
            "environment": cfg.get("name") or cases_doc.get("environment"),
            "elapsed_sec": round(elapsed, 1),
            "summary": counts, "total": total,
            "results": results,
            # 【审查修复·非静默】前置未成功的留痕：此前 run_pre 对连接失败/
            # 非 dict 响应完全静默，这里会是空的，报告查不到前置为何没跑通。
            "pre_not_run": list(pre_not_run or []),
        }, f, ensure_ascii=False, indent=2)

    # 4) 汇总 MD
    p_md = out / f"smoke_summary_{run_id}.md"
    pass_n = counts.get("PASS", 0)
    lines = [
        f"# 接口冒烟汇总 · {cfg.get('name') or cases_doc.get('environment') or ''}",
        "",
        f"> 运行时间：{run_id} ｜ 耗时 {elapsed:.1f}s ｜ 接口总数 {total}",
        "",
        "| 判定 | 数量 | 占比 |",
        "| --- | --- | --- |",
    ]
    for k in ("PASS", "FAIL", "N/A", "ERROR"):
        c = counts.get(k, 0)
        pct = f"{c / total * 100:.1f}%" if total else "-"
        lines.append(f"| {k} | {c} | {pct} |")
    lines += [
        "",
        f"**通过率**（PASS / 有效样本（排除 N/A））："
        f"{pass_n}/{max(total - counts.get('N/A', 0), 1)}",
        "",
        # 【W8 配套】保守口径：N/A 也算未通过——na_keywords 子串命中可能吞掉真失败，
        # 保守口径给出「含 N/A 分母」的下界，两条口径并排呈现防误读。
        f"**保守通过率**（PASS / 全部样本（含 N/A））：{pass_n}/{max(total, 1)}",
        "",
        "## 失败与异常明细",
        "",
    ]
    bad = [r for r in results if r["判定"] in ("FAIL", "ERROR")]
    if bad:
        lines += ["| action | 名称 | 判定 | 错误信息 |", "| --- | --- | --- | --- |"]
        for r in bad:
            lines.append(f"| {r['action']} | {r['名称']} | {r['判定']} | {r['错误信息'][:80]} |")
    else:
        lines.append("无。")
    lines += [
        "",
        "## N/A 清单（环境限制，非缺陷）",
        "",
    ]
    nas = [r for r in results if r["判定"] == "N/A"]
    if nas:
        lines += ["| action | 名称 | 说明 |", "| --- | --- | --- |"]
        for r in nas:
            lines.append(f"| {r['action']} | {r['名称']} | {r['错误信息'][:80]} |")
    else:
        lines.append("无。")
    # 【审查修复·非静默】前置未成功清单：这些用例的「跳过」根因在前置，
    # 不点名的话读报告的人只会看到一串没有原因解释的 N/A。
    lines += [
        "",
        "## 前置未成功清单（依赖用例被跳过的根因）",
        "",
    ]
    if pre_not_run:
        lines += ["| 用例 action | 用例名称 | 前置 action | 原因 | 尝试次数 | HTTP |",
                  "| --- | --- | --- | --- | --- | --- |"]
        for r in pre_not_run:
            lines.append("| %s | %s | %s | %s | %s | %s |" % (
                r.get("action"), r.get("名称"), r.get("前置action"),
                str(r.get("原因") or "")[:60], r.get("尝试次数"), r.get("HTTP")))
    else:
        lines.append("无。")
    with open(p_md, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print("\n=== 汇总 ===")
    print("  " + "  ".join(f"{k}={counts.get(k, 0)}" for k in ("PASS", "FAIL", "N/A", "ERROR")))
    if pre_not_run:
        print(f"  前置未成功 {len(pre_not_run)} 处（详见产物 pre_not_run）")
    for p in (p_res, p_iss, p_json, p_md):
        print(f"  产物: {p}")


if __name__ == "__main__":
    sys.exit(main())
