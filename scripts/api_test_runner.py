# -*- coding: utf-8 -*-
"""
通用接口自动化测试运行器（模板 / TEMPLATE）

把任意「单 POST + JSON Body 的网关动作」接口的快速测试跑起来。
本文件是 **通用模板**：下面所有 Sojump / 1000107 / 1000108 等仅作「可运行示例」，
请替换为**你要测的接口**的 action、参数、契约字段与环境配置。框架/维度/断言逻辑完全复用。

用法：
  1. 配置个人参数（appid / 账号 / 域名 / corpid，禁止写死在脚本里）：
     方式A：复制 assets/personal_config.example.json 为 personal_config.json 填入真实值，
            放到 ~/.workbuddy/wjx-api-test/ 或通过环境变量 WJX_CONFIG 指定路径；
     方式B：直接导出环境变量 WJX_BASE_URL / WJX_APPID / WJX_MAIN / WJX_SUB / WJX_CORPID / WJX_CROSS_APPID。
  2. 在 main() 把 EXAMPLE 用例替换为你自己的接口用例。
  3. 运行：python api_test_runner.py
     补跑清理：python api_test_runner.py --cleanup-only   # 仅执行清理清单中上次遗留的待删资源
     切环境：WJX_BASE_URL=https://实际地址/实际路径 python api_test_runner.py  # 地址取自接口文档『接口地址』

设计要点（来自真实接口测试复盘，避免"全绿但没测出问题"）：
  * 异常用例必须命中错误关键词(err_exact)才 PASS，不再只看 result=false；
  * 【参数校验铁律】非法/缺失/类型错/枚举越界输入必须断言"应被拒绝"——
    用 assert_validation()；绝不要把非法输入写成 expect_success=True（会把缺陷掩盖成通过）；
  * 成功用例校验响应 data 契约字段(check_contract)；
  * HTTP 5xx 一律记问题；
  * 连接/网络异常自动重试，重试后仍失败归 ENV/网络问题，不误判为业务缺陷；
  * 问题记录导出 CSV(utf-8-sig，逗号分隔，含逗号/引号/换行的字段用双引号包裹)，Excel/WPS 打开不空白。
  * 核心增强能力：
    - 适用性感知：用例可标注 applicability（适用/N/A），N/A 用例跳过执行但记录在案；
    - 增强断言 helper：check_timezone（时区偏移）、check_idempotent（幂等性）、
      check_field_naming（字段命名规范）、check_batch_partial（批量部分成功）、
      check_download_url（下载链接有效性）、check_enum_exhaustive（枚举完整性）、
      check_pagination（分页边界）、check_uniqueness（业务唯一性）、
      check_ui_parity（前端-接口口径一致）、check_null_triad（可空字段四态）、
      check_http_semantics（HTTP 状态码语义）、check_transport_contract（请求头/传输契约）、
      check_error_root_cause（根因掩盖检测）、check_error_i18n（错误信息国际化）、
      check_currency（多币种，国际版）、check_datetime_tz（时区换算，国际版）、
      assert_no_over_validation（过度校验，B8）、check_error_code_mapping（错误码-文案映射一致性，H9）；
    - 测试数据清理：teardown() 直接批量删除；register_cleanup() 登记崩溃安全清理清单，
      run_cleanup_manifest() 统一补删，--cleanup-only 模式补跑上次遗留。
    - Webhook 测试模式：webhook_trigger() 触发事件 + webhook_verify() 验证推送。
  * 辅助验证能力（调用其他接口交叉验证）
    - verify_by_query()：调用详情查询接口验证字段一致；
    - verify_by_list()：调用列表接口验证资源出现/消失；
    - verify_absent()：删除后验证资源不存在；
    - verify_count_delta()：验证计数变化（+1/-1）；
    - chain_verify()：封装"创建→查询验证"串联模式。

依赖：仅 Python 标准库。
"""

import io
import json
import os
import ssl
import sys
import time
import threading
import urllib.request
import urllib.error

# ===================== 基础配置（个人参数，从配置文件/环境变量加载，禁止写死） =====================
# 个人参数（appid / 账号 / 域名 / corpid / sign / sysid）全部外置，便于 skill 分享给团队其他成员：
#   1) 优先读环境变量：WJX_BASE_URL / WJX_APPID / WJX_MAIN / WJX_SUB / WJX_CORPID / WJX_CROSS_APPID / WJX_SIGN / WJX_SYSID
#   2) 其次读本地配置文件：WJX_CONFIG 指向的路径，或默认 ~/.workbuddy/wjx-api-test/personal_config.json
#   3) 都没配则启动时明确报错（见 main 顶部校验），不写死任何个人默认值。
# 配置模板见 assets/personal_config.example.json（占位符，随 skill 分享）；真实配置放 skill 包外，不入库。
# 测试地址：必须从接口文档（知识库契约）的『接口地址』字段读取实际值，禁止写死默认域名。
# 加载逻辑已抽到 scripts/personal_config.py，供 api_test_runner 与 smoke_runner 共用（个人参数单一来源）。
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from personal_config import (ENCODE, DEFAULT_SIGN, load_personal_config, load_cross_env,
                             config_consistency_check)
# 归因的**单一出处**：本模块与 smoke_runner.py 共用，避免两处各写一套判定。
import attribution_engine as _ATTR

PERSONAL = load_personal_config()
BASE_URL = PERSONAL.get("base_url", "")
APPID = PERSONAL.get("appid", "")
SIGN = PERSONAL.get("sign", DEFAULT_SIGN)
CREATER_MAIN = PERSONAL.get("main_account", "")
CREATER_SUB = PERSONAL.get("sub_account", "")
CORPID = PERSONAL.get("corpid", "")
CROSS_APPID = PERSONAL.get("cross_appid", "")
CREATER_INVALID_SUB = "no_such_sub_account"   # 不存在的子账号（依赖失败用例，非个人参数）

# 【异常场景专用】跨环境参数：仅当显式设置 WJX_CROSS_ENV=<环境name>（可配 WJX_CROSS_CONFIG 指定
# 另一份配置文件）时才加载到 CROSS_CFG，供跨企业/跨租户/越权隔离等异常用例取用。
# 正常用例只用 PERSONAL 里本环境的参数，禁止读取 CROSS_CFG。
CROSS_CFG = None
if os.environ.get("WJX_CROSS_ENV"):
    CROSS_CFG = load_cross_env(
        path=os.environ.get("WJX_CROSS_CONFIG") or os.environ.get("WJX_CONFIG"),
        env=os.environ.get("WJX_CROSS_ENV"),
        region=os.environ.get("WJX_CROSS_REGION"),
    )
    print("[cross-env] 异常场景跨环境参数已显式加载：%s（base_url=%s）"
          % (os.environ.get("WJX_CROSS_ENV"), CROSS_CFG.get("base_url", "")))

TIMEOUT = 60
# TLS 证书校验：默认关闭，便于对接内网 / 自签证书测试环境。
# ⚠️ 关闭证书校验会使请求暴露在中途人(MITM)攻击风险下，仅限受信任的内网测试环境使用。
# 在更严格的环境，设置环境变量 WJX_VERIFY_TLS=1 即可启用默认校验（删除下方两行覆盖）。
SSL_CTX = ssl.create_default_context()
if os.environ.get("WJX_VERIFY_TLS") != "1":
    SSL_CTX.check_hostname = False
    SSL_CTX.verify_mode = ssl.CERT_NONE

# 文档约定的成功响应 data 必含字段（契约校验；按你的接口改）
CONTRACT_KEYS = ["vid", "sid", "status"]

# 可选：标题须与内容内 <title> 一致等"未文档化强校验"——你的接口若有，提供对齐 helper
def html_with_title(title, body=""):
    """生成 title 与 html_content 内 <title> 一致的 HTML（按接口规则对齐，避免误命中一致性校验）。"""
    return f"<html><head><title>{title}</title></head><body>{body}"


# ===================== 运行时容器 =====================
results = []          # 全部用例结果
findings = []         # 统一问题记录：合并 缺陷/待确认/已确认非问题/符合预期/环境风险

# 【运行实证依赖】**已人工确认**的前置依赖，由 flow.py 从 runtime_dependencies.json 注入。
# 空列表 = 与接入前完全一致：不做任何前置判定，失败照常计缺陷（增量接入）。
# 只接受 status=confirmed 的记录——候选依赖不得参与判定，否则一次误判就能让
# 真缺陷静默从台账里消失。
CONFIRMED_PRECONDITION_DEPS = []

# ===================== 覆盖率门禁（反静默缺漏）状态 =====================
# 用户在 main() 里声明本次要覆盖的维度编号（如 ["A","B","C","AA","AB","AC","H"]）。
# 为空则不强制，仅提示建议；设 WJX_STRICT_COVERAGE=1 时声明维度缺用例即 exit(2)。
COVERAGE_PLAN = []

# 约束对账结果：由 case_runner / flow 从 generated_cases.json 的 check_key 字段装载。
# 维度门禁回答「维度跑没跑」，它回答「契约声明的字段约束有没有用例兑现」——两者互补。
CHECK_KEY_MISSING = {}
# 示例（接入你的接口时取消注释并改为本次适用的维度编号；覆盖维度对照见 applicability_matrix.md / framework.md）：
# COVERAGE_PLAN = ["A", "C", "D", "AA", "AB", "AC", "H"]
shallow_passes = 0      # 仅校验 result=true、未做深度断言的"浅用例"计数（报告看似完整的主因）
coverage_blocked = False  # 覆盖率门禁是否拦截（声明维度缺用例）
# 只有这些状态代表"用例真的跑了断言"。SKIPPED / N/A 只是记录了维度编号，
# 不计入覆盖率——否则"一条没跑"会被伪装成"已覆盖"。
# PRECONDITION_UNMET 计入：它确实发了请求、拿到了响应，只是结论既不算通过也不算失败；
# 不计入反而会让"跑过但被前置挡住"的维度被报成覆盖率缺口，制造噪音。
EXECUTED_STATUSES = ("PASS", "FAIL", _ATTR.PRECONDITION_UNMET)


def _is_coverage_gap(finding):
    """识别"覆盖率缺口"类记录：它描述的是"没测到"，不能反过来证明"测到了"。"""
    fid = str(finding.get("id") or "")
    desc = str(finding.get("desc") or finding.get("问题描述") or "")
    return fid.startswith("COV-") or "覆盖率缺口" in desc


# ===================== 工具函数 =====================
def ts():
    return str(int(time.time()))


def build_payload(action, **kwargs):
    p = {"encode": ENCODE, "appid": APPID, "action": action, "ts": ts(), "sign": SIGN}
    p.update(kwargs)
    return p


def complete_request_body(body):
    """补齐请求报文的【公共固定参数】（encode/appid/sign/ts），确保问题单记录的是
    **完整报文**，便于开发直接复现。

    规则（与 references/issue_format.md「请求 Body 须记录真实完整报文」一致）：
      - 仅填补 body 中【缺失】的公共参数，不覆盖已显式提供的字段（如 cross-appid 用例
        显式传跨企业 appid 时不会被覆盖回当前 appid）；
      - ts 每次请求变化，用占位符 `<请求时unix时间戳>` 标明，不写死具体值；
      - 只对标「原始请求报文」（含 action 字段）做补齐；结构化记录（如 chain_verify 的
        create_action/create_params 形式）不含 action 键，不会被误补。
    返回补齐后的【新 dict】，不修改入参。"""
    if not isinstance(body, dict) or "action" not in body:
        return body
    merged = dict(body)
    for k, v in (("encode", ENCODE), ("appid", APPID), ("sign", SIGN), ("ts", "<请求时unix时间戳>")):
        if k not in merged:
            merged[k] = v
    return merged



# 网络异常重试次数（可由执行配置 execution.retry_network_errors 覆盖）。
# 重试后仍失败归 ENV/网络问题，不计入业务缺陷。
RETRY_COUNT = 2

# 最近一次响应的响应头。用**旁路**记录而不是扩充 call() 的返回值：
# 全仓有十余处按 `code, raw, parsed = call(...)` 解包，改返回值会成片炸掉。
LAST_RESPONSE_HEADERS = {}
# 【W35 并发安全】call_meta 的响应头读数走 thread-local：并发执行时各线程读到
# 本线程最近一次请求的头，不会被别的请求覆盖（全局单例在并发下会张冠李戴）。
_HEADERS_LOCAL = threading.local()

# 本次执行**创建成功**的资源（action + id），供收尾清理/残留清单使用（flow.cleanup_step）。
CREATED_RESOURCES = []


def _record_response_headers(headers):
    """把最近一次响应的头写入旁路，供传输层契约检查（check_transport_contract）读取。

    【W35】同时写全局（兼容既有直接读 LAST_RESPONSE_HEADERS 的消费方）与
    thread-local（call_meta 并发路径从 thread-local 读，不会被别的线程覆盖）。
    """
    global LAST_RESPONSE_HEADERS
    try:
        snapshot = dict(headers.items()) if headers is not None else {}
    except Exception:
        snapshot = {}
    LAST_RESPONSE_HEADERS = snapshot
    _HEADERS_LOCAL.headers = snapshot


