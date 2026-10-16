# -*- coding: utf-8 -*-
"""声明式用例执行器：执行 case_generator.py 产出的用例 JSON，替代手写 Python main()。

与 api_test_runner.py 的关系：本模块是**执行入口**，复用前者的全部能力——
build_payload / call / run / assert_validation / 各断言 helper / add_issue / 覆盖率门禁。

关键：自动把 cases 文件里的 coverage_plan 装进 api_test_runner.COVERAGE_PLAN，
使「反静默缺漏」门禁默认生效（不用人手声明，也就不会形同虚设）。

跳过语义（重要）：enabled=false 或 needs=manual/ui_baseline 的用例不会被静默丢弃——
会以 SKIPPED / N/A 状态记入 results 并带上维度编号，在汇总里点名，
满足「N/A 必须列出证明已判断」的要求，同时避免污染通过率。

用法：
  python case_runner.py --cases generated_cases.json
  python case_runner.py --cases generated_cases.json --dry-run     # 只列计划不发请求
  python case_runner.py --cases generated_cases.json --only B,G     # 只跑指定维度
"""
import argparse
import csv
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import api_test_runner as R  # noqa: E402
import ui_baseline as UB     # noqa: E402
import minimizer             # noqa: E402
import assertion_engine as AE  # noqa: E402
import attribution_engine      # noqa: E402

try:
    from personal_config import resolve_placeholders
except Exception:  # 独立运行兜底：无个人配置模块时保留占位符原样（由下方点名跳过）
    def resolve_placeholders(cfg, personal):
        return cfg

CSV_COLS = R.FINDING_CSV_COLS  # 台账列口径唯一来源在 api_test_runner，此处仅别名引用

# 【W34 入口防呆】配置校验只存在于 api_test_runner.main()（直接跑深度流程才触发），
# 而推荐入口 flow.py→case_runner / case_runner.main 都经 import 调用、绕过那道校验——
# 空配置时每条 call() 返回 -1、全部判 ERROR 归 ENV、覆盖率全 skipped，
# exit 0 产出「看似跑完」的空报告。本函数在真实执行前统一调用（幂等，跑多次只报一次）。
_CONFIG_VERIFIED = False


def ensure_runnable_config():
    """BASE_URL/APPID/主账号缺失或配置矛盾时 sys.exit(2)，不让空配置静默跑完全程。

    与 api_test_runner.main() 顶部的校验同一套口径（复用其文案与 config_consistency_check），
    提前到真实请求发出之前；dry-run（不发请求）不经过这里，不受影响。
    """
    global _CONFIG_VERIFIED
    if _CONFIG_VERIFIED:
        return
    missing = []
    if not R.BASE_URL:
        missing.append("WJX_BASE_URL（测试地址，取自契约文档『接口地址』，或用 personal_config.json 的 base_url）")
    if not R.APPID:
        missing.append("WJX_APPID（当前环境 appid，或用 personal_config.json 的 appid）")
    if not R.CREATER_MAIN:
        missing.append("WJX_MAIN（主账号，或用 personal_config.json 的 main_account）")
    if missing:
        sys.stderr.write(
            "错误：以下个人参数未配置（case_runner 入口校验，与 api_test_runner.main 同口径）：\n"
            "  - " + "\n  - ".join(missing) + "\n"
            "配置方式（二选一，skill 包内不含任何个人数据）：\n"
            "  1) 复制 assets/personal_config.example.json 为 personal_config.json 填入真实值，"
            "放到 ~/.workbuddy/wjx-api-test/ 或通过 WJX_CONFIG 指定路径；\n"
            "  2) 直接导出环境变量：WJX_BASE_URL / WJX_APPID / WJX_MAIN / WJX_SUB / WJX_CORPID。\n"
            "（未配置就执行会让每条请求失败并全部归 ENV，产出一份看似完成实则什么都没测的报告。）\n")
        sys.exit(2)
    try:
        from personal_config import config_consistency_check
        consistency = config_consistency_check(
            R.PERSONAL,
            cross_env_requested=bool(os.environ.get("WJX_CROSS_ENV")),
            cross_cfg=R.CROSS_CFG,
        )
        fatal = [m for lvl, m in consistency if lvl == "fatal"]
        for m in fatal:
            sys.stderr.write("错误（配置矛盾，禁止运行）：%s\n" % m)
        if fatal:
            sys.exit(2)
        warns = [m for lvl, m in consistency if lvl == "warn"]
        if warns:
            print("⚠️ 配置一致性警告：")
            for m in warns:
                print("   - %s" % m)
    except ImportError:
        pass  # 独立兜底场景（无 personal_config 模块）：缺失校验已在上面完成
    _CONFIG_VERIFIED = True

# FAIL 后是否自动做最小复现缩减。可由执行配置 execution.minimize_findings=false 关闭
# （也可用环境变量 WJX_NO_MINIMIZE=1），供离线/只读场景使用。
MINIMIZE_ENABLED = True

# 并发 / 连发类检查的硬上限：配置写错（如 times=999）不能真的打 999 次。
# 这类检查的请求是真实发出的，上限是最后一道防线，不是可调参数。
CONCURRENCY_MAX_TIMES = 8
RATE_LIMIT_MAX_TIMES = 10

# 限流可观测信号：HTTP 429 或响应体里出现这些词，即认为限流已生效。
RATE_LIMIT_KEYWORDS = ("限流", "频率", "过于频繁", "请求过快", "请求太频繁",
                       "rate limit", "too many", "throttl")

# 终态/危险枚举语义：含这些词的枚举值不得由自动化真实提交（删数据/停用的副作用不可逆）。
_DANGEROUS_ENUM_HINTS = ("delete", "remove", "disable", "cancel",
                         "删除", "禁用", "下架", "作废", "撤销", "终止", "停用", "销毁")


def _skip(case, status, reason):
    R.results.append({
        "case_no": case.get("id", ""), "name": case.get("name", ""),
        "category": case.get("category", ""), "action": case.get("action", ""),
        "http_code": 0, "status": status, "reason": reason,
        "applicability": case.get("applicability", "适用"),
        "dimension": case.get("dimension", ""),
        "response": {"note": case.get("note", "")},
    })
    print("[%s] %s | %s | %s" % (case.get("id", ""), case.get("name", ""), status, reason))


def _skip_uncovered(case, why, suggestion):
    """记一条覆盖缺口后再跳过。

    "没测到"必须进台账。若只记 SKIPPED，当同一维度下还有别的用例跑过时，
    门禁会判定该维度"已覆盖"，这个从未执行的检查就彻底静默了。
    """
    R.add_issue("[%s] 检查未执行：%s" % (case.get("id"), why),
                case.get("params", {}), fid="COV-%s" % case.get("id"),
                expected=suggestion, actual="未执行任何断言（跳过）",
                severity="中", dimension=case.get("dimension") or "未分类")
    _skip(case, "SKIPPED", why)


def extract_resource(parsed, action):
    """从创建/更新成功的响应里提取资源标识（供收尾清理与残留清单使用）。"""
    if not isinstance(parsed, dict) or parsed.get("result") is not True:
        return None
    data = parsed.get("data")
    if not isinstance(data, dict):
        return None
    for k in ("id", "record_id", "data_id"):
        v = data.get(k)
        if isinstance(v, (int, str)) and v != "":
            return {"action": str(action), "id": v}
    return None


def _check_contract_diff(case, parsed, args):
    documented = set(args.get("documented_fields") or [])
    data = (parsed or {}).get("data") or {}
    if not isinstance(data, dict) or not documented:
        return
    actual = set(data.keys())
    missing = sorted(documented - actual)
    redundant = sorted(actual - documented)
    if missing:
        R.add_issue("[%s] 响应缺少文档声明字段：%s" % (case.get("id"), missing),
                    case.get("params", {}), fid=case.get("id"),
                    expected="文档声明字段齐全", actual="缺 %s" % missing,
                    severity="高", dimension="K")
    if redundant:
        R.add_issue("[%s] 响应返回文档未声明字段（冗余/未文档化）：%s" % (case.get("id"), redundant),
                    case.get("params", {}), fid=case.get("id"),
                    expected="无未声明字段", actual="多 %s" % redundant,
                    severity="低", dimension="K")


