# -*- coding: utf-8 -*-
"""契约断言 helper（纯函数）——从 api_test_runner 外迁的无状态检查函数。

这些函数只依赖入参与标准库，不读写 api_test_runner 的模块级状态
（results / call / 个人配置等），因此可安全独立成模块。
api_test_runner 用 `from assertion_helpers import (...)` 把它们重新引入自身
命名空间，从而保持 `R.check_xxx` 的既有引用契约不变（含测试里的 monkeypatch）。

本模块绝不 import api_test_runner（避免循环依赖）。
"""

import json
import re
import time
import hashlib

CONTRACT_KEYS = ["vid", "sid", "status"]


def html_with_title(title, body=""):
    """生成 title 与 html_content 内 <title> 一致的 HTML（按接口规则对齐，避免误命中一致性校验）。"""
    return f"<html><head><title>{title}</title></head><body>{body}"


def _is_coverage_gap(finding):
    """识别"覆盖率缺口"类记录：它描述的是"没测到"，不能反过来证明"测到了"。"""
    fid = str(finding.get("id") or "")
    desc = str(finding.get("desc") or finding.get("问题描述") or "")
    return fid.startswith("COV-") or "覆盖率缺口" in desc


def ts():
    return str(int(time.time()))


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