def call(payload, retries=2, extra_headers=None, adapter=None):
    """发送请求，返回 (code, raw, parsed)。
    code=-1 表示重试后仍连接失败（归 ENV/网络，非业务缺陷）。

    extra_headers 用于模拟不同客户端特征（User-Agent / Accept 等）：黑盒测试拿不到
    多版本 SDK，只能靠改请求头让服务端走到不同的客户端分支。

    adapter：【W36 可选注入】传入 adapters.base 的实例时，响应用
    adapter.classify_response() 统一分类（GraphQL「200+errors 判失败」等防假通过
    逻辑此前是死代码——执行链从不经过它）。不传时行为与接入前完全一致（增量）。
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    hdrs = {"Content-Type": "application/json; charset=utf-8"}
    if extra_headers:
        hdrs.update({str(k): str(v) for k, v in extra_headers.items()})
    last_exc = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                BASE_URL, data=data, headers=hdrs, method="POST")
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CTX) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                code = resp.getcode()
                _record_response_headers(resp.headers)
            parsed = None
            try:
                parsed = json.loads(raw)
            except Exception:
                pass
            # 【W36】注入 adapter 时用 classify_response 统一分类：非 ok 状态把
            # parsed 归一为 {"result": False, "errormsg": ...}，让下游断言链路
            # （run/assert_validation 按 result 分支）在 GraphQL/gRPC 语义下同样成立。
            if adapter is not None:
                try:
                    _cls = adapter.classify_response(
                        type("_Resp", (), {"status_code": code, "body": raw})())
                    _st = str((_cls or {}).get("status", ""))
                    if _st != "ok" and isinstance(parsed, dict) and parsed.get("result") is not False:
                        _err = _cls.get("errors") or _cls.get("message") or _st
                        parsed = {"result": False,
                                  "errormsg": "adapter[%s]: %s" % (_st, _err)}
                except Exception as _exc:  # noqa: BLE001
                    # 分类器故障不改变原判（响应按原样交给下游），但必须出声：adapter
                    # 正是为「HTTP 200 + 业务 errors」补的防假通过机制，它自己故障了却
                    # 零留痕，与「没注入 adapter」在产物上完全无从区分。
                    print("[adapter] 响应分类器故障（%s），本次按原样交给下游：%s"
                          % (_st, _exc), file=sys.stderr)
            return (code, raw, parsed)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            _record_response_headers(getattr(e, "headers", None))
            return (e.code, raw, None)
        except Exception as e:
            last_exc = e
            if attempt < retries:
                time.sleep(0.4 * (attempt + 1))
                continue
            return (-1, f"EXCEPTION:{type(e).__name__}:{e}", None)
    return (-1, f"EXCEPTION:{type(last_exc).__name__}:{last_exc}", None)


def call_raw(body_bytes, content_type="application/json", retries=2):
    """发送原始报文（畸形/非JSON体的系统错误处理用例）。"""
    last_exc = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            BASE_URL, data=body_bytes,
                headers={"Content-Type": content_type}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CTX) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                code = resp.getcode()
                _record_response_headers(resp.headers)
            parsed = None
            try:
                parsed = json.loads(raw)
            except Exception:
                pass
            return (code, raw, parsed)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            return (e.code, raw, None)
        except Exception as e:
            last_exc = e
            if attempt < retries:
                time.sleep(0.4 * (attempt + 1))
                continue
            return (-1, f"EXCEPTION:{type(e).__name__}:{e}", None)
    return (-1, f"EXCEPTION:{type(last_exc).__name__}:{last_exc}", None)


def call_meta(payload, retries=None):
    """同 call()，但额外返回响应头：(code, raw, parsed, headers)。

    给需要看传输层的检查用（Content-Type / charset / Content-Encoding / trace-id 等）。
    响应头由 call() 旁路记录，本函数只负责读取——call() 的返回值契约保持不变。

    【W35 并发安全】LAST_RESPONSE_HEADERS 是全局单例，并发执行时可能被别的请求覆盖
    ——并发用例的请求ID/TraceID 证据会张冠李戴。改为 thread-local 读数：每个线程
    读到的是**本线程最近一次**请求的头；LAST_RESPONSE_HEADERS 保留（全仓十余处直接
    读它的消费方不受影响），语义变为"最后一次"，并发路径不再依赖它。
    """
    code, raw, parsed = call(payload, retries=RETRY_COUNT if retries is None else retries)
    local = getattr(_HEADERS_LOCAL, "headers", None)
    # 【审查修复·HTTPError 有真实响应头】旧的守卫是 `code == -1 or parsed is None`
    # 就返回空头，把两类完全不同的失败混为一谈：
    #   ① code == -1：连接失败，压根没有响应 —— 此时 thread-local 里残留的是
    #      **上一次成功请求**的头，返回它会把旧 TraceID 安在本次失败上，必须空；
    #   ② parsed is None 且 code >= 400：HTTPError（如 500 的 HTML 错误页、
    #      502 网关页）——服务端**确实回了响应头**（Request-ID / TraceID /
    #      Content-Type 常只在这里），把它们丢掉等于丢证据：4xx/5xx 台账里
    #      恰恰最需要这几列来定位。call() 已在 HTTPError 分支调过
    #      _record_response_headers(e.headers)，此处照实返回。
    if code == -1:
        return code, raw, parsed, {}
    if local is None:
        # 兜底：极端情况下（子线程未经过 _record）回落全局，避免返回 NoneType
        local = LAST_RESPONSE_HEADERS
    return code, raw, parsed, dict(local)


# 缺陷证据包目录与本次运行标识：台账「证据路径」列指向这里写出的脱敏 JSON。
# 此前该列恒空——`evidence_bundle.write_bundle` 全仓只有 smoke_runner 接入，深度链路
# 从不落证据包，开发拿不到复现线索，artifact_readback 每轮回读必报 EV_ALL_EMPTY。
# 目录落在已 gitignore 的 api_test_out/ 下，不污染工作树；可用 WJX_EVIDENCE_DIR 覆盖，
# 设成空串则关闭证据包产出（该列随之为空，回读会如实报缺口而不是静默）。
EVIDENCE_DIR = os.environ.get("WJX_EVIDENCE_DIR", os.path.join("api_test_out", "evidence"))
RUN_ID = time.strftime("run_%Y%m%d_%H%M%S")


def _write_evidence(case_id, payload):
    """为一个缺陷落脱敏证据包，返回路径；失败出声并返回空串（不阻断主流程）。"""
    if not EVIDENCE_DIR:
        return ""
    try:
        import evidence_bundle as _evid
        return _evid.write_bundle(EVIDENCE_DIR, RUN_ID, case_id or "case", payload)
    except Exception as exc:  # noqa: BLE001
        print("[evidence] 证据包写入失败（case=%s）：%s；台账「证据路径」留空"
              % (case_id, exc), file=sys.stderr)
        return ""


def add_finding(fid, kind, desc, payload, expected="", actual="", severity="", status="待确认",
                dimension="", confidence=None, http_code=None, request_id="", trace_id="",
                evidence_path="", attribution="", retest_status="待复测"):
    """统一问题记录。kind: 缺陷|待确认|已确认非问题|符合预期|环境风险
    dimension: 所属维度编号（如 B8-2 / AA3 / H9），用于按维度统计缺陷产出、反哺 defect_patterns.md。

    台账列与 case_runner.CSV_COLS 一一对应。采集三列（HTTP状态码 / 请求ID / TraceID）
    通常由调用方经 auto_evidence() 自动填入（run() / assert_validation() 已接好），
    直接调本函数时需自己带 http_code / request_id / trace_id。

    证据路径与归因两列**在此兜底**，不再依赖调用方传：
    - 缺陷（kind=缺陷）落一份脱敏证据包（`evidence_bundle.write_bundle`），台账「证据路径」
      指向它。此前全仓只有 smoke_runner 接入 write_bundle，深度链路从不产出证据包——
      开发拿不到复现线索，且 artifact_readback 每轮回读必报 EV_ALL_EMPTY。
    - 归因默认为 `DEFECT`（本函数记的 kind=缺陷 就是「确认为接口缺陷」这一类，
      与 attribution_engine 的 6 类同词表）；调用方知道更精确归因时用 attribution= 覆盖。
    """
    if kind == "缺陷":
        if not evidence_path:
            evidence_path = _write_evidence(fid, {
                "case": fid, "desc": desc, "request": payload,
                "expected": expected, "actual": actual,
                "http_code": http_code, "dimension": dimension,
            })
        if not attribution:
            attribution = "DEFECT"
    findings.append({
        "id": fid, "kind": kind, "desc": desc,
        "request": json.dumps(complete_request_body(payload), ensure_ascii=False),
        "expected": expected, "actual": actual,
        "severity": severity, "status": status,
        "dimension": dimension,
        "confidence": confidence if confidence is not None else 0.8,
        "http_code": http_code, "request_id": request_id, "trace_id": trace_id,
        "evidence_path": evidence_path, "attribution": attribution,
        "retest_status": retest_status,
    })


# 台账列口径的**唯一来源**：深度流程（本模块 write_csv）与声明式执行器（case_runner.write_reports）
# 共用这一份列定义与渲染函数，避免"同一份台账两套表头"（曾经深度流程只导 9 列，
# 扩展列虽已落库却在 CSV 里被截断，消费方看到的是一张没有证据链的表）。
#
# 列定义外置在 assets/finding_columns.json：改列名或增删列改那个文件，不必动代码。
# assets 缺失或损坏时回退下面的内置默认列——包在缺文件时仍能跑，且出声告知。
DEFAULT_FINDING_COLS = ["编号", "类型", "严重程度", "状态", "维度", "问题描述", "请求Body",
                        "预期结果", "实际结果", "置信度", "HTTP状态码", "请求ID", "TraceID",
                        "证据路径", "归因", "复测状态"]


# finding 的英文键 ↔ 台账表头中文列名。finding_row 按 FINDING_CSV_COLS 取值，
# 所以增删列只需改 assets/finding_columns.json 与这张映射表，不用改渲染逻辑。
_FINDING_FIELD_KEYS = {
    "编号": "id", "类型": "kind", "严重程度": "severity", "状态": "status",
    "维度": "dimension", "问题描述": "desc", "请求Body": "request",
    "预期结果": "expected", "实际结果": "actual", "置信度": "confidence",
    "HTTP状态码": "http_code", "请求ID": "request_id", "TraceID": "trace_id",
    "证据路径": "evidence_path", "归因": "attribution", "复测状态": "retest_status",
}


def _load_finding_cols_from(path):
    """从指定路径读台账列名，失败回退内置默认列并返回告警文案（告警为 None 表示正常）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            cols = [c["header"] for c in json.load(f)["finding_columns"]]
        if not cols or not all(isinstance(c, str) and c for c in cols):
            raise ValueError("列定义为空或含非法表头：%r" % (cols,))
        return cols, None
    except Exception as e:
        return list(DEFAULT_FINDING_COLS), \
            "台账列定义读取失败（%s: %s），已回退内置默认列；改列名请编辑 assets/finding_columns.json" \
            % (type(e).__name__, e)


def _load_finding_cols():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "assets", "finding_columns.json")
    cols, warn = _load_finding_cols_from(path)
    if warn:
        print("⚠️ " + warn)
    return cols, warn


FINDING_CSV_COLS, FINDING_COLS_WARN = _load_finding_cols()

# 加载期即点名未知表头：新增一列却忘了加映射键时，finding_row 会把它渲染成空值，
# 而空值不报错——整列证据链悄悄消失，正是本模块要防的那类静默失败。
_UNKNOWN_HEADERS = [h for h in FINDING_CSV_COLS if h not in _FINDING_FIELD_KEYS]
if _UNKNOWN_HEADERS:
    _msg = ("台账列定义含未登记映射的表头 %s，对应单元格将渲染为空值；"
            "请在 assets/finding_columns.json 与 _FINDING_FIELD_KEYS 中同步登记" % (_UNKNOWN_HEADERS,))
    print("⚠️ " + _msg)
    FINDING_COLS_WARN = " | ".join(x for x in (FINDING_COLS_WARN, _msg) if x)


def _clip_cell(text, limit=500):
    """台账单元格截断：报告要能被人读，证据全文在 report_full.json 里。"""
    text = str(text or "")
    return text if len(text) <= limit else "%s…(共%d字)" % (text[:limit], len(text))


def finding_row(x):
    """把一条 finding 渲染成与 FINDING_CSV_COLS 对齐的一行（含失败最小复现附加信息）。

    超长单元格（如 UI 代理用例的长文本 payload）截断到 500 字并标明原始长度——
    截断只发生在 CSV 渲染层，findings 本体与 report_full.json 保留全文作证据。

    按当前列定义逐列取值：列定义里出现未知表头时填空值并已在加载期校验过非空，
    不静默错位（错位会让整张台账的证据链整体偏移，是最难发现的静默失败）。"""
    actual = x.get("actual", "")
    if x.get("minimal_repro"):
        actual = "%s ；最小复现请求：%s" % (actual, x["minimal_repro"])
    out = []
    for header in FINDING_CSV_COLS:
        key = _FINDING_FIELD_KEYS.get(header)
        val = x.get(key, "") if key else ""
        # 「实际结果」列取上面拼好的 minimal_repro 版本，其余列直接取 finding 原值
        if header == "实际结果":
            val = actual
        # desc / request / expected / actual 是长文本列，超长截断并标明原始长度
        if header in ("问题描述", "请求Body", "预期结果", "实际结果"):
            val = _clip_cell(val)
        out.append(val)
    return out


def extract_request_ids(headers):
    """从响应头提取请求ID / TraceID，用于台账证据链。

    服务端把链路ID放在响应头是通行做法，但**头名各系统不统一**，故按常见候选名
    逐个匹配（大小写不敏感）。取不到就返回空串——台账留空是可见的"没采到"，
    比填一个占位值更诚实。
    """
    if not headers:
        return "", ""
    lower = {str(k).lower(): v for k, v in dict(headers).items()}
    rid_keys = ("x-request-id", "request-id", "x-requestid", "x-rid",
                "x-correlation-id", "x-cid")
    tid_keys = ("x-trace-id", "trace-id", "x-traceid", "x-b3-traceid", "x-tid",
                "x-amzn-trace-id", "x-ot-span-trace-id")
    rid = next((lower[k] for k in rid_keys if lower.get(k)), "")
    tid = next((lower[k] for k in tid_keys if lower.get(k)), "")
    return str(rid or ""), str(tid or "")


def auto_evidence(http_code=None, headers=None):
    """按响应事实自动填台账的采集类字段。

    存在的理由：HTTP 状态码是 `call()` 返回值的第一项、响应头早已由
    `_record_response_headers` 旁路采好，但两者都没接进台账——**输入全在手上，
    只是没人把它们交给 add_issue**，导出的 CSV 里这几列恒为空。
    签名按"只传拿得到的"设计：headers 省略时回退读旁路缓存。

    【审查修复·W35 并发安全】回退此前直接读模块级 LAST_RESPONSE_HEADERS 全局
    单例——并发执行时它会被别的线程的请求覆盖，台账里的 request_id/trace_id
    会张冠李戴（本函数正是 W35 要保护的那条证据链，却仍在读全局，
    与 call_meta 的 thread-local 口径不一致）。改为与 call_meta 同源：
    先读本线程最近一次请求的头（_HEADERS_LOCAL），本线程没记录过才回落全局。
    """
    if headers is None:
        headers = getattr(_HEADERS_LOCAL, "headers", None)
        if headers is None:
            headers = LAST_RESPONSE_HEADERS
    rid, tid = extract_request_ids(headers)
    return {"http_code": http_code, "request_id": rid, "trace_id": tid}


def add_issue(desc, payload, fid="", expected="", actual="", severity="中", dimension="",
              http_code=None, headers=None, attribution=""):
    ev = auto_evidence(http_code, headers)
    add_finding(fid, "缺陷", desc, payload, expected, actual, severity, "待修复",
                dimension=dimension, attribution=attribution, **ev)


def add_observation(desc, payload, fid="", expected="", actual="", severity="中",
                    dimension="", http_code=None, headers=None, attribution=""):
    ev = auto_evidence(http_code, headers)
    add_finding(fid, "待确认", desc, payload, expected, actual, severity, "待确认",
                dimension=dimension, attribution=attribution, **ev)


def assert_non_empty(items, label="断言集合"):
    """【空转假通过防护】确保待断言集合非空（陷阱 17）。

    用于包裹用户自定义的断言循环：若 items 为空（如枚举文档为空、批量明细为空），
    循环体 0 次执行会恒 PASS，把"没测"伪装成"通过"。
    返回 (ok, msg)：ok=False 表示空转，应判该用例无效并告警（如 add_issue）。
    用法：ok, msg = assert_non_empty(documented_enum_values, "枚举文档"); if not ok: add_issue(...)"""
    if items is None:
        return False, f"{label} 为 None（空转：未提供任何待校验项）"
    if hasattr(items, "__len__") and len(items) == 0:
        return False, f"{label} 为空（空转：循环 0 次执行，恒 PASS 会掩盖漏测）"
    return True, ""


def check_contract(data):
    """校验成功响应 data 字段与类型，返回 (ok, msg)。按接口改 CONTRACT_KEYS / 类型规则。"""
    if not isinstance(data, dict):
        return False, "data 非对象"
    missing = [k for k in CONTRACT_KEYS if k not in data]
    if missing:
        return False, f"缺少契约字段: {missing}"
    return True, "契约字段齐全"


# ===================== 增强断言 helper =====================
def check_timezone(timestamp_str, expected_offset_hours=8, field_name="时间字段"):
    """校验时间字段的时区偏移是否符合预期。
    历史缺陷：接口传 UTC 时间，页面显示多 8 小时。
    返回 (ok, msg)。timestamp_str 需为可解析的 ISO 格式或带时区字符串。"""
    import re
    # 提取时区偏移，如 +08:00 / Z / +0000
    m = re.search(r'([+-]\d{2}:?\d{2}|Z)$', str(timestamp_str).strip())
    if not m:
        return None, f"{field_name} 无时区信息，无法校验（需确认是否为本地时间）"
    offset = m.group(1)
    if offset == 'Z':
        actual_hours = 0
    else:
        sign = 1 if offset[0] == '+' else -1
        parts = offset[1:].replace(':', '')
        actual_hours = sign * int(parts[:2])
    if actual_hours == expected_offset_hours:
        return True, f"{field_name} 时区偏移 +{actual_hours}h，符合预期"
    return False, f"{field_name} 时区偏移 {actual_hours}h，预期 +{expected_offset_hours}h（可能存在时区转换缺陷）"