def _check_field_naming(case, parsed, args):
    data = (parsed or {}).get("data") or {}
    if not isinstance(data, dict) or not data:
        return
    ok, bad, msg = R.check_field_naming(data)
    if ok is False:
        R.add_issue("[%s] 字段命名风格不统一：%s" % (case.get("id"), msg),
                    case.get("params", {}), fid=case.get("id"),
                    expected="命名风格统一", actual=str(bad), severity="低", dimension="K")


def _check_url_alive(case, parsed, args):
    """C：资源链接可用性 + 类型/尺寸合理性。图片 404、类型不符、尺寸为 0
    会直接导致前端图片裂开或版面错位——这是接口侧能自动发现的最强 UI 还原代理信号之一。"""
    field = args.get("field") or ""
    ok, msg, meta = R.check_resource_url(parsed or {}, url_field=field,
                                         expect_kind=args.get("expect_kind") or "")
    if ok is False:
        R.add_issue("[%s] 响应 %s 资源不可用/不合理：%s" % (case.get("id"), field, msg),
                    case.get("params", {}), fid=case.get("id"),
                    expected="资源可访问且类型/尺寸合理", actual=msg,
                    severity="中", dimension=case.get("dimension") or "C")
    elif meta.get("warn"):
        R.add_observation("[%s] %s" % (case.get("id"), meta["warn"]), case.get("params", {}),
                          fid=case.get("id"), severity="低", dimension=case.get("dimension") or "C")


def _check_error_root_cause(case, parsed, args):
    action = case.get("action")
    ok, a_msg, b_msg = R.check_error_root_cause(
        action, args.get("payload_a") or {}, args.get("payload_b") or {},
        cause_a=args.get("cause_a", "凭证错误"), cause_b=args.get("cause_b", "参数缺失"))
    if ok is False:
        R.add_issue("[%s] 根因掩盖：两种不同成因返回相同错误信息" % case.get("id"),
                    {"a": args.get("payload_a"), "b": args.get("payload_b")}, fid=case.get("id"),
                    expected="不同成因错误信息不同", actual="A=%s / B=%s" % (a_msg, b_msg),
                    severity="中", dimension="H")


def _check_structure_complete(case, parsed, args):
    """Y：嵌套结构完整性——文档声明字段逐个存在且不为 None。"""
    fields = args.get("documented_fields") or []
    data = (parsed or {}).get("data") or {}
    if not isinstance(data, dict) or not fields:
        return
    empty = [f for f in fields if f in data and data.get(f) in (None, "", [], {})]
    if empty:
        R.add_issue("[%s] 复杂结构返回不完整（字段存在但为空）：%s" % (case.get("id"), empty),
                    case.get("params", {}), fid=case.get("id"),
                    expected="各子类型结构完整", actual="空字段 %s" % empty,
                    severity="中", dimension="Y")


def _check_name_consistency(case, parsed, args):
    req = [n for n in (args.get("request") or []) if "_" in n or n.islower()]
    resp = [n for n in (args.get("response") or []) if "_" in n or n.islower()]
    if req and resp:
        snake_req = sum(1 for n in req if "_" in n) / float(len(req))
        snake_resp = sum(1 for n in resp if "_" in n) / float(len(resp))
        if abs(snake_req - snake_resp) > 0.5:
            R.add_issue("[%s] 请求与响应命名风格不一致（请求下划线占比 %.0f%% vs 响应 %.0f%%）"
                        % (case.get("id"), snake_req * 100, snake_resp * 100),
                        case.get("params", {}), fid=case.get("id"),
                        expected="命名风格一致", actual="风格混用", severity="低", dimension="K")


def _last_result():
    return R.results[-1] if R.results else {}


def _need_config(case, check, missing, detail):
    """检查项缺少必要配置时不静默跳过：记一条明确的观察，维度不会因此被算作"已执行"。"""
    R.add_observation(
        "[%s] check=%s 未执行：缺少配置 %s（%s）"
        % (case.get("id"), check, ",".join(missing), detail),
        case.get("params", {}), fid=case.get("id"), severity="低",
        dimension=case.get("dimension") or "")


def _check_http_semantics(case, parsed, args):
    """H：HTTP 状态码语义——400/401/403/404/409/429 必须与业务语义一致（401 与 403 混用是典型缺陷）。"""
    code = _last_result().get("http_code")
    if not code:
        return
    business_result = parsed.get("result") if isinstance(parsed, dict) else None
    semantic = args.get("semantic")
    if semantic:
        # 语义断言走 assertion_engine（此前该模块仅被测试引用，从未接入真实执行链）
        ar = AE.assert_http_semantics(code, semantic)
        ok, msg = ar.ok, ar.message
    else:
        ok, msg = R.check_http_semantics(code, business_result,
                                         expected_http=args.get("expected_http"))
    dim = case.get("dimension") or "H"
    if ok is False:
        R.add_issue("[%s] HTTP 状态码语义不符：%s" % (case.get("id"), msg),
                    case.get("params", {}), fid=case.get("id"),
                    expected="HTTP 状态码与业务语义一致", actual=msg,
                    severity="中", dimension=dim)
    elif ok is None:
        R.add_observation("[%s] %s" % (case.get("id"), msg), case.get("params", {}),
                          fid=case.get("id"), severity="低", dimension=dim)


def _check_roundtrip(case, parsed, args):
    """B/C：往返完整性——写入后回读，提交值必须原样返回。

    富文本 / HTML / 特殊字符在存取过程中被转义、截断或丢样式，是前端渲染错版的高发前因；
    本检查不需要页面基线即可自动执行。

    两档语义（按 check_args 自动选择）：
      给了 read_action  → 真正往返：调读接口取回结构 → 原样回写写接口 → 被拒即过度校验缺陷；
      未给 read_action  → 退化为"写入后回读一致性"：提交值必须出现在响应 data 中且完全相同。
    """
    args = args or {}
    read_action = args.get("read_action")
    if read_action:
        _c, _r, read_parsed = R.call(R.build_payload(read_action, **(args.get("read_params") or {})))
        if not isinstance(read_parsed, dict) or read_parsed.get("result") is not True:
            R.add_observation(
                "[%s] 往返验证跳过：辅助读接口 %s 不可用（归 ENV/权限，不判主接口缺陷）"
                % (case.get("id"), read_action), case.get("params", {}), fid=case.get("id"),
                severity="低", dimension=case.get("dimension") or "B")
            return
        echoed = read_parsed.get("data") or {}
        if not isinstance(echoed, dict) or not echoed:
            return
        # 配套接口匹配性探测：读回来的结构里一个主接口提交过的字段都没有，
        # 说明这个 read_action 多半不是本接口的配套读接口（自动扫描猜错的情形）。
        # 此时点名降级为"回读响应自身"，而不是拿别人的结构去做回写——
        # 后者既验不到东西，还会把无关字段的拒绝报成主的过度校验缺陷。
        submitted = {k for k in (case.get("params") or {}) if not k.startswith("_")}
        if submitted and not (submitted & set(echoed)):
            R.add_observation(
                "[%s] 配套读接口 %s 疑似不匹配：读回结构与主接口提交字段无交集（提交 %s / 读回 %s）"
                % (case.get("id"), read_action, sorted(submitted)[:8], sorted(echoed)[:8]),
                case.get("params", {}), fid=case.get("id"),
                expected="read_action 应指向本接口的详情/查询接口",
                actual="字段无交集，已降级为「写入后回读响应自身」",
                severity="低", dimension=case.get("dimension") or "B")
            read_action = None
        else:
            allowed = args.get("writable_fields")
            write_params = {k: v for k, v in echoed.items() if not allowed or k in allowed}
            _c2, _r2, w = R.call(R.build_payload(case.get("action"), **write_params))
            if isinstance(w, dict) and w.get("result") is False:
                R.add_issue(
                    "[%s] 读接口返回结构原样回写写接口被拒绝（过度校验）：%s"
                    % (case.get("id"), str(w.get("errormsg") or "")[:160]),
                    write_params, fid=case.get("id"),
                    expected="读到的结构原样回写应成功",
                    actual="被拒绝：%s" % str(w.get("errormsg") or "")[:160],
                    severity="高", dimension=case.get("dimension") or "B")
            return
    # --- 退化路径：写入后回读响应自身（未给 read_action 或配套接口被判定为不匹配）---
    if not isinstance(parsed, dict):
        return
    data = parsed.get("data")
    if not isinstance(data, dict):
        return
    params = case.get("params") or {}
    fields = args.get("fields")
    if not fields:
        fields = [k for k in params if k in data]
    matched = [f for f in fields if f in data and f in params]
    dim = case.get("dimension") or "C"
    if not matched:
        # 空转防护：创建接口常只回显 {"id": ..}，不回显提交值——零比对零记录会伪装成 PASS
        R.add_issue(
            "[%s] 往返比对未执行：响应未回显任何目标字段 %s（无 read_action 时无法真往返）"
            % (case.get("id"), [str(f) for f in fields][:8]),
            params, fid="COV-%s" % case.get("id"),
            expected="创建/更新响应应回显提交值，或在 check_args 提供 read_action 做真往返",
            actual="响应 data 键：%s" % sorted(data)[:12], severity="中", dimension=dim)
        return
    mismatched = []
    for f in matched:
        if data[f] != params[f]:
            mismatched.append((f, params[f], data[f]))
    if mismatched:
        R.add_issue(
            "[%s] 往返不一致：写入值未被原样回读（疑似转义/截断/丢样式）：%s"
            % (case.get("id"), "; ".join("%s 提交=%r 回读=%r" % (f, a, b) for f, a, b in mismatched)),
            params, fid=case.get("id"), expected="写入值原样回读",
            actual="字段不一致 %s" % [f for f, _, _ in mismatched], severity="中", dimension=dim)