def check_idempotent(action, payload_builder, times=2, **kwargs):
    """校验接口幂等性：重复发送相同请求 N 次，验证结果一致且不产生重复数据。
    返回 (ok, results_list, msg)。
    用法：check_idempotent("1000107", lambda: {"creater": "x", "title": "幂等测试"}, times=2)

    【反假通过 W3】接口全失败时（code=-1 或 parsed 缺 result 键）不得判「幂等通过」：
    None==None 的比对恒真，会把「接口宕机/网络挂」伪装成「幂等正常」。全失败返回
    (None, results, msg)，由调用方归 ENV 或重试——绝不返回 True。
    """
    results = []
    for i in range(times):
        p = build_payload(action, **payload_builder())
        p.update(kwargs)
        code, raw, parsed = call(p)
        results.append({"code": code, "parsed": parsed, "raw": raw[:200]})
    # 【W3】先验证「接口真的在正常响应」，再谈幂等：任何一次 code=-1（连接失败），
    # 或 parsed 没有 result 键（响应非预期形状），都不具备比对前提。
    comparable = []
    # 【三轮审查】enumerate 取代 results.index(r)：两次响应完全相同时 index 按值
    # 查找恒返 0，「第 N 次」文案错位（不影响判定，纯文案）。
    for idx, r in enumerate(results, 1):
        if r["code"] == -1:
            return None, results, "第 %d 次请求连接失败(code=-1)，无法验证幂等性（归 ENV，需重跑）" % idx
        p = r["parsed"]
        if not (isinstance(p, dict) and "result" in p):
            return None, results, "第 %d 次响应无 result 键（非 JSON/网关错误），无法验证幂等性（需确认）" % idx
        comparable.append(p)
    first = comparable[0]
    all_same = all(p == first for p in comparable)
    if all_same:
        return True, results, f"重复 {times} 次结果一致，幂等性通过"
    return False, results, f"重复 {times} 次结果不一致，幂等性存疑（需确认是否应幂等）"


def check_field_naming(data, expected_pattern=r'^[a-z][a-z0-9_]*$', exclude_keys=None):
    """校验响应字段命名是否符合规范（默认小写下划线）。
    历史缺陷：num_vaild 拼写错误应为 num_valid。
    返回 (ok, bad_fields, msg)。"""
    import re
    exclude = set(exclude_keys or [])
    bad = []
    if isinstance(data, dict):
        for k in data.keys():
            if k in exclude:
                continue
            if not re.match(expected_pattern, str(k)):
                bad.append(k)
    if bad:
        return False, bad, f"字段命名不规范: {bad}（预期小写下划线，检查拼写错误如 num_vaild→num_valid）"
    return True, [], "字段命名规范"


def check_batch_partial(parsed, expected_success_count=None, expected_fail_count=None):
    """校验批量操作的部分成功语义。
    历史缺陷：批量修改一个有问题整体报错，其他都改不了；应成功项保存、失败项返回明细。
    返回 (ok, msg)。"""
    if not isinstance(parsed, dict):
        return None, "响应非对象，无法校验批量结果"
    data = parsed.get("data") or {}
    success = data.get("success_count") or data.get("success") or 0
    fail = data.get("fail_count") or data.get("failed") or 0
    details = data.get("fail_details") or data.get("errors") or []
    msg_parts = [f"成功={success}, 失败={fail}"]
    if details:
        msg_parts.append(f"失败明细={len(details)}条")
    ok = True
    if expected_success_count is not None and success != expected_success_count:
        ok = False
        msg_parts.append(f"预期成功={expected_success_count}")
    if expected_fail_count is not None and fail != expected_fail_count:
        ok = False
        msg_parts.append(f"预期失败={expected_fail_count}")
    if fail > 0 and not details:
        ok = False
        msg_parts.append("有失败但无失败明细（部分成功语义缺失）")
    return ok, ", ".join(msg_parts)


def check_download_url(parsed, url_field="download_url", timeout=10):
    """校验导出接口返回的下载链接可访问且在有效期内。
    返回 (ok, msg)。"""
    if not isinstance(parsed, dict):
        return False, "响应非对象"
    data = parsed.get("data") or {}
    url = data.get(url_field) or parsed.get(url_field)
    if not url:
        return False, f"未找到下载链接字段 {url_field}"
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
            code = resp.getcode()
        if 200 <= code < 400:
            return True, f"下载链接可访问 (HTTP {code})"
        return False, f"下载链接不可访问 (HTTP {code})，可能已过期"
    except Exception as e:
        return False, f"下载链接访问失败: {type(e).__name__}: {e}"


def check_resource_url(parsed, url_field="url", expect_kind="", timeout=10):
    """【接口侧 UI 代理信号】资源 URL 可用性 + 类型 + 尺寸合理性。

    这是接口侧**不需要页面基线**就能自动发现的 UI 还原前因：
      - 图片 404 / 403        → 前端图片裂开；
      - Content-Type 不符      → 前端按错误方式解码（图片当文本、HTML 当图片）；
      - Content-Length 为 0    → 前端渲染空白占位，版面塌陷。

    expect_kind: "image" / "html" / "pdf" / ""（不校验类型）。
    返回 (ok, msg, meta)，meta 含 url / status / content_type / content_length / warn。
    """
    meta = {"url": "", "status": None, "content_type": "", "content_length": None, "warn": ""}
    if not isinstance(parsed, dict):
        return False, "响应非对象", meta
    data = parsed.get("data") if isinstance(parsed.get("data"), dict) else {}
    url = data.get(url_field) or parsed.get(url_field)
    if not url:
        return False, "未找到资源链接字段 %s" % url_field, meta
    meta["url"] = str(url)
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
            code = resp.getcode()
            ctype = (resp.headers.get("Content-Type") or "").lower()
            clen = resp.headers.get("Content-Length")
    except Exception as e:
        return False, "资源不可访问: %s: %s" % (type(e).__name__, e), meta
    meta["status"] = code
    meta["content_type"] = ctype
    meta["content_length"] = int(clen) if (clen or "").isdigit() else None
    if not (200 <= code < 400):
        return False, "资源不可访问 (HTTP %s)" % code, meta
    if meta["content_length"] == 0:
        return False, "资源为空（Content-Length=0），前端将渲染空白占位", meta
    kinds = {"image": ("image/", "图片"), "html": ("text/html", "HTML"),
             "pdf": ("application/pdf", "PDF")}
    if expect_kind in kinds:
        prefix, label = kinds[expect_kind]
        if prefix not in ctype:
            return False, "资源类型不符：期望 %s，实际 Content-Type=%r" % (label, ctype), meta
    if not ctype:
        meta["warn"] = "资源 %s 未返回 Content-Type，前端可能按错误类型解码" % url_field
        return True, "资源可访问但缺 Content-Type", meta
    return True, "资源可访问 (HTTP %s, %s)" % (code, ctype), meta


def check_blob_integrity(source_bytes, downloaded_bytes=None, expected_sha256=None, min_size=1,
                         content_type=None, expected_content_type=None):
    """【文件/大对象完整性】校验上传/下载/导出类二进制内容是否完整。

    适用：文件上传、附件下载、导出文件、大对象响应、压缩包与图片/音频等二进制内容。
    可检查：最小大小、SHA256、一致性、Content-Type 期望。
    返回 (ok, problems, msg)。"""
    import hashlib
    problems = []
    src = source_bytes or b""
    down = downloaded_bytes if downloaded_bytes is not None else src
    if len(src) < min_size:
        problems.append(f"源内容过小：{len(src)} bytes，小于最小值 {min_size}")
    if len(down) < min_size:
        problems.append(f"下载内容过小：{len(down)} bytes，小于最小值 {min_size}")
    if expected_sha256:
        src_sha = hashlib.sha256(src).hexdigest()
        down_sha = hashlib.sha256(down).hexdigest()
        if src_sha != expected_sha256:
            problems.append(f"源内容 SHA256 不符：{src_sha} != {expected_sha256}")
        if down_sha != expected_sha256:
            problems.append(f"下载内容 SHA256 不符：{down_sha} != {expected_sha256}")
    if expected_content_type and content_type and str(content_type).lower() != str(expected_content_type).lower():
        problems.append(f"Content-Type 不符：实际 {content_type!r}，预期 {expected_content_type!r}")
    if problems:
        return False, problems, f"文件/大对象完整性失败: {'; '.join(problems)}"
    return True, [], "文件/大对象完整性通过"


def check_enum_exhaustive(actual_enum_values, documented_enum_values, field_name="枚举字段"):
    """校验接口返回的枚举值是否与文档一致（不多不少）。
    历史缺陷：分享方式枚举缺内部消息；limit_join_type 写"每年"实为"每周"。
    返回 (ok, missing, extra, msg)。

    额外支持：以差异集合形式返回，便于契约治理与文档回归自动化。"""
    actual = set(str(v) for v in actual_enum_values)
    documented = set(str(v) for v in documented_enum_values)
    missing = documented - actual
    extra = actual - documented
    if not missing and not extra:
        return True, set(), set(), f"{field_name} 枚举与文档完全一致"
    msg = f"{field_name} 枚举不一致"
    if missing:
        msg += f"，文档有但接口缺: {missing}"
    if extra:
        msg += f"，接口有但文档未声明: {extra}"
    return False, missing, extra, msg


def check_contract_drift(current_contract, baseline_contract, *, allow_additive_fields=True,
                         allow_additive_enum_values=False, field_name="契约"):
    """【契约治理】对比当前契约与基线契约，识别破坏性/非破坏性变更。

    current_contract / baseline_contract 均为字典，可包含：fields、required_fields、enums、headers、status_codes。
    返回 (ok, issues, msg)。

    规则：
    - 删除字段、收紧必填、缩小枚举、收紧状态码 → 破坏性变更
    - 新增字段、扩展枚举、增加可选项 → 默认视为可接受（可通过参数关闭）
    - 适用于接口契约治理、回归差异比对、版本升级审核。"""
    issues = []
    cur = current_contract or {}
    base = baseline_contract or {}

    cur_fields = set(cur.get("fields") or [])
    base_fields = set(base.get("fields") or [])
    removed = base_fields - cur_fields
    added = cur_fields - base_fields
    if removed:
        issues.append(f"字段被删除: {sorted(removed)}")
    if added and not allow_additive_fields:
        issues.append(f"新增字段未允许: {sorted(added)}")

    cur_req = set(cur.get("required_fields") or [])
    base_req = set(base.get("required_fields") or [])
    newly_required = cur_req - base_req
    if newly_required:
        issues.append(f"新增必填字段: {sorted(newly_required)}")

    cur_enums = cur.get("enums") or {}
    base_enums = base.get("enums") or {}
    for field, base_values in base_enums.items():
        cur_values = set(cur_enums.get(field) or [])
        base_values = set(base_values or [])
        miss = base_values - cur_values
        extra = cur_values - base_values
        if miss:
            issues.append(f"枚举 {field} 缺失: {sorted(miss)}")
        if extra and not allow_additive_enum_values:
            issues.append(f"枚举 {field} 新增值未允许: {sorted(extra)}")

    base_headers = set((base.get("headers") or []))
    cur_headers = set((cur.get("headers") or []))
    if base_headers - cur_headers:
        issues.append(f"请求头契约被削减: {sorted(base_headers - cur_headers)}")

    base_codes = set((base.get("status_codes") or []))
    cur_codes = set((cur.get("status_codes") or []))
    if base_codes - cur_codes:
        issues.append(f"状态码支持被削减: {sorted(base_codes - cur_codes)}")

    if issues:
        return False, issues, f"{field_name} 存在破坏性变更: {'; '.join(issues)}"
    return True, [], f"{field_name} 与基线兼容"


def check_repro_trace(request_payload, response_payload=None, endpoint=None, headers=None,
                      correlation_id=None, trace_id=None, file_path=None, sql=None, notes=None):
    """【可观测性与复现链路】生成可直接交付的复现记录。

    用于把问题单补齐为可执行、可追踪的复现材料：请求、响应、端点、headers、trace/correlation id、文件/SQL 线索。
    返回一个 dict，可直接写入问题台账或附加到报告。"""
    trace = {
        "endpoint": endpoint,
        "headers": headers or {},
        "request": request_payload,
        "response": response_payload,
        "correlation_id": correlation_id,
        "trace_id": trace_id,
        "file_path": file_path,
        "sql": sql,
        "notes": notes,
    }
    trace["repro_text"] = json.dumps(trace, ensure_ascii=False, indent=2)
    return trace


def check_streaming_protocol(metadata, frames=None, expected_open=True, expected_close=True,
                             order_sensitive=True, allow_heartbeat=True):
    """【流式协议专项】校验 WebSocket / SSE / gRPC-stream 类交互的最小行为。

    适用：连接建立、心跳、消息顺序、关闭语义、重连、丢帧、重复帧。
    metadata 可包含 protocol / connection_state / close_code / headers 等。
    frames 为按顺序的事件/消息列表，每项可含 seq/type/payload/timestamp。
    返回 (ok, problems, msg)。"""
    problems = []
    meta = metadata or {}
    frames = frames or []
    protocol = str(meta.get("protocol") or "").lower()
    if protocol and protocol not in {"websocket", "ws", "sse", "grpc", "grpc-stream", "stream"}:
        problems.append(f"未知流式协议类型: {protocol}")
    if expected_open and meta.get("opened") is False:
        problems.append("连接未建立成功")
    if expected_close and meta.get("closed") is False and meta.get("close_code") is None:
        problems.append("未观察到正常关闭语义")

    seqs = []
    for i, frame in enumerate(frames):
        if isinstance(frame, dict):
            seq = frame.get("seq", i)
            ftype = str(frame.get("type") or frame.get("event") or "").lower()
        else:
            seq = i
            ftype = ""
        seqs.append(seq)
        if not allow_heartbeat and ftype == "heartbeat":
            problems.append("出现 heartbeat，但当前场景不允许心跳帧")
    if order_sensitive and seqs and seqs != sorted(seqs):
        problems.append(f"消息顺序异常: {seqs}")
    if len(seqs) != len(set(seqs)):
        problems.append("检测到重复帧序号，可能存在重放/重复投递")
    if not frames and expected_open:
        problems.append("无任何帧记录，无法证明流式交互有效")

    if problems:
        return False, problems, f"流式协议异常: {'; '.join(problems)}"
    return True, [], "流式协议最小行为通过"


def check_release_observability(events, metrics=None, trace_id=None, correlation_id=None,
                               required_events=None, min_logs=1, max_errors=0):
    """【可观测性与复现链路】验证日志/指标/追踪是否足够支撑定位。

    events: 观察到的日志或事件序列；metrics: 指标字典；required_events: 必须出现的关键事件名列表。
    返回 (ok, problems, msg)。"""
    problems = []
    events = events or []
    metrics = metrics or {}
    names = []
    for ev in events:
        if isinstance(ev, dict):
            names.append(str(ev.get("name") or ev.get("event") or ev.get("type") or ""))
        else:
            names.append(str(ev))
    if len(events) < min_logs:
        problems.append(f"日志/事件过少: {len(events)} < {min_logs}")
    if required_events:
        missing = [e for e in required_events if e not in names]
        if missing:
            problems.append(f"缺少关键事件: {missing}")
    error_count = metrics.get("error_count")
    if error_count is not None and error_count > max_errors:
        problems.append(f"错误计数过高: {error_count} > {max_errors}")
    if trace_id and not any(trace_id in str(ev) for ev in events):
        problems.append("未在日志/事件中发现 trace_id")
    if correlation_id and not any(correlation_id in str(ev) for ev in events):
        problems.append("未在日志/事件中发现 correlation_id")
    if problems:
        return False, problems, f"可观测性不足: {'; '.join(problems)}"
    return True, [], "可观测性与复现链路通过"


# ===================== 补充维度断言 helper（AA–AD / B8-B9 / H6-H7 / 状态码语义） =====================
def assert_no_over_validation(case_no, name, action, **kwargs):
    """【反向盲区专用】断言"合法输入应被接受"——与 assert_validation() 方向相反。

    用于 B8 过度校验：文档标注为可选 / 可空的字段，传 null / 空串 / 不传 → **应成功**。
    若接口拒绝，即为"过度校验缺陷"，必须记问题单，不能归因为"需求就是这样"。

    最高发触发点：把读接口返回的结构原样回写给写接口（历史缺陷：获取外观设置返回的 data
    直接传给保存外观设置，报 The BackgroundImage field is required.，而该字段本可为空）。

    返回 (ok, parsed)。失败时自动记入问题台账。"""
    _strip_control_keys(kwargs)
    payload = build_payload(action, **kwargs)
    code, raw, parsed = call(payload, retries=RETRY_COUNT)
    result = parsed.get("result") if isinstance(parsed, dict) else None
    errmsg = str(parsed.get("errormsg", "")) if isinstance(parsed, dict) else ""

    if code >= 500:
        add_issue(f"[{case_no}]{name} 服务端返回 {code}", payload, fid=case_no,
                  expected="HTTP 2xx/4xx 业务错误", actual=f"HTTP {code}")
    if result is True:
        status, reason = "PASS", "合法输入被接受"
    elif result is False:
        status, reason = "FAIL", f"合法输入被拒绝（过度校验）: {errmsg}"
        add_issue(f"[{case_no}]{name} 过度校验：该字段应允许为空/不传，却被拒绝：{errmsg}",
                  payload, fid=case_no, expected="result=true（合法输入应被接受）",
                  actual=f"result=false（{errmsg}）", severity="中")
    else:
        status, reason = "FAIL", f"未知result值: {result}"
        add_issue(f"[{case_no}]{name} 返回未知result值: {result}", payload, fid=case_no,
                  expected="result 为 true/false", actual=f"result={result}")

    print(f"[{case_no}] {name} | 分类=B8过度校验 | HTTP={code} | {status}")
    print(f"        请求: {json.dumps(payload, ensure_ascii=False)}")
    print(f"        响应: {raw[:200]}")
    print(f"        说明: {reason}\n")
    results.append({"case_no": case_no, "name": name, "category": "B8过度校验",
                    "action": action, "http_code": code, "status": status,
                    "reason": reason, "applicability": "适用",
                    "response": parsed if parsed is not None else raw})
    return status == "PASS", parsed


def check_null_triad(action, field, base_payload, case_prefix="B9"):
    """【空值四态】对同一可选字段分别传：缺失 / null / "" / [] —— 四态行为须文档化且一致。

    base_payload 为不含 field 的完整参数字典。返回 (results_dict)。
    典型缺陷：非必填手机号传 "" 报格式错误；内容字段传空串竟创建成功。"""
    variants = {
        "缺失": "__OMIT__",  # 不放进 payload
        "null": None,        # 显式 null（json.dumps 序列化为 null）
        "空串": "",
        "空数组": [],
    }
    out = {}
    for label, val in variants.items():
        payload = dict(base_payload)
        if val != "__OMIT__":
            payload[field] = val
        code, raw, parsed = call(build_payload(action, **payload))
        result = parsed.get("result") if isinstance(parsed, dict) else None
        errmsg = str(parsed.get("errormsg", "")) if isinstance(parsed, dict) else ""
        out[label] = {"http": code, "result": result, "errmsg": errmsg}
        print(f"[{case_prefix}] {field} = {label:<4} | HTTP={code} | result={result} | {errmsg[:80]}")
    inconsistent = len({(v["result"]) for v in out.values()}) > 1
    if inconsistent:
        add_issue(f"[{case_prefix}]{field} 空值四态行为不一致：{ {k: v['result'] for k, v in out.items()} }",
                  base_payload, fid=case_prefix,
                  expected="四态行为文档化且一致", actual=str({k: v["result"] for k, v in out.items()}),
                  severity="中")
    return out


def check_http_semantics(code, business_result, business_code=None, expected_http=None):
    """【HTTP 状态码语义】校验业务语义与 HTTP 状态码映射是否一致。

    只判 5xx 远远不够：参数错误应 400、未认证应 401、越权应 403、不存在应 404、
    冲突应 409、限流应 429。401 与 403 混用是最典型的误用——客户端无法判断
    该重新登录还是申请授权。
    返回 (ok, msg)。"""
    if expected_http is not None:
        if code == expected_http:
            return True, f"HTTP {code} 符合预期"
        return False, f"HTTP 状态码语义不符：实际 {code}，预期 {expected_http}"

    if code >= 500:
        return False, f"HTTP {code} 服务端异常（一律记缺陷）"
    if business_result is False and code == 200:
        return None, ("业务失败但 HTTP 200（伪成功）——若非文档化约定的单 POST JSON 网关语义，"
                      "应返回对应 4xx 状态码")
    if code in (400, 401, 403, 404, 405, 409, 429):
        return True, f"HTTP {code} 与业务失败语义一致"
    return True, f"HTTP {code}"


def check_transport_contract(headers, expected=None, required=None,
                             field_name="请求头/传输契约", expect_json=False):
    """【V / K】校验请求头与传输契约是否明确且符合预期。

    适用：Content-Type / Accept / charset / gzip / idempotency-key / trace-id / correlation-id
    等传输层契约。用于补足仅看 body 和 result 不足以发现的网关 / SDK / 中间件问题。
    expect_json=True 时额外校验 Content-Type 声明为 JSON：响应体是 JSON 却声明
    text/html，部分客户端会解析失败——属传输层契约缺陷。
    返回 (ok, problems, msg)。"""
    problems = []
    headers = headers or {}
    expected = expected or {}
    required = list(required or [])
    lower_map = {str(k).lower(): v for k, v in headers.items()}

    for key in required:
        if str(key).lower() not in lower_map:
            problems.append(f"缺少必需头 {key}")

    for key, val in expected.items():
        actual = lower_map.get(str(key).lower())
        if actual is None:
            problems.append(f"缺少预期头 {key}")
        elif str(actual) != str(val):
            problems.append(f"{key} 取值不符：实际 {actual!r}，预期 {val!r}")

    if expect_json:
        ct = str(lower_map.get("content-type") or "")
        if ct and "json" not in ct.lower():
            problems.append(f"Content-Type 未声明 JSON：实际 {ct!r}")

    if problems:
        return False, problems, f"{field_name} 存在问题: {'; '.join(problems)}"
    return True, [], f"{field_name} 校验通过"


def check_error_root_cause(action, payload_a, payload_b, cause_a="凭证错误", cause_b="参数缺失"):
    """【根因不掩盖】两种不同成因的错误请求，错误信息必须不同。

    历史缺陷：传错 roleId / 用户名 / 时间戳，一律提示"sign 验证失败"；
    缺少 appId 也报"验签失败"——真实根因被掩盖，排查被彻底误导。
    返回 (ok, msg_a, msg_b, msg)。"""
    _, _, pa = call(build_payload(action, **payload_a))
    _, _, pb = call(build_payload(action, **payload_b))
    ma = str(pa.get("errormsg", "")) if isinstance(pa, dict) else ""
    mb = str(pb.get("errormsg", "")) if isinstance(pb, dict) else ""
    if not ma or not mb:
        return None, ma, mb, "至少一侧未返回错误信息，无法比对（需确认是否真的失败）"
    if ma == mb:
        return False, ma, mb, (f"根因被掩盖：「{cause_a}」与「{cause_b}」返回完全相同的错误信息 "
                               f"「{ma}」，排查会被误导，应分别给出具体原因")
    return True, ma, mb, "不同成因的错误信息不同，根因未被掩盖"


def check_error_i18n(errmsg, expect_lang=None, placeholder_patterns=None):
    """【错误信息渲染与国际化】检测未渲染的模板占位，并按期望语言校验。

    历史缺陷：邮箱重复时提示 Email[]（占位未渲染）；报错返回变量形式未转英文。
    expect_lang: 'zh' 期望含中文 / 'en' 期望不含中文。为 None 时只做占位检测。
    返回 (ok, problems, msg)。"""
    import re
    pats = placeholder_patterns or [r'\{+\d+\}+', r'\{+[a-zA-Z_]+\}+', r'%[sd]',
                                    r'\[\]', r'\(\)', r'\$\{[^}]*\}', r'\{\{[^}]*\}\}']
    problems = []
    for p in pats:
        if re.search(p, str(errmsg)):
            problems.append(f"疑似未渲染占位（匹配 {p}）")
    if expect_lang == 'zh' and not re.search(r'[\u4e00-\u9fff]', str(errmsg)):
        problems.append("期望中文错误信息，实际无中文")
    if expect_lang == 'en' and re.search(r'[\u4e00-\u9fff]', str(errmsg)):
        problems.append("期望英文错误信息，实际含中文（国际版场景需关注）")
    if problems:
        return False, problems, f"错误信息「{errmsg}」存在问题: {'; '.join(problems)}"
    return True, [], "错误信息渲染完整、语言符合预期"