_ID_KEYS = ("vid", "id", "sid", "uid", "page_id", "survey_id", "order_id", "qid")


def _pick_resource_id(data, preferred=None):
    """从响应 data 里挑资源标识：优先显式指定字段，其次已知 id 类键，最后按后缀兜底。"""
    if not isinstance(data, dict):
        return None
    if preferred and data.get(preferred) is not None:
        return data.get(preferred)
    for k in _ID_KEYS:
        if data.get(k) is not None:
            return data.get(k)
    for k in sorted(data):
        if k.lower().endswith(("id", "_id")) and data.get(k) is not None:
            return data.get(k)
    return None


def _check_cross_list(case, parsed, args):
    """C1：交叉验证——主接口说成功不算，去独立列表接口里看资源到底在不在。

    framework §9.4.2 的核心要求：辅助接口与主接口越独立，验证越可信。
    「响应 success 但没落库 / 落错库」是只看主接口响应永远抓不到的缺陷。
    """
    list_action = args.get("list_action")
    if not list_action:
        _need_config(case, "cross_list", ["check_args.list_action"], "缺配套列表接口，无法交叉验证")
        return
    data = parsed.get("data") if isinstance(parsed, dict) else None
    rid = _pick_resource_id(data, args.get("id_field"))
    if rid is None:
        _need_config(case, "cross_list", ["响应 data 中的资源 id"],
                     "主接口响应未回显资源 id，无法到列表里定位（可用 check_args.id_field 指定）")
        return
    dim = case.get("dimension") or "C"
    ok, _found, msg = R.verify_by_list(
        list_action, rid,
        expected_present=bool(args.get("expected_present", True)),
        list_field=args.get("list_field") or "list",
        id_field=args.get("list_id_field") or args.get("id_field") or "vid",
        extra_params=args.get("extra_params"))
    if ok is False:
        R.add_issue("[%s] 交叉验证失败（列表侧）：%s" % (case.get("id"), msg),
                    case.get("params", {}), fid=case.get("id"),
                    expected="资源应出现在独立列表接口 %s 中" % list_action,
                    actual=msg, severity="高", dimension=dim)
    elif ok is None:
        R.add_observation("[%s] 交叉验证未完成（列表侧）：%s" % (case.get("id"), msg),
                          case.get("params", {}), fid=case.get("id"),
                          severity="低", dimension=dim)


# 计数基线必须在**主请求发出之前**采集：check 是在请求之后跑的，那时两次取数
# 都已经是"创建后"了，差值恒为 0，+1/-1 永远验不出来。
_COUNT_BASELINE = {}


def _read_count(count_action, count_field, extra_params=None):
    code, _raw, parsed = R.call(R.build_payload(count_action, **(extra_params or {})))
    if code == -1 or not isinstance(parsed, dict) or parsed.get("result") is not True:
        return None
    data = parsed.get("data") or {}
    if isinstance(data, dict):
        return data.get(count_field) or data.get("count") or data.get("total")
    return None


def capture_pre_baseline(case):
    """主请求执行前采集计数基线（仅对 check=count_delta 的用例生效）。"""
    if (case.get("check") or "") != "count_delta":
        return
    args = case.get("check_args") or {}
    ca = args.get("count_action")
    if not ca:
        return
    _COUNT_BASELINE[case.get("id")] = _read_count(
        ca, args.get("count_field") or "total", args.get("extra_params"))


def _check_count_delta(case, parsed, args):
    """C1：交叉验证——列表/统计接口的总数应随创建 +1、删除 -1（§9.4.2 计数行）。"""
    ca = args.get("count_action")
    if not ca:
        _need_config(case, "count_delta", ["check_args.count_action"], "缺配套统计/列表接口")
        return
    dim = case.get("dimension") or "C"
    baseline = _COUNT_BASELINE.pop(case.get("id"), None)
    ok, before, after, msg = R.verify_count_delta(
        ca, int(args.get("expected_delta", 1)),
        count_field=args.get("count_field") or "total",
        extra_params=args.get("extra_params"), baseline=baseline)
    if ok is False:
        R.add_issue("[%s] 计数交叉验证失败：%s" % (case.get("id"), msg),
                    case.get("params", {}), fid=case.get("id"),
                    expected="总数变化 %+d（基线 %s）" % (int(args.get("expected_delta", 1)), before),
                    actual="实际 %s：%s" % (after, msg), severity="高", dimension=dim)
    elif ok is None:
        R.add_observation("[%s] 计数交叉验证未完成：%s" % (case.get("id"), msg),
                          case.get("params", {}), fid=case.get("id"),
                          severity="低", dimension=dim)


_TRACE_KEYS = ("trace", "traceid", "requestid", "reqid", "logid")
_EXPORT_KEYS = ("url", "download", "downloadurl", "fileurl", "file", "token", "filetoken")


def _find_resp_key(parsed, keys, depth=0):
    """在响应里找语义字段（忽略大小写与下划线/连字符差异），返回字符串值或 None。"""
    if depth > 4 or not isinstance(parsed, dict):
        return None
    norm = [k.lower().replace("_", "").replace("-", "") for k in keys]
    for k, v in parsed.items():
        if str(k).lower().replace("_", "").replace("-", "") in norm:
            return "" if v is None else str(v)
    for v in parsed.values():
        if isinstance(v, dict):
            got = _find_resp_key(v, keys, depth + 1)
            if got is not None:
                return got
    return None


def _check_trace_id(case, parsed, args):
    """V：请求追踪——响应应携带请求标识，且逐次不同。

    契约没声明追踪字段是设计选择而非缺陷，所以识别不到时只记观察；
    硬判的只有「字段存在却为空」与「两次调用标识相同」这两种确定失效。
    """
    action = case.get("action")
    if not action:
        _need_config(case, "trace_id", ["action"], "无法确定接口")
        return
    dim = case.get("dimension") or "V"
    params = dict(case.get("params") or {})
    first = _find_resp_key(parsed, _TRACE_KEYS)
    if first is None:
        R.add_observation("[%s] 响应未包含可识别的请求追踪字段（trace/request id），跳过唯一性验证"
                          % case.get("id"), params, fid=case.get("id"),
                          severity="低", dimension=dim)
        return
    if not first.strip():
        R.add_issue("[%s] 请求追踪字段存在但为空" % case.get("id"), params, fid=case.get("id"),
                    expected="请求标识非空，可用于串联单次请求的日志",
                    actual="空值", severity="中", dimension=dim)
        return
    if not args.get("expect_distinct", True):
        R.add_observation("[%s] 响应携带请求追踪标识=%s" % (case.get("id"), first[:40]),
                          params, fid=case.get("id"), severity="低", dimension=dim)
        return
    _c, _r, parsed2 = R.call(R.build_payload(action, **params))
    second = _find_resp_key(parsed2 if isinstance(parsed2, dict) else {}, _TRACE_KEYS)
    if second is None or not str(second).strip():
        R.add_issue("[%s] 第二次调用缺失或空请求追踪标识（首次=%s）"
                    % (case.get("id"), first[:40]), params, fid=case.get("id"),
                    expected="每次调用都返回非空且唯一的请求标识",
                    actual="第二次为 %s" % ("空" if second is not None and not str(second).strip() else "缺失"),
                    severity="中", dimension=dim)
        return
    if str(second).strip() == first.strip():
        R.add_issue("[%s] 两次调用返回相同的请求追踪标识：%s（无法区分单次请求）"
                    % (case.get("id"), first[:40]), params, fid=case.get("id"),
                    expected="逐次不同，可用于定位单次请求链路",
                    actual="两次均为 %s" % first[:40], severity="中", dimension=dim)
        return
    R.add_observation("[%s] 请求追踪标识逐次不同（%s → %s），可串联日志"
                      % (case.get("id"), first[:20], str(second)[:20]),
                      params, fid=case.get("id"), severity="低", dimension=dim)


def _check_export_credential(case, parsed, args):
    """M：导出下载——成功的判据是拿到能真正下载的凭据，不是 result=true。

    同样只在能识别到下载字段时才硬判：字段名因业务而异，识别不到记观察而非缺陷。
    """
    dim = case.get("dimension") or "M"
    params = dict(case.get("params") or {})
    got = _find_resp_key(parsed, _EXPORT_KEYS)
    if got is None:
        R.add_observation("[%s] 响应未包含可识别的下载凭据字段（url/file/token），跳过校验"
                          % case.get("id"), params, fid=case.get("id"),
                          severity="低", dimension=dim)
        return
    if not got.strip():
        R.add_issue("[%s] 导出成功但下载凭据为空" % case.get("id"), params, fid=case.get("id"),
                    expected="响应给出可用的下载地址或文件 token",
                    actual="凭据字段为空", severity="高", dimension=dim)
        return
    R.add_observation("[%s] 导出返回下载凭据=%s" % (case.get("id"), got[:60]),
                      params, fid=case.get("id"), severity="低", dimension=dim)


def _check_idempotent(case, parsed, args):
    """AC：幂等——相同请求重复提交，结果应一致且不产生重复对象。

    判定二分（防误报，与 concurrency 同一口径）：
      两次结果不一致但只是"新建了不同对象" → **观察**：创建类接口本就允许多条，
        除非接口声明了幂等键（request_id/幂等号），那时才是缺陷；
      两次结果不一致且不是新建对象 → 缺陷：更新类接口重复提交结果漂移。
    """
    action = case.get("action")
    if not action:
        _need_config(case, "idempotent", ["action"], "无法确定接口")
        return
    try:
        times = int(args.get("times") or 2)
    except Exception:
        times = 2
    times = max(2, min(times, CONCURRENCY_MAX_TIMES))
    params = dict(case.get("params") or {})
    dim = case.get("dimension") or "AC"
    ok, results, msg = R.check_idempotent(action, lambda: dict(params), times=times)
    if ok is None:
        # 【W3 配套】接口全失败/响应缺 result——不具备幂等比对前提。
        # 记观察留痕（归 ENV/需重跑），不得流入「幂等良好」也不得流入 FAIL。
        R.add_observation("[%s] 幂等性无法验证：%s" % (case.get("id"), msg),
                          params, fid=case.get("id"), severity="低", dimension=dim)
        return
    if ok:
        R.add_observation("[%s] 相同请求重复 %d 次结果一致，幂等良好" % (case.get("id"), times),
                          params, fid=case.get("id"), severity="低", dimension=dim)
        return
    ids = []
    for r in results or []:
        p = r.get("parsed")
        d = p.get("data") if isinstance(p, dict) else None
        ids.append(_pick_resource_id(d, args.get("id_field")))
    distinct = sorted({str(x) for x in ids if x is not None})
    if len(distinct) > 1:
        R.add_observation(
            "[%s] 相同请求重复 %d 次创建了 %d 个不同对象（无幂等去重）：%s"
            % (case.get("id"), times, len(distinct), distinct[:5]),
            params, fid=case.get("id"),
            expected="声明了幂等键的接口应返回同一对象；未声明时本条仅作观察",
            actual="得到不同资源 id：%s" % distinct[:5], severity="中", dimension=dim)
        return
    R.add_issue("[%s] 相同请求重复提交结果不一致：%s" % (case.get("id"), msg),
                params, fid=case.get("id"),
                expected="重复提交结果应一致（幂等）", actual=msg, severity="中", dimension=dim)


def _check_cross_tenant_read(case, parsed, args):
    """A：越权读的**配对对照**检查——先自证本企业读得到，再看跨企业读不读得到。

    为什么不能直接断言"跨企业读被拒绝"：读接口常常因为缺必填参数、对象不存在而失败，
    那种失败会被当成"越权被拦住了"，是一条**假的通过**——比漏测更危险。
    所以判定必须配对：
      本企业身份也读不到 → 无法归因，记观察（读接口不可用/参数不足），不判越过权；
      本企业读得到、跨企业也读得到 → 越权缺陷（跨企业身份拿到了本企业数据）；
      本企业读得到、跨企业读不到 → 隔离生效，记观察。
    """
    ra = args.get("read_action") or case.get("action")
    rp = dict(args.get("read_params") or {})
    dim = case.get("dimension") or "A"
    personal = getattr(R, "PERSONAL", {}) or {}
    cross = (args.get("cross_appid") or "").strip()
    if not cross or cross.startswith("<"):
        cross = (resolve_placeholders({"appid": "<跨企业appid>"}, personal) or {}).get("appid", "")
    if not cross or str(cross).startswith("<"):
        _need_config(case, "cross_tenant_read", ["<跨企业appid>"],
                     "个人配置缺 cross_appid，无法构造跨企业身份")
        _skip(case, "N/A", "缺 cross_appid：越权读结论不可归因，不做判定")
        return
    _c1, _r1, mine = R.call(R.build_payload(ra, **rp))
    ok_mine = bool(isinstance(mine, dict) and mine.get("result") is True)
    if not ok_mine:
        R.add_observation(
            "[%s] 越权读未判定：本企业身份调 %s 也失败，拒绝无法归因到越权隔离"
            % (case.get("id"), ra), rp, fid=case.get("id"),
            expected="本企业身份应能读到（对照组成立才能判越权）",
            actual="本企业身份即失败：%s" % str((mine or {}).get("errormsg") or "")[:120],
            severity="低", dimension=dim)
        return
    p2 = dict(rp)
    p2["appid"] = cross
    _c2, _r2, other = R.call(R.build_payload(ra, **p2))
    ok_other = bool(isinstance(other, dict) and other.get("result") is True)
    if ok_other:
        R.add_issue(
            "[%s] 跨企业身份读到本企业数据（越权读）：读接口 %s" % (case.get("id"), ra),
            p2, fid=case.get("id"),
            expected="跨企业 appid 应取不到本企业数据",
            actual="返回成功：%s" % str((other.get("data") or {}))[:160], severity="高", dimension=dim)
        return
    R.add_observation(
        "[%s] 跨企业身份未读到数据，租户隔离生效（读接口 %s）" % (case.get("id"), ra),
        p2, fid=case.get("id"),
        expected="跨企业身份应取不到本企业数据",
        actual="被拒绝：%s" % str((other or {}).get("errormsg") or "")[:120],
        severity="低", dimension=dim)