def check_pagination(list_action, base_params, page_param="page_index",
                     size_param="page_size", total_field="total", list_field="list",
                     max_page_size=100000):
    """【AA 分页】校验分页边界、total 一致性、末页行为。

    重点：筛选条件无效时**严禁静默返回全量**（历史缺陷：state 参数筛选不出来，返回的是全部数据）。
    返回 (ok, findings, msg)。

    新增能力：支持 cursor/token 类分页字段校验，确保翻页游标稳定、不会重复/回退。"""
    findings = []

    def _call(**extra):
        params = dict(base_params)
        params.update(extra)
        code, raw, parsed = call(build_payload(list_action, **params))
        return code, parsed

    # 基线：第 1 页
    code, base = _call(**{page_param: 1, size_param: 10})
    if not isinstance(base, dict):
        return None, findings, "列表接口未返回对象，无法校验分页"
    bdata = base.get("data") or {}
    total = bdata.get(total_field)
    base_items = bdata.get(list_field) or []
    findings.append(f"基线: total={total}, 本页条数={len(base_items)}")

    # cursor/token 类字段自动探测
    cursor_keys = ["cursor", "next_cursor", "nextCursor", "next_token", "nextToken", "page_token", "pageToken"]
    cursor_key = next((k for k in cursor_keys if k in bdata), None)
    if cursor_key:
        cursor = bdata.get(cursor_key)
        findings.append(f"检测到游标字段 {cursor_key}={cursor!r}")
        if cursor in (None, "", [], {}):
            findings.append(f"游标字段 {cursor_key} 为空，无法稳定翻页")
        else:
            c2, r2 = _call(**{cursor_key: cursor, page_param: 1, size_param: 10})
            r2data = (r2.get("data") or {}) if isinstance(r2, dict) else {}
            items2 = r2data.get(list_field) or []
            if isinstance(items2, list) and items2 == base_items:
                findings.append(f"游标 {cursor_key} 重新请求后仍返回相同首页结果（疑似游标未生效）")

    # 边界：0 / 负数 / 超大
    for label, val in [("0", 0), ("负数", -1), ("超大", max_page_size)]:
        c, r = _call(**{page_param: val})
        if c >= 500:
            findings.append(f"page={val} → HTTP {c}（服务端异常，应为 4xx 或空结果）")
        else:
            findings.append(f"page={val} → HTTP {c}, result={r.get('result') if isinstance(r, dict) else '?'}")

    # 条数边界
    for label, val in [("0", 0), ("负数", -1), ("超大", max_page_size)]:
        c, r = _call(**{page_param: 1, size_param: val})
        if c >= 500:
            findings.append(f"size={val} → HTTP {c}（服务端异常，应有上限保护）")
        else:
            findings.append(f"size={val} → HTTP {c}, result={r.get('result') if isinstance(r, dict) else '?'}")

    # 超范围页码（末页行为）
    if isinstance(total, int) and total > 0:
        far = (total // 10) + 100
        c, r = _call(**{page_param: far, size_param: 10})
        rdata = (r.get("data") or {}) if isinstance(r, dict) else {}
        items = rdata.get(list_field)
        if items is None and r is not None and isinstance(r, dict) and r.get("result") is False:
            findings.append(f"超范围页码 page={far} → 拒绝（已定义，可接受）")
        elif isinstance(items, list) and len(items) == 0:
            findings.append(f"超范围页码 page={far} → 返回空列表（已定义，可接受）")
        elif isinstance(items, list) and len(items) > 0:
            findings.append(f"超范围页码 page={far} → 仍返回 {len(items)} 条（疑似静默回退到首页）")

    bad = [f for f in findings if ("HTTP 5" in f) or ("疑似静默回退" in f) or ("服务端异常" in f) or ("游标" in f and "为空" in f)]
    # 【W7 兑现 docstring 承诺】「筛选失效严禁静默返全量」此前只在文案里：
    # 边界循环只把 result 拼进字符串、从不比对返回条数 vs total。补一个显式检查：
    # 用一条不可能命中的筛选值请求——返回条数仍等于全量 total 即筛选静默失效。
    if isinstance(total, int) and total > 0 and base_items:
        # 【审查修复·判定条件】比条数必须在**同一口径**上。旧实现请求页长用基线页长
        # （max(10, len(base_items))），却拿返回条数与**全量 total** 比：total 一旦
        # 大于页长（total=57 / page_size=10 是最常见形态），返回条数恒等于页长，
        # 永远 != total —— 这条检查恒不成立，漏的正是 W7 要防的那一类。
        # 改为：请求页长取「能装下全量」，让「返回条数 == total」成为同口径比较；
        # 服务端回显 total 字段时直接比 total（不受服务端页长上限影响，最可靠）。
        _probe_size = min(max(int(total), 10), max_page_size)
        c, fr = _call(**{page_param: 1, size_param: _probe_size,
                         "keyword": "__no_such_keyword_9x7y__",
                         "filter_field": "__no_such_value__"})
        fdata = (fr.get("data") or {}) if isinstance(fr, dict) else {}
        fitems = fdata.get(list_field)
        ftotal = fdata.get(total_field)
        # 【审查修复·结论文案】服务端拒绝未知筛选参数（result=false / 无列表 /
        # 响应非 dict）时，返回 0 条**不是**「筛选生效」的证据——那只是服务端
        # 不接受这个参数名。旧实现一律写成「筛选生效或已定义」，用一个没验证过的
        # 结论给台账盖章：真失效的接口也会被这句盖章放行。如实记不可判定。
        if (not isinstance(fr, dict) or fr.get("result") is False
                or (not isinstance(fitems, list) and not isinstance(ftotal, int))):
            _why = (fr.get("errormsg") if isinstance(fr, dict) else "") or ("HTTP %s" % c)
            findings.append(
                "筛选有效性不可判定：传入不可能命中的筛选值后服务端未返回列表（%s）——"
                "既未证明筛选生效，也未证明失效，请人工确认该接口是否支持 "
                "keyword/filter_field 筛选" % str(_why)[:80])
        elif isinstance(ftotal, int):
            # 最强证据：服务端自己报全量条数，与页长无关
            if ftotal == total:
                findings.append(
                    "筛选失效：传入不可能命中的筛选值后 total 仍为 %d（筛选参数被静默忽略）"
                    % ftotal)
                bad.append(findings[-1])
            else:
                findings.append(
                    "无效筛选值后 total=%d（≠全量 %d，筛选已生效）" % (ftotal, total))
        elif _probe_size >= total and len(fitems) == total:
            # 不回显 total 时退回条数比较：页长已能装下全量，返回条数恰等于全量
            # 即筛选被静默忽略（不可能命中的筛选值本该返回 0 条）
            findings.append(
                "筛选失效：传入不可能命中的筛选值后仍返回全部 %d 条（筛选参数被静默忽略）"
                % total)
            bad.append(findings[-1])
        elif len(fitems) == 0:
            findings.append("无效筛选值返回 0 条（筛选已生效）")
        else:
            # 0 < 条数 < 全量：既可能是筛选生效后仍有命中，也可能是服务端自行截断了
            # 页长（page_size 被压到上限）——两种解释在报文上无法区分，不下结论。
            findings.append(
                "筛选有效性不可判定：无效筛选值返回 %d 条（既非 0 也非全量 %d，"
                "无法区分「筛选仍生效」与「服务端截断了页长」）" % (len(fitems), total))
    if bad:
        return False, findings, "分页存在缺陷: " + "; ".join(bad)
    return True, findings, "分页边界、游标/Token 行为与末页行为已定义"


def check_uniqueness(action, base_payload, unique_field, duplicate_value, expect_reject=True):
    """【AC 业务唯一性】重复提交应唯一的字段，验证是否被拒绝且错误信息回显冲突值。

    历史缺陷：重复 IP 段无限制；自定义字段名重复时提示语却写成"标签名不能重复"（对象名错误）；
    邮箱重复提示 Email[] 未回显实际值。
    返回 (ok, parsed, msg)。"""
    payload = dict(base_payload)
    payload[unique_field] = duplicate_value
    code, raw, parsed = call(build_payload(action, **payload))
    result = parsed.get("result") if isinstance(parsed, dict) else None
    errmsg = str(parsed.get("errormsg", "")) if isinstance(parsed, dict) else ""

    if expect_reject:
        if result is False:
            ok, _, m = check_error_i18n(errmsg)
            if not ok:
                return True, parsed, f"唯一性校验已生效，但错误信息有问题: {m}"
            if str(duplicate_value) and str(duplicate_value) not in errmsg:
                return True, parsed, (f"唯一性校验已生效，但错误信息未回显冲突值 "
                                      f"「{duplicate_value}」（实际: {errmsg}）")
            return True, parsed, f"唯一性校验生效且错误信息回显冲突值: {errmsg}"
        return False, parsed, f"唯一性校验缺失：{unique_field}={duplicate_value} 重复提交仍成功"
    if result is True:
        return True, parsed, "允许重复，符合预期"
    return False, parsed, f"期望允许重复但被拒绝: {errmsg}"


def check_event_delivery(query_fn, trigger_info, expected_count, event_type=None,
                         max_wait=30, poll_interval=2, allow_duplicate=False):
    """【AD 事件与回调】校验事件推送：不丢失、不重复。

    query_fn: 调用方注入的查询回调，签名 query_fn(trigger_info) -> list[event]。
    返回值：实际收到的事件列表（由调用方从推送日志 / 接收端获取）。
    返回 (ok, events, msg)。

    历史缺陷：创建 8 个 webhook 只推了 1 个；并发作答 50 条日志只有 31 条（丢失）；
    全局订阅下创建问卷同时产生"创建"+"更新"两条（重复）。"""
    import time
    events = []
    waited = 0
    while waited < max_wait:
        events = query_fn(trigger_info) or []
        if event_type:
            events = [e for e in events if (e.get("event_type") or e.get("type")) == event_type]
        if len(events) >= expected_count:
            break
        time.sleep(poll_interval)
        waited += poll_interval

    msg = f"期望 {expected_count} 条，实际 {len(events)} 条（等待 {waited}s）"
    if len(events) < expected_count:
        return False, events, "事件丢失: " + msg
    if len(events) > expected_count and not allow_duplicate:
        return False, events, "事件重复推送: " + msg
    return True, events, "事件推送条数正确: " + msg


def check_ui_parity(api_value, page_value, aspect="口径", field_name="字段"):
    """【AB 前端-接口口径一致性】把页面基线作为预期值，与接口返回值比对。

    历史缺陷（占比约 8.4%）：共享文件夹问卷页面可修改、接口报"不属于当前用户"；
    时间显示与页面差 8 小时；枚举取值集合与页面下拉项不一致；接口报错根因不同、
    页面却展示同一条兜底文案（aspect="错误提示"）。
    返回 (ok, msg)。page_value 需由测试人员在页面操作后手工记录并传入——
    基线登记与三种来源（人工/浏览器自动化/设计稿）见 references/ui_baseline.md。"""
    if api_value == page_value:
        return True, f"{field_name} {aspect} 与页面一致: {api_value}"
    return False, (f"{field_name} {aspect} 与页面不一致：接口返回「{api_value}」，"
                   f"页面显示「{page_value}」（前端-接口口径不一致缺陷）")


def check_datetime_tz(iso_str, expected_offset_hours=8, field_name="时间字段"):
    """【Z6 时区一致性，国际版专项】时间字段是否按用户时区转换。

    check_timezone() 只判断偏移量字符，本函数进一步换算**时刻值**是否与页面一致：
    以页面显示值为基线，差固定小时数即为时区未转换缺陷（不要归为 ENV）。
    返回 (ok, msg)。"""
    import re
    from datetime import datetime, timedelta, timezone
    s = str(iso_str).strip()
    m = re.match(r'^(.+?)(Z|[+-]\d{2}:?\d{2})$', s)
    if not m:
        return None, f"{field_name} 无时区标记，无法判定时区转换（需确认是否为本地时间）"
    dt_str, off = m.group(1), m.group(2)
    try:
        dt = datetime.fromisoformat(dt_str.replace('Z', ''))
    except ValueError:
        return None, f"{field_name} 时间格式无法解析: {dt_str}"
    if off == 'Z':
        offset_h = 0
    else:
        sign = 1 if off[0] == '+' else -1
        parts = off[1:].replace(':', '')
        offset_h = sign * int(parts[:2])
    utc_dt = dt - timedelta(hours=offset_h)
    local_dt = utc_dt + timedelta(hours=expected_offset_hours)
    return True, (f"{field_name}: 原始 {s} → UTC {utc_dt.isoformat()} → "
                  f"UTC{expected_offset_hours:+d} {local_dt.isoformat()}（与页面比对后确认是否一致）")


def check_currency(amount, currency, expected_scale=2, max_value=None):
    """【Z8 多币种，国际版专项】校验金额精度、币种代码与取值范围。

    历史缺陷：金额精度不一致、不支持的币种被静默按默认币种处理。
    返回 (ok, problems, msg)。"""
    from decimal import Decimal
    problems = []
    try:
        d = Decimal(str(amount))
    except Exception:
        return False, ["金额无法解析为数字"], f"金额非法: {amount}"
    exp = d.as_tuple().exponent
    scale = -exp if isinstance(exp, int) and exp < 0 else 0
    if scale != expected_scale:
        problems.append(f"精度 {scale} 位，预期 {expected_scale} 位")
    if d < 0:
        problems.append("金额为负数")
    if max_value is not None and d > Decimal(str(max_value)):
        problems.append(f"金额超出上限 {max_value}")
    if not currency or len(str(currency)) != 3 or not str(currency).isalpha():
        problems.append(f"币种代码「{currency}」不符合 ISO 4217 三位字母规范")
    if problems:
        return False, problems, f"金额校验失败: {'; '.join(problems)}"
    return True, [], f"金额 {amount} {currency} 校验通过"
def run_na(case_no, name, reason, category="适用性-N/A", dimension=""):
    """记录 N/A 用例：接口不支持该功能，跳过执行但记录在案。
    N/A 不计入通过率，但必须列出证明已做适用性判断。

    注意：N/A **不算**"已执行"。覆盖率门禁只看 status 在 EXECUTED_STATUSES 的记录，
    因此这里记录没有副作用——维度不会因为"标了 N/A"就被当成覆盖到了。"""
    print(f"[{case_no}] {name} | 分类={category} | N/A | 跳过: {reason}")
    results.append({"case_no": case_no, "name": name, "category": category,
                    "action": "N/A", "http_code": None, "status": "N/A",
                    "reason": f"不适用: {reason}", "dimension": dimension,
                    "response": None})
    return None, None


# ===================== Webhook 测试模式 =====================
def webhook_trigger(action, event_type, **kwargs):
    """触发一个会产生 Webhook 事件的操作（如创建/更新/删除资源）。
    返回操作响应，供后续 webhook_verify 验证事件是否推送。"""
    p = build_payload(action, **kwargs)
    code, raw, parsed = call(p)
    return {"code": code, "parsed": parsed, "event_type": event_type, "timestamp": ts()}


def webhook_verify(trigger_info, expected_event=None, max_wait=30, poll_interval=2,
                   query_fn=None):
    """验证 Webhook 事件是否被正确推送（通用、可直接使用，不内置任何具体系统的查询逻辑）。

    通过轮询调用方注入的 `query_fn` 判断是否已收到事件，因此本函数本身是完整可用的，
    不会留下占位 / TODO。把"如何查你系统的 webhook 日志"这一与具体系统相关的部分，
    用 `query_fn` 回调交给你实现即可。

    参数：
      trigger_info : webhook_trigger() 的返回值（含 event_type 等）
      expected_event: 期望收到的事件名（默认取 trigger_info 的 event_type）
      max_wait     : 最长等待秒数
      poll_interval: 轮询间隔秒数
      query_fn     : 可调用的查询函数，返回 True 表示事件已收到；例如：
                     lambda: call_your_webhook_log_api(event_type, since=trigger_info["timestamp"])
    返回：(received: bool, detail: str)
    """
    import time as _time
    ev = expected_event or (trigger_info or {}).get("event_type")
    if query_fn is None:
        return False, "webhook_verify 未提供 query_fn：请注入你的 webhook 日志查询函数（见函数文档）"
    deadline = _time.time() + max_wait
    while _time.time() < deadline:
        try:
            received = bool(query_fn())
        except Exception as e:
            return False, f"query_fn 执行异常: {e}"
        if received:
            return True, f"Webhook 事件 {ev} 已推送"
        _time.sleep(poll_interval)
    return False, f"Webhook 事件 {ev} 在 {max_wait}s 内未收到推送（可能未触发或推送失败；请确认 query_fn 已实现）"


# ===================== 辅助验证 helper（调用其他接口交叉验证） =====================
def verify_by_query(query_action, resource_id, expected_fields, id_field="vid",
                    retries=3, retry_interval=1.0, case_tag=""):
    """调用独立详情查询接口做辅助验证。
    用途：创建/修改后，调用查询接口验证字段与预期一致（不只看主接口响应）。
    参数：
      query_action: 查询接口的 action（如 "1000109"）
      resource_id:  要查询的资源 ID
      expected_fields: 期望的字段字典，如 {"title": "新标题", "status": 1}
      id_field:     资源 ID 在查询请求中的参数名（如 vid/sid/id）
      retries:      重试次数（应对最终一致性延迟）
      retry_interval: 重试间隔（秒）
    返回 (ok, detail, msg)。ok=True 表示全部字段匹配。
    """
    import time as _time
    last_detail = None
    for attempt in range(retries):
        p = build_payload(query_action, **{id_field: str(resource_id)})
        code, raw, parsed = call(p)
        if code == -1:
            last_detail = {"error": f"连接失败: {raw[:100]}"}
            if attempt < retries - 1:
                _time.sleep(retry_interval)
                continue
            return None, last_detail, f"辅助查询接口连接失败（重试 {retries} 次），归 ENV/网络，不判主接口缺陷"
        if not isinstance(parsed, dict) or parsed.get("result") is not True:
            errmsg = str(parsed.get("errormsg", "")) if isinstance(parsed, dict) else raw[:100]
            last_detail = {"code": code, "errormsg": errmsg}
            if attempt < retries - 1:
                _time.sleep(retry_interval)
                continue
            return False, last_detail, f"辅助查询返回失败: HTTP={code}, errormsg={errmsg}"
        data = parsed.get("data") or {}
        last_detail = data
        # 逐字段断言
        mismatches = []
        for key, expected in expected_fields.items():
            actual = data.get(key)
            if actual != expected:
                mismatches.append(f"{key}: 预期={expected!r}, 实际={actual!r}")
        if not mismatches:
            return True, data, f"辅助查询验证通过，{len(expected_fields)} 个字段全部一致"
        if attempt < retries - 1:
            _time.sleep(retry_interval)
            continue
        return False, data, f"辅助查询字段不一致: {'; '.join(mismatches)}"
    return False, last_detail, "辅助验证重试耗尽"


def verify_by_list(list_action, resource_id, expected_present=True,
                   list_field="list", id_field="vid",
                   extra_params=None, retries=3, retry_interval=1.0):
    """调用独立列表查询接口做辅助验证。
    用途：创建后验证资源出现在列表中；删除后验证资源从列表消失。
    参数：
      list_action:    列表接口的 action
      resource_id:    要查找的资源 ID
      expected_present: True=期望在列表中，False=期望不在列表中
      list_field:     列表数据在响应 data 中的字段名（如 list/items/data）
      id_field:       资源 ID 在列表项中的字段名
      extra_params:   列表接口额外参数（如分页、筛选）
    返回 (ok, found, msg)。
    """
    import time as _time
    for attempt in range(retries):
        params = dict(extra_params or {})
        p = build_payload(list_action, **params)
        code, raw, parsed = call(p)
        if code == -1:
            if attempt < retries - 1:
                _time.sleep(retry_interval)
                continue
            return None, False, "辅助列表接口连接失败，归 ENV/网络"
        if not isinstance(parsed, dict) or parsed.get("result") is not True:
            return None, False, f"辅助列表返回失败: {raw[:100]}"
        data = parsed.get("data") or {}
        items = data.get(list_field) or data.get("list") or []
        if isinstance(items, dict):
            items = items.get("list") or items.get("items") or []
        found = any(str(item.get(id_field)) == str(resource_id) for item in items if isinstance(item, dict))
        if expected_present and found:
            return True, True, f"辅助列表验证通过：资源 {resource_id} 出现在列表中"
        if not expected_present and not found:
            return True, False, f"辅助列表验证通过：资源 {resource_id} 已从列表消失"
        if attempt < retries - 1:
            _time.sleep(retry_interval)
            continue
        if expected_present:
            return False, False, f"辅助列表验证失败：期望资源 {resource_id} 在列表中，但未找到（列表共 {len(items)} 条）"
        else:
            return False, True, f"辅助列表验证失败：期望资源 {resource_id} 已消失，但仍在列表中"
    return False, False, "辅助列表验证重试耗尽"


def verify_absent(query_action, resource_id, id_field="vid",
                  expected_errormsg_keywords=None, retries=3, retry_interval=1.0):
    """验证资源已不存在（删除/彻底删除后的辅助验证）。
    用途：删除后调用详情查询，期望返回失败（资源不存在）。
    参数：
      expected_errormsg_keywords: 期望错误信息包含的关键词列表（如 ["不存在", "已删除"]）
    返回 (ok, msg)。
    """
    import time as _time
    for attempt in range(retries):
        p = build_payload(query_action, **{id_field: str(resource_id)})
        code, raw, parsed = call(p)
        if code == -1:
            if attempt < retries - 1:
                _time.sleep(retry_interval)
                continue
            return None, "辅助查询连接失败，归 ENV/网络"
        if isinstance(parsed, dict) and parsed.get("result") is False:
            errmsg = str(parsed.get("errormsg", ""))
            if expected_errormsg_keywords:
                if all(k in errmsg for k in expected_errormsg_keywords):
                    return True, f"资源已不存在，错误信息符合预期: {errmsg}"
                return False, f"资源已不存在但错误语义不符，期望含 {expected_errormsg_keywords}，实际: {errmsg}"
            return True, f"资源已不存在: {errmsg}"
        if isinstance(parsed, dict) and parsed.get("result") is True:
            # 资源还在，可能是软删除标记为已删除状态
            data = parsed.get("data") or {}
            status = data.get("status")
            if status in ("deleted", "已删除", 3, -1):
                return True, f"资源标记为已删除状态(status={status})"
            if attempt < retries - 1:
                _time.sleep(retry_interval)
                continue
            return False, f"资源仍存在且未标记删除，查询返回成功（删除可能未生效）"
        if attempt < retries - 1:
            _time.sleep(retry_interval)
    return False, "验证资源不存在重试耗尽"


def verify_count_delta(count_action, expected_delta, count_field="total",
                       extra_params=None, baseline=None):
    """验证统计/计数接口的数量变化。
    用途：创建后期望 +1，删除后期望 -1。
    参数：
      count_action:   计数/统计接口的 action
      expected_delta: 期望变化量（如 +1, -1）
      count_field:    计数字段在响应 data 中的名称
      baseline:       操作前的基准数量（若不提供则调用一次获取）
    返回 (ok, before, after, msg)。
    """
    def get_count():
        params = dict(extra_params or {})
        p = build_payload(count_action, **params)
        code, raw, parsed = call(p)
        if code == -1 or not isinstance(parsed, dict) or parsed.get("result") is not True:
            return None
        data = parsed.get("data") or {}
        # 【W17】合法计数 0 是假值，`or` 链会把 0 跳成 None → 「删除到空」场景
        # 的计数交叉验证被判「无法获取基准，归 ENV」。显式判 None，0 是合法计数。
        for k in (count_field, "count", "total"):
            if isinstance(data, dict) and data.get(k) is not None:
                return data.get(k)
        return None
    if baseline is None:
        baseline = get_count()
    if baseline is None:
        return None, None, None, "无法获取基准数量（计数接口调用失败），归 ENV"
    after = get_count()
    if after is None:
        return None, baseline, None, "无法获取操作后数量（计数接口调用失败），归 ENV"
    actual_delta = after - baseline
    if actual_delta == expected_delta:
        return True, baseline, after, f"计数验证通过: {baseline} → {after}（变化 {actual_delta:+d}，符合预期 {expected_delta:+d}）"
    return False, baseline, after, f"计数验证失败: {baseline} → {after}（变化 {actual_delta:+d}，预期 {expected_delta:+d}）"


def chain_verify(create_action, create_params, query_action, expected_fields,
                 id_field="vid", query_id_field=None):
    """串联验证模式：创建 → 辅助查询验证字段一致。
    这是最常用的辅助验证模式，封装了"创建成功后立即回读"的完整流程。
    返回 (created_id, verify_ok, verify_msg)。
    """
    qid_field = query_id_field or id_field
    parsed, created_id = run("CHAIN-CREATE", f"串联创建({create_action})", create_action,
                             True, "辅助验证-串联", contract=True, **create_params)
    if not created_id:
        return None, False, "创建失败或未返回资源 ID，无法进行辅助验证"
    ok, detail, msg = verify_by_query(query_action, created_id, expected_fields,
                                      id_field=qid_field)
    if not ok:
        add_issue(f"[CHAIN] 创建后辅助查询验证失败: {msg}",
                  {"create_action": create_action, "create_params": create_params,
                   "query_action": query_action, "resource_id": created_id},
                  fid="CHAIN", expected=f"字段一致: {expected_fields}", actual=msg, severity="中")
    return created_id, ok, msg


def teardown(delete_action, resource_ids, id_field="vid", ignore_errors=True):
    """测试数据清理：批量删除测试中创建的资源。
    用途：在用例执行完毕后清理测试数据，避免污染环境。
    参数：
      delete_action: 删除接口的 action
      resource_ids:  要删除的资源 ID 列表
      id_field:      资源 ID 在删除请求中的参数名
      ignore_errors: 删除失败时是否继续（True=记录但不中断，False=遇到错误即停止）
    返回 (deleted_count, failed)。
    """
    deleted = 0
    failed = []
    for rid in resource_ids:
        if rid is None:
            continue
        p = build_payload(delete_action, **{id_field: str(rid)})
        code, raw, parsed = call(p)
        if isinstance(parsed, dict) and parsed.get("result") is True:
            deleted += 1
        else:
            err = str(parsed.get("errormsg", raw[:80])) if isinstance(parsed, dict) else raw[:80]
            failed.append({"id": rid, "error": err})
            if not ignore_errors:
                break
    print(f"[teardown] 清理完成: 成功删除 {deleted}/{len(resource_ids)} 个资源")
    if failed:
        for f in failed:
            print(f"  失败: id={f['id']}, error={f['error']}")
    return deleted, failed


# ---------------------------------------------------------------------------
# 清理登记表（崩溃安全）：register_cleanup 立即落盘，进程被杀后仍可用
# `python api_test_runner.py --cleanup-only` 补跑清理，避免孤儿数据。
# ---------------------------------------------------------------------------
CLEANUP_MANIFEST_PATH = "cleanup_manifest.jsonl"


def register_cleanup(delete_action, resource_id, id_field="vid"):
    """登记一条待清理资源并立即写入清理清单文件（崩溃后可补清理）。

    与 teardown() 的区别：teardown 立即执行删除；register_cleanup 只登记，
    由本轮结束时的 run_cleanup_manifest() 统一执行——即使进程中途被杀，
    已登记的资源仍可通过 --cleanup-only 补删。
    """
    entry = {"action": delete_action, "id": str(resource_id), "id_field": id_field}
    with io.open(CLEANUP_MANIFEST_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def run_cleanup_manifest(manifest_path=None):
    """执行清理清单中的全部待删资源；成功的条目从清单移除，失败的保留待重试。
    返回 (deleted_count, failed_list)。"""
    path = manifest_path or CLEANUP_MANIFEST_PATH
    if not os.path.exists(path):
        print(f"[cleanup] 清理清单不存在（{path}），无需清理")
        return 0, []
    entries, failed = [], []
    with io.open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    print(f"  [cleanup] 跳过损坏行: {line[:60]}")
    deleted = 0
    remaining = []
    for e in entries:
        p = build_payload(e["action"], **{e["id_field"]: e["id"]})
        code, raw, parsed = call(p)
        if isinstance(parsed, dict) and parsed.get("result") is True:
            deleted += 1
        else:
            err = str(parsed.get("errormsg", raw[:80])) if isinstance(parsed, dict) else raw[:80]
            failed.append({"id": e["id"], "error": err})
            remaining.append(e)
    # 重写清单：仅保留失败条目
    with io.open(path, "w", encoding="utf-8") as f:
        for e in remaining:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"[cleanup] 清理完成: 成功 {deleted}/{len(entries)}，失败 {len(failed)}（保留在清单中待重试）")
    for fitem in failed:
        print(f"  失败: id={fitem['id']}, error={fitem['error']}")
    return deleted, failed


def check_error_code_mapping(parsed, code_message_map, code_key="errorcode", msg_key="errormsg"):
    """断言业务错误码与错误文案的映射一致性（framework H9）。
    code_message_map: {错误码字符串: 文案中必须出现的关键短语}
    校验：① 返回的错误码在映射表中（未声明的码记缺陷）；② 实际文案包含该码的关键短语。
    返回 (ok, msg)。
    """
    if not isinstance(parsed, dict):
        return True, "非 dict 响应，跳过错误码校验"
    code = parsed.get(code_key)
    msg = str(parsed.get(msg_key, "") or "")
    if code is None or code == "" or parsed.get("result") is True:
        return True, "成功响应或无错误码，跳过"
    code = str(code)
    if code not in code_message_map:
        return False, f"错误码 {code} 未在文档/映射表中声明（实际文案: {msg[:60]}）"
    expected = str(code_message_map[code])
    if expected.lower() not in msg.lower():
        return False, f"错误码 {code} 文案映射不一致：期望含『{expected}』，实际『{msg[:60]}』"
    return True, f"错误码 {code} 文案映射一致"


# 控制参数白名单之外，任何"看起来像测试框架参数"的键都不许进入请求报文。
# 历史缺陷：run() 未声明 dimension，dimension 落进 **kwargs 后被 build_payload 拼进真实请求，
# 既污染了发给服务端的报文，又在结果记录处抛 NameError 使整条深度执行链崩溃。
_CONTROL_KEYS = ("dimension", "category", "applicability", "contract", "err_exact",
                 "expect_success", "case_no", "name", "action")


def _strip_control_keys(kwargs):
    leaked = [k for k in _CONTROL_KEYS if k in kwargs]
    if leaked:
        raise TypeError(
            "以下键是测试框架控制参数，不允许进入请求报文，请检查调用签名：%s" % ", ".join(leaked))
    return kwargs


def run(case_no, name, action, expect_success, category,
        contract=False, err_exact=None, applicability="适用", dimension="",
        tolerate_reject=None, check_mode=None, **kwargs):
    """执行单个用例，记录结果并收集问题/观察。
    err_exact: 异常用例期望命中的错误关键词列表；不命中则判 FAIL 并记问题。
    dimension: 用例所属维度编号，写入结果供覆盖率门禁统计——**不是请求参数**。
    check_mode: 深度校验标识（case_runner.CHECKS 注册的检查名，如 cross_list/pagination）。
    带 check_mode 的用例不计入 shallow_passes（其断言深度由 check 函数提供）——
    命名参数保证它不会像普通 kwargs 一样泄漏进请求报文（【审查修复】此前
    exec_case 不传、kwargs 形态又会进 payload，两头都不对）。
    【审查修复·参数名撞名】此前该控制参数叫 `check`：它与业务字段名 `check`
    撞名——业务真有 check 字段时，调用方 `run(..., **{"check": 1})` 会命中
    命名参数而非进 kwargs，直接 TypeError「got multiple values for keyword
    argument 'check'」把整条执行链崩掉（业务字段名是不可控的，测试框架不该
    占用它）。改名 check_mode，同步更新全部调用方（case_runner.exec_case）。
    注意方向：expect_success=True 表示"合法输入应成功"；expect_success=False 表示"用例本身
    就是在测'应被拒绝'的场景"。**绝不要把非法输入配成 expect_success=True**——
    那会把"接口没做校验"误判成"测试通过"。参数校验类用例请改用 assert_validation()。"""
    _strip_control_keys(kwargs)
    payload = build_payload(action, **kwargs)
    code, raw, parsed = call(payload, retries=RETRY_COUNT)
    result = parsed.get("result") if isinstance(parsed, dict) else None
    errmsg = str(parsed.get("errormsg", "")) if isinstance(parsed, dict) else ""

    # 本次请求的采集事实（HTTP 状态码 + 响应头里的请求ID/TraceID）自动接进台账。
    # 用局部包装而非在各分支逐一传参：code 只在这里拿得到，散着传必然漏，
    # 而漏掉的表现恰好是"那几列恒为空"——不报错的静默缺失。
    _ev = auto_evidence(code)

    def add_issue(*a, **kw):        # noqa: N807 局部遮蔽，仅在本函数内补采集字段
        kw.setdefault("http_code", _ev["http_code"])
        return globals()["add_issue"](*a, **kw)

    def add_observation(*a, **kw):  # noqa: N807 同上
        kw.setdefault("http_code", _ev["http_code"])
        return globals()["add_observation"](*a, **kw)

    # HTTP 5xx 一律记问题
    if code >= 500:
        add_issue(f"[{case_no}]{name} 服务端返回 {code}", payload, fid=case_no,
                  expected="HTTP 2xx/4xx 业务错误", actual=f"HTTP {code}")

    if code == -1:
        status, reason = "ERROR", f"连接/网络异常(重试后仍失败): {raw[:120]}"
        add_observation(f"[{case_no}]{name} 连接/网络异常(重试后仍失败)，归 ENV/网络问题",
                        payload, fid=case_no, expected="网络可达，正常返回", actual=raw[:120])
    elif parsed is None:
        status, reason = "FAIL", f"响应非JSON: {raw[:120]}"
        add_issue(f"[{case_no}]{name} 响应不是合法JSON", payload, fid=case_no,
                  expected="合法JSON响应", actual=raw[:120])
    elif result is True and expect_success:
        status, reason = "PASS", "result=true"
        if contract:
            ok, msg = check_contract(parsed.get("data") or {})
            if not ok:
                status = "FAIL"
                reason = f"契约校验不通过: {msg}"
                add_issue(f"[{case_no}]{name} 响应契约不通过: {msg}", payload, fid=case_no,
                          expected="契约字段与类型齐全", actual=msg)
    elif result is True and not expect_success:
        status, reason = "FAIL", "期望失败却返回成功（校验未生效）"
        add_issue(f"[{case_no}]{name} 期望校验失败但实际成功（参数校验缺失）", payload, fid=case_no,
                  expected="result=false（被校验拦截）", actual=f"result=true（{errmsg}）")
    elif result is False and not expect_success:
        if err_exact:
            if all(k in errmsg for k in err_exact):
                status, reason = "PASS", f"result=false 且命中预期错误({errmsg})"
            else:
                status, reason = "FAIL", f"错误语义不准确，期望包含{err_exact}，实际: {errmsg}"
                add_issue(f"[{case_no}]{name} 错误语义不准确：期望包含{err_exact}，实际「{errmsg}」",
                          payload, fid=case_no, expected=f"错误含{err_exact}", actual=errmsg)
        else:
            # 【反假通过 W1】未断言具体错误文案时，不得把任何 result=false 直接判 PASS：
            # 「未登录/缺 sign/网关拒绝」导致的失败与「目标校验生效」在报文上无法区分，
            # 默认放行会让校验缺失类真缺陷静默溜走。降级为「待确认」高危观察：
            # 进 findings 留痕（可人工翻案），但不计 PASS。
            # 【二轮审查口径澄清】该维度是否被门禁判「已执行」由 coverage_gate 的
            # kind/status 过滤决定（kind=待确认 不补计维度）——W1 的承诺是
            # 「留痕可翻案」，不是「观察算已执行」。两条注释此前口径矛盾，以本条为准。
            status = "待确认"
            reason = (f"result=false({errmsg})，但未断言具体错误文案（err_exact 缺失）——"
                      f"无法区分「目标校验生效」与「未登录/缺参数等其它拒绝原因」，需人工确认")
            # 【二轮审查】展开 auto_evidence 全量（含 request_id/trace_id）——
            # W1 的目标就是"可人工翻案"，翻案正需要这两列；只传 http_code 会让
            # 待确认行的采集列恒空，与同函数 add_issue 路径证据链不一致。
            add_finding(fid=str(case_no), kind="待确认",
                        desc=f"[{case_no}]{name} 异常用例被拒绝，但未断言具体错误文案，"
                             f"拒绝原因可能与测试意图无关：{errmsg}",
                        payload=payload,
                        expected="拒绝文案指向测试意图的目标字段名，或补 err_exact 后重跑",
                        actual=f"拒绝文案：{errmsg}",
                        severity="高", dimension=dimension, **_ev)
    elif result is False and expect_success:
        # 关键陷阱 3：安全拦截 ≠ 缺陷。带 tolerate_reject 的用例（如注入特殊字符探渲染降级）
        # 被内容安全策略拦下时按「符合预期」结算，避免把策略生效误报成接口缺陷。
        if tolerate_reject and any(str(k) in errmsg for k in tolerate_reject):
            status, reason = "PASS", f"被预期内策略拒绝，符合预期（{errmsg}）"
        else:
            # 【前置条件未满足 ≠ 缺陷】命中**已确认**的运行实证依赖时单列点名：
            # 既不计失败也不计通过，且不进缺陷台账（记 observation 而非 issue）。
            # 只有这条分支做前置判定——"前置未满足"只会表现为"本该成功的失败了"，
            # 不会出现在"期望被拒绝"的用例上，在别处判等于扩大误判面。
            att = _ATTR.attribute(response=parsed, action=action, assertion_ok=False,
                                  confirmed_deps=CONFIRMED_PRECONDITION_DEPS)
            if att.get("category") == _ATTR.PRECONDITION_UNMET:
                status = _ATTR.PRECONDITION_UNMET
                reason = att.get("reason") or "前置条件未满足"
                # 【W39b】命中判据点名（命中的依赖与子串），让「这条失败为何没进台账」可翻案。
                _m = att.get("matched") if isinstance(att.get("matched"), dict) else {}
                _mtag = ""
                if _m:
                    _mtag = "［命中依赖 target=%s，判据 %s］" % (
                        _m.get("target_action", ""), _m.get("match_reason", ""))
                add_observation(
                    "[{}]{} 前置条件未满足（不计缺陷）：{}{}".format(
                        case_no, name, reason, _mtag),
                    payload, fid=case_no, expected="先满足前置条件再重跑该用例",
                    actual=f"被拒绝：{errmsg}", dimension=dimension,
                    attribution=("%s:先调 %s" % (att.get("category"), att.get("evidence_action"))
                                 if att.get("evidence_action") else att.get("category")))
            else:
                status, reason = "FAIL", f"期望成功却失败: {errmsg}"
                add_issue(f"[{case_no}]{name}：本应成功，但实际失败：{errmsg}", payload, fid=case_no,
                          expected="成功", actual=f"失败：{errmsg}",
                          attribution=att.get("category"))
    else:
        status, reason = "FAIL", f"未知result值: {result}"
        add_issue(f"[{case_no}]{name} 返回未知result值: {result}", payload, fid=case_no,
                  expected="result 为 true/false", actual=f"result={result}")

    vid = None
    if status == "PASS" and isinstance(parsed, dict):
        vid = (parsed.get("data") or {}).get("vid")

    # 【浅用例检测】仅断言 result=true、未做深度断言（contract/增强断言），是"报告看似完整但覆盖浅"的主因。
    # 不强制失败（合法 happy-path 也可浅），但记低危观察并在汇总告警，避免静默掩盖弱覆盖。
    # 【W51 反假告警】深度校验（cross_list/pagination/count_delta/trace_id 等
    # case_runner.CHECKS 注册过的 check）的用例**不是浅用例**——它们只是不挂
    # contract 标志，断言深度由 check 函数提供。旧判定把这类用例也计入
    # shallow_passes，汇总里「浅用例 N 条」大部分是假告警。kw 里有 check 的跳过。
    # 【W51 反假告警】check 已升为 run() 的命名参数（exec_case 透传），深度校验
    # 用例（cross_list/pagination/count_delta/trace_id 等）不计浅用例——断言深度
    # 由 check 函数提供；命名参数形态保证不进请求报文也不进 kwargs。
    _deep_check = bool(check_mode)
    if status == "PASS" and expect_success and not contract and not _deep_check:
        global shallow_passes
        shallow_passes += 1
        add_observation(f"[{case_no}]{name} 浅用例：仅断言 result=true，未校验契约字段或追加增强断言",
                        payload, fid=case_no, expected="建议 contract=True 或追加增强断言",
                        actual="仅 result=true", severity="低")

    print(f"[{case_no}] {name} | 分类={category} | HTTP={code} | {status}")
    print(f"        请求: {json.dumps(payload, ensure_ascii=False)}")
    print(f"        响应: {raw[:260]}")
    if status == "PASS":
        print(f"        说明: {reason}")
    print()

    results.append({"case_no": case_no, "name": name, "category": category,
                    "action": action, "http_code": code, "status": status,
                    "reason": reason, "applicability": applicability, "dimension": dimension,
                    "response": parsed if parsed is not None else raw})
    return parsed, vid


def assert_validation(case_no, name, action, err_exact=None, applicability="适用", dimension="", **kwargs):
    """【参数校验专用】断言"非法/缺失/类型错误/枚举越界 输入 → 应被【拒绝】"。

    B 维度（参数校验）的标准用法，专为避免"把非法输入断言成应当成功"的盲区而封装：
      - 接口【拒绝】(result=false)  → 未断言错误文案时不能直接放行
            · 给了 err_exact 且命中  → PASS，且错误语义具体（最佳）
            · 给了 err_exact 未命中  → FAIL 且记【缺陷】：错误语义与测试意图不符（与 run() 同口径）
            · 未给 err_exact         → 待确认（高危）：无法区分目标校验生效与其它拒绝原因
      - 接口【接受】(result=true)   → FAIL 且记【缺陷(参数校验缺失)】：非法/缺失输入不应被接受

    ⚠️ 不要用 run(..., expect_success=True) 测非法输入——那会把"接口没做校验"误判成"测试通过"。
       参数校验一律用本函数（语义固定为"期望被拒绝"），方向不可能写反。

    参数：
      err_exact: 期望命中的错误关键词列表（如 ["参数不足","include_sub_account"]）；可选，
                 用于进一步校验错误语义是否具体（H 维度）。不命中不算拒绝失败，仅记观察。
    返回 (parsed, None)。
    """
    _strip_control_keys(kwargs)
    payload = build_payload(action, **kwargs)
    code, raw, parsed = call(payload, retries=RETRY_COUNT)
    result = parsed.get("result") if isinstance(parsed, dict) else None
    errmsg = str(parsed.get("errormsg", "")) if isinstance(parsed, dict) else ""

    # 同run()：把本次请求的采集事实自动接进台账，避免这两列恒为空
    _ev = auto_evidence(code)

    def add_issue(*a, **kw):        # noqa: N807 局部遮蔽
        kw.setdefault("http_code", _ev["http_code"])
        return globals()["add_issue"](*a, **kw)

    def add_observation(*a, **kw):  # noqa: N807 局部遮蔽
        kw.setdefault("http_code", _ev["http_code"])
        return globals()["add_observation"](*a, **kw)

    # HTTP 5xx 一律记问题
    if code >= 500:
        add_issue(f"[{case_no}]{name} 服务端返回 {code}", payload, fid=case_no,
                  expected="HTTP 2xx/4xx 业务错误", actual=f"HTTP {code}")

    if code == -1:
        status, reason = "ERROR", f"连接/网络异常(重试后仍失败): {raw[:120]}"
        add_observation(f"[{case_no}]{name} 连接/网络异常(重试后仍失败)，归 ENV/网络问题",
                        payload, fid=case_no, expected="网络可达，正常返回", actual=raw[:120])
    elif result is True:
        # 关键：非法输入被接受 = 参数校验缺失 缺陷（绝不是"测试通过"）
        status, reason = "FAIL", "非法/缺失输入被接受（参数校验未生效）"
        add_issue(f"[{case_no}]{name} 非法/缺失参数未被拒绝（校验缺失）", payload, fid=case_no,
                  expected="result=false（被校验拦截）", actual=f"result=true（{errmsg}）",
                  severity="中")
    elif result is False:
        # 已拒绝 —— 校验方向正确
        if err_exact:
            if all(k in errmsg for k in err_exact):
                status, reason = "PASS", f"已拒绝且错误语义具体({errmsg})"
            else:
                # 【反假通过 W2】与 run() 口径统一：配置了 err_exact 却未命中，说明
                # 错误文案与测试意图不符（错对象名/占位未渲染/根因被掩盖）——这是真实
                # 缺陷，不得降级成 PASS + 低危观察。run() 同场景判 FAIL，这里同样判。
                status, reason = "FAIL", f"错误语义不具体: 期望含{err_exact}, 实际「{errmsg}」"
                add_issue(f"[{case_no}]{name} 已拒绝但错误语义不具体(H2)：期望含{err_exact}，实际「{errmsg}」",
                          payload, fid=case_no, expected=f"错误含{err_exact}", actual=errmsg,
                          severity="中")
        else:
            # 【反假通过 W1 同源】无 err_exact 的拒绝不判 PASS：与 run() 一致降级「待确认」。
            status = "待确认"
            reason = (f"已拒绝({errmsg})，但未断言具体错误文案——拒绝原因可能与测试意图无关，需人工确认")
            add_finding(fid=str(case_no), kind="待确认",
                        desc=f"[{case_no}]{name} 参数校验用例被拒绝，但未断言具体错误文案，"
                             f"拒绝原因可能与测试意图无关：{errmsg}",
                        payload=payload,
                        expected="拒绝文案指向测试意图的目标字段，或补 err_exact 后重跑",
                        actual=f"拒绝文案：{errmsg}",
                        severity="高", dimension=dimension, **_ev)
    else:
        status, reason = "FAIL", f"未知result值: {result}"
        add_issue(f"[{case_no}]{name} 返回未知result值: {result}", payload, fid=case_no,
                  expected="result 为 true/false", actual=f"result={result}")

    print(f"[{case_no}] {name} | 分类=参数校验 | HTTP={code} | {status}")
    print(f"        请求: {json.dumps(payload, ensure_ascii=False)}")
    print(f"        响应: {raw[:260]}")
    if status == "PASS":
        print(f"        说明: {reason}")
    print()

    results.append({"case_no": case_no, "name": name, "category": "参数校验",
                    "action": action, "http_code": code, "status": status,
                    "reason": reason, "applicability": applicability, "dimension": dimension,
                    "response": parsed if parsed is not None else raw})
    return parsed, None


# ===================== 覆盖率门禁（反静默缺漏） =====================
def coverage_gate():
    """【防呆·反静默缺漏】声明了 COVERAGE_PLAN 后，任一适用维度无任何用例/问题记录即告警（可拦截）。

    解决"做少了却静默产出一份看起来完整的报告"：运行器是模板，用例由人填，没有代码校验
    "你是不是漏了适用的 C/E/AA 等维度"。本门禁在收尾时比对「声明覆盖维度」vs「实际执行触及维度」，
    缺口即记【缺陷(覆盖率缺口)】并在汇总里高亮；设 WJX_STRICT_COVERAGE=1 时直接 exit(2)。

    未声明 COVERAGE_PLAN（默认）则跳过，仅提示建议——模板不强制，但分享前建议声明以启用反漏测。
    实际触及维度 = 各**已执行**用例 result.dimension ∪ 各问题记录 finding.dimension（一个维度被任一 PASS/FAIL/PRECONDITION_UNMET 用例或问题覆盖即算触及；SKIPPED/N/A 只进 touched_only、被标「假覆盖」，不算触及）。
    """
    global coverage_blocked
    if not COVERAGE_PLAN:
        print("\n[覆盖率门禁] 未声明 COVERAGE_PLAN，跳过反静默缺漏检查。"
              "建议在 main() 声明本次要覆盖的维度编号（如 COVERAGE_PLAN=[\"A\",\"B\",\"C\",\"AA\",\"AB\",\"AC\",\"H\"]）以启用。")
        return
    # 关键：只有真正执行过的用例才算"触及维度"。被跳过/标 N/A 的用例虽然带维度编号，
    # 但没有产生任何断言结果——把它们算作覆盖会把"一条没跑"伪装成"已覆盖"（假绿）。
    executed, touched_only = set(), set()
    for r in results:
        d = r.get("dimension")
        if not d:
            continue
        toks = [t.strip() for t in str(d).split(",") if t.strip()]
        if str(r.get("status") or "").upper() in EXECUTED_STATUSES:
            executed.update(toks)
        else:
            touched_only.update(toks)
    for f in findings:
        # 覆盖率缺口本身不算"触及维度"——否则门禁会用自己的缺口结论证明自己没问题。
        if _is_coverage_gap(f):
            continue
        # 【W9 反虚高】findings 里只有**真实跑出问题的记录**才补计维度。
        # add_observation 的产出（kind=待确认）包含大量"缺配置未执行"的留痕
        # （_need_config / 各类"未执行"观察），它们带 dimension 但描述的是"没跑成"——
        # 计入 executed 等于让"缺配置没跑"冒充"已测过"。因此只有 add_issue 产出的
        # kind=缺陷（真缺陷，必然来自真实执行）补计维度；"未执行"字样的观察一律排除，
        # 与 _need_config 注释、case-coverage.md 口径对齐。
        #
        # 【审查修复】补上 kind=待确认 这一类。W1 的「无 err_exact 的拒绝不判 PASS，降级
        # 待确认高危观察」产出的正是这种记录：用例**确实发了请求、拿到了响应**，只是
        # 断言不明确（无法区分目标校验生效与其它拒绝原因）。此前门禁只认 kind=缺陷，
        # 于是一个只由这类用例构成的维度会被报成覆盖率缺口（severity=高，
        # WJX_STRICT_COVERAGE=1 时直接 exit 2）——即「W1 修好的假通过变成了假缺口」。
        # 为什么不能把「待确认」加进 EXECUTED_STATUSES：那个 kind 同时表示「跑过但断言
        # 不明」与「缺配置没跑」两种语义（见上），加进去会让后者冒充已测过。改在这里
        # 用已有的「未执行」排除做精确分流，与 PRECONDITION_UNMET 的处理口径一致。
        kind = str(f.get("kind") or "").strip()
        if kind not in ("缺陷", "待确认"):
            continue
        if "未执行" in str(f.get("desc") or ""):
            continue
        d = f.get("dimension")
        if d:
            executed.update(t.strip() for t in str(d).split(",") if t.strip())
    planned = [str(x).strip() for x in COVERAGE_PLAN if str(x).strip()]
    covered = sorted(set(planned) & executed)
    # 两类缺口：① 完全无记录；② 有记录但全部是跳过/N/A（"假覆盖"）
    missing = [d for d in planned if d not in executed]
    skipped_only = [d for d in missing if d in touched_only]
    print("\n[覆盖率门禁] 声明覆盖 %d 个维度，实际执行 %d 个：%s"
          % (len(planned), len(covered), ", ".join(covered) or "（无）"))
    per_dim = {}
    for r in results:
        d = (r.get("dimension") or "").strip()
        if d in planned:
            per_dim.setdefault(d, {"执行": 0, "跳过": 0})
            key = "执行" if str(r.get("status") or "").upper() in EXECUTED_STATUSES else "跳过"
            per_dim[d][key] += 1
    if per_dim:
        print("  维度明细（执行/跳过）：%s"
              % "  ".join("%s=%d/%d" % (d, v["执行"], v["跳过"]) for d, v in sorted(per_dim.items())))
    if skipped_only:
        print("⚠️ 假覆盖：以下维度有用例但**全部被跳过**（未执行任何断言），不得视为已覆盖：%s"
              % ", ".join(skipped_only))
    if missing:
        coverage_blocked = True
        print("❌ 覆盖率缺口（疑似漏测）：以下声明维度无任何**已执行**用例：%s" % ", ".join(missing))
        for d in missing:
            fake = d in skipped_only
            add_issue("覆盖率缺口：维度 %s 声明覆盖但本次运行无任何用例执行（%s）"
                      % (d, "假覆盖：用例全部被跳过" if fake else "疑似漏测/静默缺漏"),
                      {"coverage_plan": planned}, fid="COV-%s" % d,
                      expected="该维度至少 1 条用例真正执行（或显式 N/A 并给出证明）",
                      actual="全部跳过（0 条执行）" if fake else "0 条",
                      severity="高", dimension=d)
        if os.environ.get("WJX_STRICT_COVERAGE") == "1":
            print("WJX_STRICT_COVERAGE=1：覆盖率缺口按硬门禁处理，exit(2)。")
            sys.exit(2)
    else:
        print("✅ 声明维度均已覆盖，无静默缺漏。")


def check_key_gate():
    """约束对账门禁：契约声明的字段约束，是否每条都有用例去兑现。

    与 coverage_gate 互补：维度门禁只能回答「边界维度有没有用例跑过」，回答不了
    「契约声明了 activity 的类型，有没有一条 wrong_type:activity 用例」。生成器漏掉
    某字段的某个约束时，该维度仍被其他用例覆盖，维度门禁看不出来——这是静默缺口。

    只出声、不进缺陷台账：约束缺的是「测试设计完整性」，不是接口缺陷。实测单接口
    可达上百条，进台账会淹没真缺陷。完整清单留在产物 check_key 字段里供审计。
    """
    rec = CHECK_KEY_MISSING or {}
    if not rec:
        return
    req = rec.get("required_keys") or []
    miss = rec.get("missing") or []
    print("\n[约束对账] 契约声明 %d 条字段约束，已兑现 %d 条，缺口 %d 条"
          % (len(req), len(req) - len(rec.get("missing_keys") or []),
             len(rec.get("missing_keys") or [])))
    if not miss:
        print("✅ 契约声明的字段约束均有用例兑现。")
        return
    print("⚠️ 以下约束契约已声明、但无任何用例兑现（静默缺失，维度门禁看不出来）：")
    for m in miss[:10]:
        print("    - %s  %s" % (m.get("key"), m.get("reason") or ""))
    if len(miss) > 10:
        print("    ... 另 %d 条，完整清单见产物 check_key.missing" % (len(miss) - 10))


# ===================== 用例集（把下面的 EXAMPLE 替换为你的真实用例） =====================
def main():
    # 个人参数校验：appid / 主账号必须已配置（来自配置或环境变量），禁止写死默认域名（如 <域名>）
    missing = []
    if not BASE_URL:
        missing.append("WJX_BASE_URL（测试地址，取自契约文档『接口地址』，或用 personal_config.json 的 base_url）")
    if not APPID:
        missing.append("WJX_APPID（当前环境 appid，或用 personal_config.json 的 appid）")
    if not CREATER_MAIN:
        missing.append("WJX_MAIN（主账号，或用 personal_config.json 的 main_account）")
    if missing:
        sys.stderr.write(
            "错误：以下个人参数未配置：\n  - " + "\n  - ".join(missing) + "\n"
            "配置方式（二选一，skill 包内不含任何个人数据）：\n"
            "  1) 复制 assets/personal_config.example.json 为 personal_config.json 填入真实值，"
            "放到 ~/.workbuddy/wjx-api-test/ 或通过 WJX_CONFIG 指定路径；\n"
            "  2) 直接导出环境变量：WJX_BASE_URL / WJX_APPID / WJX_MAIN / WJX_SUB / WJX_CORPID。\n"
        )
        sys.exit(2)

    # ===== 防呆门禁：配置矛盾拦截（启动阶段，不让错误参数静默跑完全程） =====
    _consistency = config_consistency_check(
        PERSONAL,
        cross_env_requested=bool(os.environ.get("WJX_CROSS_ENV")),
        cross_cfg=CROSS_CFG,
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

    # ============================================================================
    # ====== 以下全部为「示例用例」，接入你的接口时请整段删除并替换 ==============
    # ====== 只需改 action / 参数 / err_exact / CONTRACT_KEYS，框架逻辑不用动 ====
    # ============================================================================
    # 示例骨架覆盖：创建 → 更新 → 异常-非法输入 → 异常-边界 → 安全 → 鉴权
    #               → N/A适用性 → 增强断言 → 辅助验证(创建回读/删除验不存在等)
    # 说明：默认只在当前指定环境测试，不要求多环境对比；ENV 台账示例见下方（可选，默认可删除）。
    # ============================================================================
    # ===== 示例骨架：创建 → 更新 → 异常 → 安全 → 鉴权 =====
    # 创建（业务-核心流程，校验契约）
    _, created_vid = run("C01", "主账号创建", "1000107", True, "业务-核心功能流程",
        contract=True, creater=CREATER_MAIN, title="示例主页",
        page_type=0, publish=False, html_content=html_with_title("示例主页"))

    # 更新（业务-核心流程）
    if created_vid:
        run("U01", "正常更新", "1000108", True, "业务-核心功能流程",
            contract=True, vid=str(created_vid), title="更新后的主页",
            page_type=0, html_content=html_with_title("更新后的主页"))

    # 异常-非法输入（精确错误预期）
    run("C06", "缺必填html_content", "1000107", False, "异常-非法输入",
        err_exact=["html_content不能为空"], creater=CREATER_MAIN, title="缺内容主页",
        page_type=0, publish=False)

    # 异常-边界条件
    run("C08", "title超长(120>100)", "1000107", False, "异常-边界条件",
        err_exact=["长度不能超过100"], creater=CREATER_MAIN, title="活" * 120,
        page_type=0, publish=False, html_content=html_with_title("活" * 120))

    # ===== 参数校验（B 维度）：务必用 assert_validation —— 断言"非法输入应被拒绝" =====
    # 注意：这里每个用例都断言"应被拒绝"。若接口接受（result=true），会自动记【缺陷(参数校验缺失)】，
    # 绝不会出现"断言方向写反导致缺陷被掩盖"的问题（这是实测踩过的最隐蔽盲区）。
    # 下面示例把 page_type 当成被校验字段演示；接入你的接口时把字段/值换成你自己的契约。
    assert_validation("B01", "枚举越界(page_type=99)", "1000107",
        err_exact=["page_type"], creater=CREATER_MAIN, title="枚举越界",
        page_type=99, publish=False, html_content=html_with_title("枚举越界"), dimension="B")
    assert_validation("B02", "类型错误(page_type='abc')", "1000107",
        creater=CREATER_MAIN, title="类型错误", page_type="abc",
        publish=False, html_content=html_with_title("类型错误"), dimension="B")
    assert_validation("B03", "必填缺失(无 title)", "1000107",
        err_exact=["title"], creater=CREATER_MAIN, page_type=0,
        publish=False, html_content=html_with_title("缺title"), dimension="B")
    assert_validation("B04", "空值/空白(title='')", "1000107",
        creater=CREATER_MAIN, title="", page_type=0,
        publish=False, html_content=html_with_title(""), dimension="B")

    # 安全-注入（正确行为：被拦截 → 记「符合预期」，非缺陷）
    xss_html = html_with_title("注入测试") + "<script>alert(1)</script>"
    run("S01", "html_content注入script(应被安全过滤拦截)", "1000107", False,
        "安全-注入", err_exact=["不安全脚本或DOM注入代码"], creater=CREATER_MAIN,
        title="注入测试", page_type=0, publish=False, html_content=xss_html)
    add_finding("S01", "符合预期",
                "S01：内容里写网页脚本被安全过滤正确拦截，返回'...不安全脚本或DOM注入代码...'。"
                "经业务确认是预期安全行为，非缺陷。",
                {"action": "1000107", "html_content": xss_html[:80] + "..."},
                expected="被安全过滤拒绝", actual="result=false（符合预期）",
                severity="信息", status="符合预期", dimension="G2")

    # 鉴权-签名（反向验证：约定不校验时，错误 sign 应仍成功；若被拒则记符合预期）
    _, _, p_a1 = call(build_payload("1000107", sign="xwrongsignx",
        creater=CREATER_MAIN, title="A1签名校验", page_type=0, publish=False,
        html_content=html_with_title("A1签名校验")))
    a1_ok = isinstance(p_a1, dict) and p_a1.get("result") is True
    add_finding("A1", "符合预期" if not a1_ok else "待确认",
                "A1：错误 sign 发送创建请求，" + ("服务端正确拒绝（签名校验生效）" if not a1_ok
                 else "服务端仍创建成功，疑似签名未校验，需确认") + "。",
                {"action": "1000107", "sign": "xwrongsignx"},
                expected="错误 sign 应被拒绝", actual=f"result={a1_ok}",
                severity="信息" if not a1_ok else "高",
                status="符合预期" if not a1_ok else "待确认", dimension="A1")

    # ENV 台账示例（可选，默认单环境测试可整段删除）：仅当你确实做了多环境对比、
    # 且观察到环境行为不一致时，才把差异记在此，不擅判缺陷。
    # add_finding("ENV", "环境风险(ENV)",
    #             "ENV：示例——同一请求在本地类与预发布类环境返回不一致（一个拒、一个默认）。"
    #             "疑似环境/配置差异，需向环境负责人确认。",
    #             {"note": "多环境行为不一致"},
    #             expected="各环境行为一致或差异有文档说明", actual="两环境行为不一致",
    #             severity="中", status="环境风险(ENV)")

    # ===== 适用性判断示例：N/A 用例（接口不支持的功能，跳过但记录） =====
    # 创建资源接口不适用"导出下载"——标注 N/A，不计入通过率
    run_na("NA01", "导出下载校验（创建接口不适用）",
           "创建资源接口无下游数据，导出下载功能不适用（见 applicability_matrix.md §2 反例）")
    # 查询接口不适用"状态机"
    run_na("NA02", "状态机校验（查询接口不适用）",
           "查询接口不改变资源状态，状态机维度不适用")

    # ===== 增强断言示例（按需启用，替换为你的接口字段） =====
    # 时区校验示例：校验返回的时间字段时区偏移
    # _, detail_resp = run("D01", "获取详情", "1000109", True, "业务-核心功能流程", contract=True, vid=str(created_vid))
    # if detail_resp and isinstance(detail_resp, dict):
    #     tz_ok, tz_msg = check_timezone((detail_resp.get("data") or {}).get("create_time", ""), field_name="create_time")
    #     if tz_ok is False:
    #         add_issue(f"[D01] 时区偏移异常: {tz_msg}", {"action": "1000109"}, fid="D01", severity="中")

    # 幂等性校验示例：重复创建相同请求
    # idem_ok, idem_results, idem_msg = check_idempotent("1000107", lambda: {"creater": CREATER_MAIN, "title": "幂等测试", "page_type": 0, "publish": False, "html_content": html_with_title("幂等测试")})
    # if not idem_ok:
    #     add_observation(f"[IDEM] 幂等性存疑: {idem_msg}", {"action": "1000107"}, fid="IDEM", severity="中")

    # 字段命名校验示例
    # if detail_resp and isinstance(detail_resp, dict):
    #     naming_ok, bad_fields, naming_msg = check_field_naming(detail_resp.get("data") or {})
    #     if not naming_ok:
    #         add_issue(f"[D01] 字段命名不规范: {naming_msg}", {"action": "1000109"}, fid="D01", severity="低")

    # ===== v3.5 新增能力示例（按需启用，替换为你的接口字段） =====
    # 过度校验示例（B8）：文档标注可选/可空的字段，不传/null/空串/空数组四态都应【成功】
    # ok, msg = assert_no_over_validation("B8-1", "可选字段 background_image 不传", "1000107",
    #     creater=CREATER_MAIN, title="过度校验测试", page_type=0, publish=False,
    #     html_content=html_with_title("过度校验测试"))
    # 四态检测（读接口返回结构直接回写写接口的场景必测）：
    # base = {"creater": CREATER_MAIN, "title": "四态测试", "page_type": 0}
    # for label, ok, msg in check_null_triad("1000108", "background_image", base, vid=str(created_vid)):
    #     print(f"  空值四态 {label}: ok={ok}, msg={msg}")

    # 分页校验示例（AA，列表接口必测）：
    # pg_ok, pg_msg = check_pagination("1000109", list_key="data.list", total_key="data.total")
    # if not pg_ok:
    #     add_issue(f"[AA] 分页异常: {pg_msg}", {"action": "1000109"}, fid="AA1", severity="中", dimension="AA")

    # 错误码-文案映射校验示例（H9）：映射表来自接口文档的错误码说明
    # code_map = {"2003": "authorization token", "1001": "parameter"}
    # ok, msg = check_error_code_mapping(parsed_response, code_map)
    # if not ok:
    #     add_issue(f"[H9] {msg}", parsed_response, fid="H9-1", severity="中", dimension="H9")

    # 崩溃安全清理示例：创建资源后立即登记，进程被杀后仍可用 --cleanup-only 补删
    # if created_vid:
    #     register_cleanup("DELETE_ACTION", created_vid, id_field="vid")
    # （本轮结束时统一执行 run_cleanup_manifest()；或命令行 python api_test_runner.py --cleanup-only）

    # ===== 辅助验证示例（调用其他接口交叉验证） =====
    # 模式1：创建后调用详情查询验证字段一致
    # vid, v_ok, v_msg = chain_verify(
    #     create_action="1000107",
    #     create_params={"creater": CREATER_MAIN, "title": "辅助验证测试", "page_type": 0,
    #                    "publish": False, "html_content": html_with_title("辅助验证测试")},
    #     query_action="1000109",  # 替换为你的详情查询接口
    #     expected_fields={"title": "辅助验证测试", "page_type": 0},
    #     id_field="vid"
    # )
    # print(f"串联验证: vid={vid}, ok={v_ok}, msg={v_msg}")

    # 模式2：删除后验证资源不存在
    # if vid:
    #     run("DEL01", "删除资源", "1000110", True, "业务-核心功能流程", vid=str(vid))  # 替换为删除接口
    #     absent_ok, absent_msg = verify_absent("1000109", vid, expected_errormsg_keywords=["不存在"])
    #     if not absent_ok:
    #         add_issue(f"[DEL01] 删除后资源仍存在: {absent_msg}", {"action": "1000110", "vid": vid},
    #                   fid="DEL01", expected="资源不存在", actual=absent_msg, severity="高")

    # 模式3：修改后调用详情验证修改生效、未传字段保留
    # run("UPD01", "修改标题", "1000108", True, "业务-核心功能流程",
    #     vid=str(vid), title="修改后的标题", html_content=html_with_title("修改后的标题"))
    # q_ok, q_detail, q_msg = verify_by_query("1000109", vid,
    #     expected_fields={"title": "修改后的标题"}, id_field="vid")
    # # 额外验证未传字段保留原值（如 page_type 不应被清空）
    # if q_ok and q_detail.get("page_type") != 0:
    #     add_issue(f"[UPD01] 部分更新未保留原值: page_type 预期=0, 实际={q_detail.get('page_type')}",
    #               {"action": "1000108", "vid": vid}, fid="UPD01", severity="中")

    # 模式4：创建后调用列表验证资源出现
    # list_ok, list_found, list_msg = verify_by_list("1000111", vid, expected_present=True)  # 替换为列表接口
    # if not list_ok:
    #     add_issue(f"[C01] 创建后列表中未找到资源: {list_msg}", {"action": "1000111"}, fid="C01", severity="中")

    # ===== 测试数据清理（teardown）=====
    # 用例执行完毕后，清理测试中创建的资源，避免污染环境
    # 收集所有创建的资源 ID（示例中 created_vid、chain_verify 返回的 vid 等）
    # test_resources = [created_vid]  # 替换为你实际创建的资源 ID 列表
    # teardown("1000110", test_resources, id_field="vid")  # 替换为你的删除接口

    # ===================== 汇总 & 落盘 =====================
    print("\n" + "#" * 70)
    print("测试汇总")
    print("#" * 70)
    passed = sum(1 for r in results if r["status"] == "PASS")
    failed = sum(1 for r in results if r["status"] == "FAIL")
    errored = sum(1 for r in results if r["status"] == "ERROR")
    na = sum(1 for r in results if r["status"] == "N/A")
    print(f"{'用例':<7}{'名称':<28}{'分类':<20}{'HTTP':<5}{'状态':<6}说明")
    for r in results:
        print(f"{r['case_no']:<7}{r['name']:<28}{r['category']:<20}"
              f"{str(r['http_code']):<5}{r['status']:<6}{r['reason']}")
    print("-" * 70)
    print(f"总计 {len(results)} 项 | 通过 {passed} | 失败 {failed} | ENV/网络异常 {errored} | N/A(不适用) {na}")
    # 【W10 口径落地】PRECONDITION_UNMET 单列分栏：它计入 executed（发了请求拿到
    # 响应的宽松口径），但不计入通过率分子（严格口径）——两口径差异必须可见，
    # 否则报告读者把「被前置挡住的用例」误读成通过或失败。
    n_precond = sum(1 for r in results if r["status"] == _ATTR.PRECONDITION_UNMET)
    if n_precond:
        print(f"其中 前置条件未满足 {n_precond} 项（已计入覆盖率分母[宽松口径]，"
              f"不计入通过率[严格口径]；case_coverage 只数 PASS/FAIL，两口径差异即此）")
    n_defect = sum(1 for x in findings if x["kind"] == "缺陷")
    n_obs = sum(1 for x in findings if x["kind"] == "待确认")
    n_env = sum(1 for x in findings if x["kind"] == "环境风险")
    n_ok = sum(1 for x in findings if x["kind"] in ("已确认非问题", "符合预期"))
    print(f"问题记录合计 {len(findings)} 条 | 缺陷 {n_defect} | 待确认 {n_obs} | 环境风险 {n_env} | 符合预期/非问题 {n_ok}")

    # ===== 防呆门禁：反静默缺漏（需在写 CSV 之前，使覆盖率缺口问题进入台账） =====
    coverage_gate()
    if shallow_passes:
        print("\n⚠️ 浅用例告警：%d 个用例仅断言 result=true、未做深度断言（契约/增强断言），"
              "报告看似完整但覆盖浅，建议补充 contract=True 或增强断言。" % shallow_passes)
    # 【W41 如实声明】稳定性检测只对「多轮结果合并」生效；单轮无 flaky 不代表稳定
    if not _ATTR.detect_flaky(results):
        print("（稳定性检测：本轮为单轮结果，未做 flaky 判定——请勿据此认为用例稳定；"
              "多轮结果合并后重跑汇总才会触发检测）")

    import csv
    def write_csv(path, rows):
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(FINDING_CSV_COLS)
            for x in rows:
                w.writerow(finding_row(x))

    write_csv("api_test_issues.csv", findings)
    write_csv("api_test_observations.csv", [x for x in findings if x["kind"] == "待确认"])

    report = {
        "base_url": BASE_URL,
        "fixed_params": {"encode": ENCODE, "appid": APPID, "sign": SIGN},
        "creater": {"main": CREATER_MAIN, "sub": CREATER_SUB},
        "summary": {"total": len(results), "passed": passed, "failed": failed,
                    "errored": errored, "na": na,
                    "findings": len(findings), "缺陷": n_defect, "待确认": n_obs,
                    "环境风险": n_env, "符合预期或非问题": n_ok},
        "cases": results,
        "findings": findings,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open("api_test_report_full.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print("\n完整问题记录已写入 api_test_issues.csv")
    print("待确认项视图已写入 api_test_observations.csv")
    print("完整报告已写入 api_test_report_full.json")


if __name__ == "__main__":
    if "--cleanup-only" in sys.argv:
        # 补跑清理：仅执行清理清单中上次运行遗留的待删资源，不执行用例。
        # 用例侧须用 register_cleanup() 登记资源才会进入清单。
        run_cleanup_manifest()
    else:
        main()