def _check_client_variant(case, parsed, args):
    """L：跨客户端兼容的**配对对照**检查——基线请求与各 UA 变体对比响应结构。

    黑盒测试拿不到多版本 SDK，只能靠改 User-Agent 让服务端走到不同客户端分支；
    所以这是降级版，真跨 SDK 兼容仍需多端环境，用例 note 里会写明。

    为什么必须配对：变体请求失败可能因为服务端不支持该端，也可能只是这次网络问题，
    单看变体会误报。基线也失败 → 无法归因，只记观察。
    """
    variants = args.get("user_agents") or {}
    if not variants:
        _skip(case, "N/A", "未配置 UA 变体，无法做跨客户端对照")
        return
    action = case.get("action")
    params = dict(case.get("params") or {})
    dim = case.get("dimension") or "L"

    def keys_of(p):
        d = (p or {}).get("data") if isinstance(p, dict) else None
        return set(d.keys()) if isinstance(d, dict) else set()

    _c0, _r0, base = R.call(R.build_payload(action, **params))
    if not (isinstance(base, dict) and base.get("result") is True):
        R.add_observation(
            "[%s] 跨客户端兼容未判定：基线请求即失败，变体差异无法归因"
            % case.get("id"), params, fid=case.get("id"),
            expected="基线请求应成功（对照组成立才能判兼容）",
            actual="基线失败：%s" % str((base or {}).get("errormsg") or "")[:120],
            severity="低", dimension=dim)
        return
    base_keys = keys_of(base)
    for label, ua in variants.items():
        _c, _r, p = R.call(R.build_payload(action, **params),
                           extra_headers={"User-Agent": ua})
        if not (isinstance(p, dict) and p.get("result") is True):
            R.add_issue(
                "[%s] %s 端请求失败，而默认端成功（跨客户端兼容）" % (case.get("id"), label),
                params, fid=case.get("id"),
                expected="同一请求在各客户端特征下都应可用",
                actual="%s 端失败：%s" % (label, str((p or {}).get("errormsg") or "")[:120]),
                severity="中", dimension=dim)
            continue
        cur = keys_of(p)
        if cur != base_keys:
            R.add_issue(
                "[%s] %s 端响应结构与默认端不一致（跨客户端兼容）" % (case.get("id"), label),
                params, fid=case.get("id"),
                expected="各端返回字段集合应一致：%s" % sorted(base_keys)[:8],
                actual="%s 端 多出=%s 缺少=%s" % (label, sorted(cur - base_keys)[:8],
                                                  sorted(base_keys - cur)[:8]),
                severity="高", dimension=dim)
            continue
        R.add_observation(
            "[%s] %s 端响应结构与默认端一致（跨客户端兼容）" % (case.get("id"), label),
            params, fid=case.get("id"),
            expected="各端字段集合一致", actual="一致，共 %d 个字段" % len(cur),
            severity="低", dimension=dim)


def _check_state_transition(case, parsed, args):
    """E：状态转换矩阵实测——逐个目标状态提交，产出「哪些被接受 / 被拒 / 回读值」。

    哪一对 (当前态 → 目标态) 合法是业务语义，自动生成器不猜，所以这里**不判非法转换缺陷**
    （那是 E03 留给人工的活）。只做两件确定的事：
      ① 把矩阵跑成事实表，人不必从零设计；
      ② 硬判唯一能确定的缺陷——"接受了但回读值 ≠ 提交值"（状态被静默改写 / 参数失效），
         这个不需要知道业务规则就能确定是错的。
    """
    field = args.get("field")
    values = args.get("values") or []
    if not field or not values:
        _need_config(case, "state_transition", ["check_args.field", "check_args.values"],
                     "缺状态字段或状态取值清单，无法跑转换矩阵")
        return
    dim = case.get("dimension") or "E"
    ra = args.get("read_action")
    rp = args.get("read_params") or {}
    base = dict(case.get("params") or {})
    rows = []
    for v in values:
        p = dict(base)
        p[field] = v
        _c, _raw, pr = R.call(R.build_payload(case.get("action") or "", **p))
        ok = bool(isinstance(pr, dict) and pr.get("result") is True)
        read_back = None
        if ok:
            src = None
            if ra:
                _c2, _r2, rd = R.call(R.build_payload(ra, **rp))
                src = rd
            else:
                src = pr
            if isinstance(src, dict) and isinstance(src.get("data"), dict):
                read_back = src["data"].get(field)
        rows.append({"target": v, "accepted": ok, "read_back": read_back})
    accepted = [str(r["target"]) for r in rows if r["accepted"]]
    rejected = [str(r["target"]) for r in rows if not r["accepted"]]
    R.add_observation(
        "[%s] 状态转换矩阵实测（%s）：接受 %s / 拒绝 %s"
        % (case.get("id"), field, accepted or "无", rejected or "无"),
        {"field": field, "rows": rows}, fid=case.get("id"),
        expected="接受/拒绝的组合应与业务状态图一致（需人工核对）",
        actual="回读值 %s" % {str(r["target"]): r["read_back"] for r in rows if r["accepted"]},
        severity="低", dimension=dim)
    drift = [r for r in rows
             if r["accepted"] and r["read_back"] is not None
             and str(r["read_back"]) != str(r["target"])]
    if drift:
        R.add_issue(
            "[%s] 状态被静默改写：%s"
            % (case.get("id"), "; ".join("提交=%s 回读=%s" % (r["target"], r["read_back"]) for r in drift)),
            {"rows": drift}, fid=case.get("id"),
            expected="接受的状态值应原样回读",
            actual="回读值与提交值不一致：%s" % [{str(r["target"]): r["read_back"]} for r in drift],
            severity="高", dimension=dim)


def _check_uniqueness(case, parsed, args):
    """AC：业务唯一性——同一唯一值重复提交，第二次必须被拒绝。

    执行顺序天然正确：本用例刚刚用该值创建过一次，这里再提交一次即为"重复提交"。"""
    field = args.get("field")
    if not field:
        _need_config(case, "uniqueness", ["check_args.field"], "无法确定唯一性字段")
        return
    params = case.get("params") or {}
    value = params.get(field)
    if value in (None, "", []):
        _need_config(case, "uniqueness", ["params.%s" % field], "唯一值字段为空，无法构造重复提交")
        return
    action = case.get("action")
    ok, _dup, msg = R.check_uniqueness(action, params, field, value, expect_reject=True)
    if ok is False:
        R.add_issue("[%s] 业务唯一性未校验：%s" % (case.get("id"), msg), params,
                    fid=case.get("id"), expected="重复值应被拒绝并回显冲突字段",
                    actual=msg, severity="高", dimension=case.get("dimension") or "AC")


def _check_pagination(case, parsed, args):
    """AA：分页一致性——页间不重复、total 与返回条数一致、筛选无效时不得静默返回全量。"""
    list_action = args.get("list_action") or case.get("action")
    page_param = args.get("page_param") or "page_index"
    if not list_action:
        _need_config(case, "pagination", ["check_args.list_action"], "无法确定列表接口")
        return
    base = args.get("base_params") or case.get("params") or {}
    ok, findings, msg = R.check_pagination(
        list_action, base,
        page_param=page_param,
        size_param=args.get("size_param") or "page_size",
        total_field=args.get("total_field") or "total",
        list_field=args.get("list_field") or "list")
    if ok is False or findings:
        R.add_issue("[%s] 分页一致性缺陷：%s" % (case.get("id"), msg), base,
                    fid=case.get("id"), expected="页间不重复、total 一致、筛选不得静默返回全量",
                    actual=msg, severity="中", dimension=case.get("dimension") or "AA")


def _check_terminal_repeat(case, parsed, args):
    """E：终态重复操作——处于终态的资源再次执行同一操作，必须被拒绝而不是静默成功。"""
    action = case.get("action")
    params = case.get("params") or {}
    if not action:
        _need_config(case, "terminal_repeat", ["action"], "无法确定接口")
        return
    ok, _results, msg = R.check_idempotent(action, lambda: dict(params), times=2)
    if ok is None:
        # 【W3 配套】不具备比对前提（接口全失败/响应缺 result），留痕不判定。
        R.add_observation(
            "[%s] 终态重复操作无法验证：%s" % (case.get("id"), msg),
            params, fid=case.get("id"), severity="低",
            dimension=case.get("dimension") or "E")
    elif ok is False:
        R.add_observation(
            "[%s] 终态重复操作未幂等：%s（需人工确认是终态保护缺失还是幂等语义不同）"
            % (case.get("id"), msg), params, fid=case.get("id"),
            severity="低", dimension=case.get("dimension") or "E")


def _check_render_degrade(case, parsed, args):
    """接口侧 UI 代理信号：字段存在但为空 → 前端渲染降级（空白、占位缺失、默认值错）。

    不需要页面基线即可自动执行：文档声明返回的字段若返回 null / 空串 / 空集合，
    前端拿到的是"有键无值"，是最常见的错版与空白区域成因。"""
    if not isinstance(parsed, dict):
        return
    data = parsed.get("data")
    if not isinstance(data, dict):
        return
    fields = args.get("fields") or []
    degraded = [f for f in fields if f in data and data.get(f) in (None, "", [], {})]
    if degraded:
        R.add_issue(
            "[%s] 渲染降级信号：文档声明字段返回空值（前端将渲染为空白/占位缺失）：%s"
            % (case.get("id"), degraded), case.get("params", {}), fid=case.get("id"),
            expected="声明字段应返回有效值或文档化的默认值",
            actual="空值字段 %s" % degraded, severity="中",
            dimension=case.get("dimension") or "Y")


def _check_ui_parity(case, parsed, args):
    """AB：前端-接口口径一致性——用页面基线作为预期值，与接口返回值逐字段比对。

    这是接口侧唯一能触达 UI 还原缺陷的通路，此前因"无基线"被永久跳过、从未真正执行。
    现在的语义是**二分**：
      有基线 → 逐字段比对，不一致即记缺陷（AB 真正被执行）；
      无基线 → 记【覆盖率缺口】，维度不会被算作已覆盖（不再假绿），也不凭空编预期值。"""
    action = case.get("action")
    baselines, src = UB.load(args.get("baseline_path"))
    dim = case.get("dimension") or "AB"
    if not baselines:
        R.add_issue(
            "[%s] AB 口径未校验：未找到页面基线，无法比对接口值与页面显示值"
            % case.get("id"),
            case.get("params", {}), fid="COV-%s" % case.get("id"),
            expected="登记页面基线后比对（references/ui_baseline.md）",
            actual="无基线文件（WJX_UI_BASELINE / ~/.workbuddy/wjx-api-test/ui_baseline.json）",
            severity="中", dimension=dim)
        return
    checked, gaps, findings = UB.check_parity(baselines, action, parsed, args)
    for name, api_v, page_v, msg in findings:
        R.add_issue("[%s] %s：%s" % (case.get("id"), name, msg),
                    case.get("params", {}), fid=case.get("id"),
                    expected="接口值与页面显示一致（页面基线：%r）" % (page_v,),
                    actual="接口返回 %r" % (api_v,), severity="高", dimension=dim)
    if checked == 0 and gaps:
        R.add_issue(
            "[%s] AB 口径未校验：字段 %s 已有页面基线缺失" % (case.get("id"), gaps),
            case.get("params", {}), fid="COV-%s" % case.get("id"),
            expected="这些字段需在基线文件中登记页面值",
            actual="缺基线字段 %s" % gaps, severity="中", dimension=dim)


def _check_transport_contract(case, parsed, args):
    """AE：传输层契约——只看 body 和 result 发现不了网关 / SDK / 中间件问题。

    Content-Type 声明错（JSON 体却标 text/html）会让部分客户端解析失败；
    可追踪标识（trace-id / request-id）缺失会让线上排障无据可依——后者只记观察，
    因为很多环境由网关按需注入，硬判缺陷会制造误报。"""
    dim = case.get("dimension") or "AE"
    headers = dict(getattr(R, "LAST_RESPONSE_HEADERS", {}) or {})
    if not headers:
        _need_config(case, "transport_contract", ["响应头"],
                     "call() 未记录到响应头，无法校验传输层契约")
        return
    required = args.get("required")
    if required is None:
        required = ["content-type"]
    ok, problems, msg = R.check_transport_contract(
        headers, expected=args.get("expected") or {}, required=required,
        expect_json=args.get("expect_json", True))
    if ok is False:
        R.add_issue("[%s] 传输层契约不符：%s" % (case.get("id"), msg),
                    case.get("params", {}), fid=case.get("id"),
                    expected="Content-Type 等传输层约定明确且正确",
                    actual="; ".join(problems), severity="中", dimension=dim)
    lower = {str(k).lower() for k in headers}
    trace_keys = {"x-trace-id", "trace-id", "x-request-id", "request-id",
                  "x-correlation-id", "correlation-id"}
    if not (lower & trace_keys):
        R.add_observation(
            "[%s] 响应未提供可追踪标识（trace-id / request-id 类）：线上排障无据可依"
            % case.get("id"), case.get("params", {}), fid=case.get("id"),
            expected="建议在响应头回传可追踪标识", actual="实际响应头：%s" % sorted(lower),
            severity="低", dimension=dim)


def _check_enum_exhaustive(case, parsed, args):
    """K/Top6：文档声明的每个枚举值，写接口都必须接受——读写一致性。

    历史缺陷：获取列表返回 0 但提交传 0 却不行；枚举 1001~1005 实为"每周"、
    文案写成"每年"。输入（documented 枚举全集）由 pattern_engine 从契约解析后随用例下发，
    此前这里挂着"需响应返回枚举全集"的占位说明，从未真正执行。"""
    dim = case.get("dimension") or "K"
    field = args.get("field")
    documented = list(args.get("documented") or [])
    if not field:
        _need_config(case, "enum_exhaustive", ["check_args.field"], "无法确定枚举字段")
        return
    ok_empty, empty_msg = R.assert_non_empty(documented, "枚举文档 %s" % field)
    if not ok_empty:
        R.add_issue("[%s] 枚举遍历未执行：%s" % (case.get("id"), empty_msg),
                    case.get("params", {}), fid="COV-%s" % case.get("id"),
                    expected="契约需声明该字段的枚举取值", actual=empty_msg,
                    severity="中", dimension=dim)
        return
    action = case.get("action")
    base = dict(case.get("params") or {})
    enum_map = args.get("enum_map") or {}
    rejected, skipped_dangerous = [], []
    for value in documented:
        label = str(enum_map.get(str(value)) or enum_map.get(value) or "")
        low = label.lower()
        if any(h in low or h in label for h in _DANGEROUS_ENUM_HINTS):
            skipped_dangerous.append((value, label))
            continue
        params = dict(base)
        params[field] = value
        _code, raw, p = R.call(R.build_payload(action, **params))
        if not isinstance(p, dict) or p.get("result") is not True:
            err = str(p.get("errormsg")) if isinstance(p, dict) else str(raw)[:120]
            rejected.append((value, err[:120]))
    if skipped_dangerous:
        R.add_observation(
            "[%s] 枚举遍历跳过危险终态值（不自动提交，需人工在受控环境评估）：%s"
            % (case.get("id"), "; ".join("%s=%s" % (v, l or "?") for v, l in skipped_dangerous)),
            {field: [v for v, _ in skipped_dangerous]}, fid=case.get("id"),
            expected="危险终态值由人工验证，不得由自动化真实提交",
            actual="已跳过 %d 个" % len(skipped_dangerous), severity="低", dimension=dim)
    if rejected:
        R.add_issue(
            "[%s] 文档声明枚举被写接口拒绝（读写不一致）：%s"
            % (case.get("id"), "; ".join("%s=%r 被拒(%s)" % (field, v, e) for v, e in rejected)),
            {field: [v for v, _ in rejected]}, fid=case.get("id"),
            expected="文档声明的每个枚举值都应被写接口接受",
            actual="被拒 %d/%d 个取值" % (len(rejected), len(documented)),
            severity="高", dimension=dim)
        return
    read_action = args.get("read_action")
    if read_action:
        _c, _r, rd = R.call(R.build_payload(read_action, **(args.get("read_params") or {})))
        if isinstance(rd, dict) and rd.get("result") is True:
            got = (rd.get("data") or {}).get(field)
            if got is not None and got not in documented:
                R.add_issue(
                    "[%s] 读接口返回文档未声明的枚举值 %r（读集 ⊃ 写集，读写不一致）"
                    % (case.get("id"), got), {field: got}, fid=case.get("id"),
                    expected="读接口返回值应落在文档声明的枚举集合内",
                    actual="读回 %r，文档集合 %s" % (got, documented),
                    severity="中", dimension=dim)
                return
    R.add_observation(
        "[%s] 枚举读写一致性通过：%d 个文档取值均可写入" % (case.get("id"), len(documented)),
        {field: documented}, fid=case.get("id"),
        expected="文档枚举可写且可读", actual="全部接受", severity="低", dimension=dim)


def _check_concurrency(case, parsed, args):
    """J2/AC5：并发重复提交不得产生重复数据。

    用线程池真实并发发出同一请求。判定口径按已知信息二分（防误报）：
      给了 unique_field → 并发成功次数 > 1 即判缺陷（同一唯一键被创建多次）；
      未给            → 只记观察（业务可能本就允许多条，硬判会把正常接口判成 bug）。
    次数受 CONCURRENCY_MAX_TIMES 硬上限约束，防误配置打爆服务。"""
    dim = case.get("dimension") or "J"
    action = case.get("action")
    if not action:
        _need_config(case, "concurrency", ["action"], "无法确定接口")
        return
    try:
        times = int(args.get("times") or 5)
    except Exception:
        times = 5
    times = max(1, min(times, CONCURRENCY_MAX_TIMES))
    params = dict(case.get("params") or {})
    unique_field = args.get("unique_field")

    def _one(_i):
        try:
            # 用 call_meta 拿到 (code, headers)：二者都来自这次 call 的返回值，同源一致。
            # 不在并发段记台账，而是把 code/headers 带出来，待主线程显式传给 add_issue——
            # 否则会读到被线程池污染的全局旁路 LAST_RESPONSE_HEADERS，request_id/trace_id
            # 张冠李戴到同批次的另一条重复请求上。
            code, _r, p, hdrs = R.call_meta(R.build_payload(action, **params))
            return (bool(isinstance(p, dict) and p.get("result") is True), code, hdrs)
        except Exception:
            return (False, None, None)

    with ThreadPoolExecutor(max_workers=times) as ex:
        oks = list(ex.map(_one, range(times)))
    success = sum(1 for x in oks if x[0])
    if unique_field and success > 1:
        R.add_issue(
            "[%s] 并发重复提交产生 %d 条成功（唯一键 %s 应只允许 1 条）"
            % (case.get("id"), success, unique_field), params, fid=case.get("id"),
            expected="同一唯一键并发提交只应成功 1 次",
            actual="并发 %d 次成功 %d 次" % (times, success),
            severity="高", dimension=dim,
            http_code=oks[0][1], headers=oks[0][2])
        return
    note = ""
    if success > 1 and not unique_field:
        note = "（未声明唯一键，需人工确认业务是否允许重复）"
    R.add_observation(
        "[%s] 并发 %d 次：成功 %d 次、失败 %d 次%s"
        % (case.get("id"), times, success, times - success, note),
        params, fid=case.get("id"), expected="并发行为符合业务唯一性预期",
        actual="成功 %d/%d" % (success, times), severity="低", dimension=dim,
        http_code=oks[0][1], headers=oks[0][2])


def _check_rate_limit(case, parsed, args):
    """J/Top9：限流的可观测性。

    连发 N 次观测是否出现限流信号（429 或限流文案）。刻意只产出**观察**而非缺陷：
    未观测到限流只说明阈值高于本次次数，不说明限流缺失——真实阈值需压测授权后测定。
    次数受 RATE_LIMIT_MAX_TIMES 硬上限约束。"""
    dim = case.get("dimension") or "J"
    action = case.get("action")
    if not action:
        _need_config(case, "rate_limit", ["action"], "无法确定接口")
        return
    try:
        times = int(args.get("times") or RATE_LIMIT_MAX_TIMES)
    except Exception:
        times = RATE_LIMIT_MAX_TIMES
    times = max(1, min(times, RATE_LIMIT_MAX_TIMES))
    try:
        interval = max(0.0, float(args.get("interval_ms") or 0) / 1000.0)
    except Exception:
        interval = 0.0
    params = dict(case.get("params") or {})
    limited_at, codes = None, []
    for i in range(times):
        code, raw, p = R.call(R.build_payload(action, **params))
        codes.append(code)
        errmsg = str(p.get("errormsg", "")) if isinstance(p, dict) else str(raw)
        low = errmsg.lower()
        if code == 429 or any(k in errmsg or k in low for k in RATE_LIMIT_KEYWORDS):
            limited_at = i + 1
            break
        if interval:
            time.sleep(interval)
    if limited_at:
        cooldown = max(0.0, float(args.get("cooldown_s") or 3))
        if cooldown:
            time.sleep(cooldown)  # 触发限流后冷却，避免污染后续用例的判定
        R.add_observation(
            "[%s] 限流已生效：第 %d 次请求被限流（观测阈值约 %d 次）"
            % (case.get("id"), limited_at, limited_at), params, fid=case.get("id"),
            expected="限流生效并有明确提示（429 / 提示语 / 恢复时间）",
            actual="第 %d 次触发限流；状态码序列 %s" % (limited_at, codes),
            severity="低", dimension=dim)
        return
    R.add_observation(
        "[%s] 连续 %d 次未观测到限流：阈值可能高于该次数，不代表限流缺失；"
        "真实阈值需压测授权后测定" % (case.get("id"), times),
        params, fid=case.get("id"), expected="明确限流阈值与限流响应语义",
        actual="%d 次请求状态码均为 %s" % (times, sorted(set(codes))),
        severity="低", dimension=dim)


def _minimize_on_fail(case):
    """FAIL 后自动做参数缩减，产出最小复现请求，写回该条 finding。

    minimizer 此前从未被任何生产代码调用；发现问题却交不出"最小复现"，
    开发要自己在几十个字段里二分。用 WJX_NO_MINIMIZE=1 可关闭（离线/只读场景）。"""
    if not MINIMIZE_ENABLED or os.environ.get("WJX_NO_MINIMIZE") == "1":
        return
    if not R.results or R.results[-1].get("status") != "FAIL":
        return
    action = case.get("action")
    params = case.get("params") or {}
    if not action or len(params) < 2:
        return
    want_success = bool((case.get("expect") or {}).get("success"))

    def replay(candidate):
        try:
            _code, _raw, parsed = R.call(R.build_payload(action, **candidate))
        except Exception:
            return False
        if not isinstance(parsed, dict):
            return False
        return (parsed.get("result") is True) != want_success

    try:
        res = minimizer.minimize_body(params, replay)
    except Exception:
        return
    removed = res.get("removed") or []
    if not removed:
        return
    for f in reversed(R.findings):
        if str(f.get("id") or "") == str(case.get("id")):
            f["minimal_repro"] = json.dumps(res["body"], ensure_ascii=False)
            break


CHECKS = {
    "contract_diff": _check_contract_diff,
    "ui_parity": _check_ui_parity,
    "structure_complete": _check_structure_complete,
    "field_naming": _check_field_naming,
    "name_consistency": _check_name_consistency,
    "url_alive": _check_url_alive,
    "error_root_cause": _check_error_root_cause,
    "http_semantics": _check_http_semantics,
    "roundtrip": _check_roundtrip,
    "render_degrade": _check_render_degrade,
    "uniqueness": _check_uniqueness,
    "cross_list": _check_cross_list,
    "count_delta": _check_count_delta,
    "idempotent": _check_idempotent,
    "trace_id": _check_trace_id,
    "export_credential": _check_export_credential,
    "state_transition": _check_state_transition,
    "cross_tenant_read": _check_cross_tenant_read,
    "client_variant": _check_client_variant,
    "pagination": _check_pagination,
    "terminal_repeat": _check_terminal_repeat,
    "transport_contract": _check_transport_contract,
    "enum_exhaustive": _check_enum_exhaustive,
    "concurrency": _check_concurrency,
    "rate_limit": _check_rate_limit,
}

# 留在本字典 = 该检查永久不执行：判据是"名字在不在字典里"（编码期事实），
# 与运行时条件无关。因此这里只允许放**确实拿不到输入或观测通道**的项，且跳过时必须点名；
# 能实现的检查一律进 CHECKS，不要放进本字典。
MANUAL_CHECKS = {
    "event_delivery": "需推送日志查询通道（观测面在接口响应之外，黑盒边界）",
    "batch_partial": "需批量接口与失败明细字段（单条接口不命中，属条件 N/A）",
    "timezone": "需页面时间显示值对照（国内版单时区场景 N/A）",
}


# 与 run()/assert_validation() 签名同名的框架控制键：若业务参数里出现同名键，
# 会让 Python 在以 **params 展开时抛 "got multiple values for argument"，整条链路直接崩溃。
RESERVED_PARAM_KEYS = ("action", "dimension", "category", "applicability",
                       "contract", "err_exact", "expect_success", "case_no", "name")


def _sanitize_params(case):
    """剔除与框架参数同名的业务键并显式告警（不静默丢弃）。"""
    params = dict(case.get("params") or {})
    clash = [k for k in RESERVED_PARAM_KEYS if k in params]
    if clash:
        for k in clash:
            params.pop(k, None)
        R.add_observation(
            "[%s] 业务参数含框架保留键 %s，已剔除以免与执行器签名冲突"
            % (case.get("id"), clash), params, fid=case.get("id"), severity="低",
            dimension=case.get("dimension") or "")
    return params


# 用例里承载个人身份的占位符（A 维度越权矩阵用）。只认这几个已知占位符——
# 不能笼统匹配 <...>：UI 代理用例的 HTML 载荷里就带 <b> 标签，会被误判成未解析占位符。
_IDENTITY_PLACEHOLDERS = ("<跨企业appid>", "<子账号>", "<主账号>", "<APPID>", "<CORPID>", "<SYSID>")


def _resolve_identity_placeholders(case, params):
    """执行期把用例里的身份占位符换成个人配置里的真实值；失败返回 None（调用方跳过）。

    解析不到时**不替换成空串**：身份为空的请求会被服务端按参数校验拒绝，
    而拒绝又会被当成"越权被拦住了"——一条假的通过。改为点名跳过，
    缺的是哪个配置一目了然。
    """
    resolved = resolve_placeholders(params, getattr(R, "PERSONAL", {}) or {})
    left = sorted({ph for v in resolved.values() if isinstance(v, str)
                   for ph in _IDENTITY_PLACEHOLDERS if ph in v})
    if left:
        _need_config(case, "identity", left, "个人配置缺该字段，越权矩阵无法构造请求")
        _skip(case, "N/A", "身份占位符未解析：%s（在 personal_config.json / WJX_* 补齐后重跑）"
              % ", ".join(left))
        return None
    return resolved


def exec_case(case, dry=False):
    cid = case.get("id", "")
    name = case.get("name", "")
    dim = case.get("dimension", "")
    action = case.get("action")
    params = _sanitize_params(case)
    expect = case.get("expect") or {}

    if dry:
        print("[%s] %-52s 维度=%-3s 预期=%s" % (cid, name[:52], dim,
                                            "成功" if expect.get("success") else "被拒绝"))
        return

    # 【W34】真实执行前的配置防呆（幂等；dry-run 不经过这里）。空配置时每条请求
    # 都会失败并全部归 ENV，产出 exit 0 的空报告——那是最危险的静默失败形态。
    ensure_runnable_config()

    if not case.get("enabled", True):
        _skip(case, "SKIPPED", "未启用（%s）" % (case.get("note") or "enabled=false"))
        return
    needs = case.get("needs") or ""
    if needs == "ui_baseline":
        # 有基线 → 正常执行 AB 比对；无基线 → 记覆盖缺口后跳过（不静默、不假绿、不编预期值）
        baselines, _src = UB.load((case.get("check_args") or {}).get("baseline_path"))
        if not baselines:
            R.add_issue(
                "[%s] AB 口径未执行：未登记页面基线，接口值与页面显示值无法比对"
                % case.get("id"), case.get("params", {}), fid="COV-%s" % case.get("id"),
                expected="按 references/ui_baseline.md 登记基线（来源：人工页面/浏览器抓取/设计稿）",
                actual="无基线，未做任何比对", severity="中", dimension=case.get("dimension") or "AB")
            _skip(case, "N/A", "无页面基线，未取基线前不得凭空填预期（%s）" % (case.get("note") or ""))
            return
    if needs == "manual":
        _skip_uncovered(case, "需人工执行（%s）" % (case.get("note") or case.get("check") or ""),
                        "补做该人工检查，或改用可自动化的等价断言")
        return
    check = case.get("check") or ""
    if check in MANUAL_CHECKS:
        _skip_uncovered(case, "check=%s 需人工/扩展能力：%s" % (check, MANUAL_CHECKS[check]),
                        "先补齐该检查所需能力（%s），否则该口径无任何验证" % MANUAL_CHECKS[check])
        return

    keys = expect.get("contract_keys")
    if keys:
        R.CONTRACT_KEYS = list(keys)

    params = _resolve_identity_placeholders(case, params)
    if params is None:
        return

    # 计数基线必须在主请求之前采集（+1/-1 是"操作前 vs 操作后"的差）
    capture_pre_baseline(case)

    if expect.get("success"):
        parsed, _vid = R.run(cid, name, action, True, case.get("category", ""),
                             contract=bool(keys), applicability=case.get("applicability", "适用"),
                             dimension=dim,
                             tolerate_reject=expect.get("tolerate_reject"),
                             **params)
    else:
        parsed, _ = R.assert_validation(cid, name, action, err_exact=expect.get("err_keywords"),
                                        applicability=case.get("applicability", "适用"),
                                        dimension=dim, **params)

    if expect.get("success") and isinstance(parsed, dict):
        _res = extract_resource(parsed, action)
        if _res:
            _res["params"] = params
            R.CREATED_RESOURCES.append(_res)

    fn = CHECKS.get(check)
    if fn and isinstance(parsed, dict):
        try:
            fn(case, parsed, case.get("check_args") or {})
        except Exception as e:
            R.add_observation("[%s] check=%s 执行异常：%s" % (cid, check, e),
                              params, fid=cid, severity="低", dimension=dim)
    _minimize_on_fail(case)


def write_reports(out_prefix="api_test"):
    with open("%s_issues.csv" % out_prefix, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLS)
        for x in R.findings:
            w.writerow(R.finding_row(x))
    with open("%s_report_full.json" % out_prefix, "w", encoding="utf-8") as f:
        json.dump({"results": R.results, "findings": R.findings,
                   "coverage_plan": R.COVERAGE_PLAN,
                   "flaky": attribution_engine.detect_flaky(R.results)}, f,
                  ensure_ascii=False, indent=2)
    print("\n问题台账 -> %s_issues.csv" % out_prefix)
    print("完整报告 -> %s_report_full.json" % out_prefix)


def main():
    ap = argparse.ArgumentParser(description="声明式用例执行器")
    ap.add_argument("--cases", required=True, help="用例 JSON（case_generator.py 产出）")
    ap.add_argument("--dry-run", action="store_true", help="只列执行计划，不发请求")
    ap.add_argument("--only", help="只跑指定维度，逗号分隔（如 B,H,K）")
    ap.add_argument("--out-prefix", default="api_test", help="产物文件名前缀")
    args = ap.parse_args()

    with open(args.cases, "r", encoding="utf-8") as f:
        doc = json.load(f)
    cases = doc.get("cases") or []
    if args.only:
        wanted = {d.strip() for d in args.only.split(",") if d.strip()}
        cases = [c for c in cases if (c.get("dimension") or "") in wanted]

    # 关键：把自动生成的覆盖计划装进门禁，让反静默缺漏默认生效
    R.COVERAGE_PLAN = doc.get("coverage_plan") or []
    # 约束对账结果（生成期算出，执行期出声）
    R.CHECK_KEY_MISSING = doc.get("check_key") or {}

    print("=" * 70)
    print("声明式用例执行 | action=%s | 接口=%s | 用例=%d" % (doc.get("action"), doc.get("name"), len(cases)))
    print("COVERAGE_PLAN（门禁）: %s" % ", ".join(R.COVERAGE_PLAN))
    print("=" * 70)

    for c in cases:
        exec_case(c, dry=args.dry_run)

    if args.dry_run:
        print("\n[dry-run] 未发起任何请求。去掉 --dry-run 即真实执行。")
        return

    print("\n" + "#" * 70)
    print("汇总")
    print("#" * 70)
    counts = {}
    for r in R.results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(counts)
    R.coverage_gate()
    R.check_key_gate()
    if R.shallow_passes:
        print("\n⚠️ 浅用例 %d 条：仅断言 result=true、未做深度断言。" % R.shallow_passes)
    write_reports(args.out_prefix)


if __name__ == "__main__":
    main()
