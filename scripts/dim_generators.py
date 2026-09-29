# -*- coding: utf-8 -*-
"""零产出维度补齐生成器。

盘点发现 32 个维度中有 15 个从未产出过用例，其中 14 个属于「能自动测但没人写生成器」：
  I 编码国际化 / F 组合交叉 / V 请求追踪 / X 批量操作 / M 导出下载，
  以及按契约字段特征触发的业务域专项 N Q W S T O P R U。

三条设计约束：
1. **有特征才生成**——契约没有层级字段就不造 N 用例，没有附件字段就不造 W 用例。
   造出来的是噪音，且会让「该维度已覆盖」变成假象。
2. **预期说不清的不硬判**——如「父级不存在」在某些业务里合法（顶级节点），
   这类用例统一 P2 + note 说明「业务允许则记符合预期」，不制造假缺陷。
3. **条数受控**——每个命中维度 2 条，编码类按字符串字段数 × 2 + 2 条全局。
"""
import re

# ---------------------------------------------------------------- 用例构造


def _mk(cid, name, dim, category, params, expect, **kw):
    """与 case_generator._mk 字段集保持一致（不含 cleanup，由 _enrich_cases 统一补）。"""
    case = {
        "id": cid, "name": name, "dimension": dim, "category": category,
        "action": kw.pop("action", None), "applicability": kw.pop("applicability", "适用"),
        "params": params, "expect": expect,
        "priority": kw.pop("priority", "P1"), "source": kw.pop("source", ""),
        "enabled": kw.pop("enabled", True), "needs": kw.pop("needs", ""),
        "note": kw.pop("note", ""),
        "risk": kw.pop("risk", "medium"), "evidence": kw.pop("evidence", []),
        "tier": kw.pop("tier", "business"),
    }
    case.update(kw)
    return case


def _str_fields(contract):
    return [f for f in (contract.get("request_fields") or [])
            if (f.get("type") or "").lower() in ("string", "str", "text", "")]


def _req_fields(contract):
    return [f for f in (contract.get("request_fields") or []) if f.get("required")]


def _array_fields(contract):
    return [f for f in (contract.get("request_fields") or [])
            if (f.get("type") or "").lower() in ("array", "list")]


# ---------------------------------------------------------------- I 编码国际化

# 覆盖四类真实编码陷阱：多字节、代理对（UTF-16 4 字节）、全角归一、双向文本。
ENCODING_SAMPLES = [
    ("CN", "中文标题内容", "多字节字符（UTF-8 3 字节/字）"),
    ("EMOJI", "测试\U0001F600表情", "emoji：UTF-16 代理对，按字符数截断会切半"),
    ("FULLWIDTH", "ＴＥＳＴ１２３", "全角字符：NFKC 归一后长度变化"),
    ("RTL", "مرحبا", "RTL 双向文本：渲染与长度计算易错"),
]

_ENC_NOTE = ("编码样本由生成器注入，预期应被正确处理（存储/回显不乱码、不被截断）。"
             "若字段业务上限制字符集（如仅英文账号名），被拒属符合预期，记「待确认」而非缺陷")


def gen_encoding_cases(contract, base, action):
    """I 维度：对每个字符串字段注入中文与 emoji，再对首个字段补全角与 RTL。

    分层为 probe——它是「字段 × 编码样本」遍历，探测的是编码处理链路是否存在，
    不代表业务场景被测过。
    """
    out = []
    fs = _str_fields(contract)
    if not fs:
        return out
    for f in fs:
        for tag, val, desc in ENCODING_SAMPLES[:2]:
            p = dict(base)
            p[f["name"]] = val
            out.append(_mk("I-ENC-%s-%s" % (tag, f["name"]),
                           "编码国际化：%s=%s（%s）" % (f["name"], val[:12], desc),
                           "I", "编码国际化", p, {"success": True},
                           action=action, priority="P2", tier="probe",
                           source="generator:encoding", note=_ENC_NOTE))
    first = fs[0]
    for tag, val, desc in ENCODING_SAMPLES[2:]:
        p = dict(base)
        p[first["name"]] = val
        out.append(_mk("I-ENC-%s-%s" % (tag, first["name"]),
                       "编码国际化：%s=%s（%s）" % (first["name"], val[:12], desc),
                       "I", "编码国际化", p, {"success": True},
                       action=action, priority="P2", tier="probe",
                       source="generator:encoding", note=_ENC_NOTE))
    return out


# ---------------------------------------------------------------- F 组合交叉

_COMB_NOTE = ("多字段异常并存时，常见缺陷是「只报第一个错误」或「500 崩溃」。"
              "断言只硬判「必须被拒绝」，错误信息是否完整需人工核对")


def gen_combination_cases(contract, base, action):
    """F 维度：单字段异常已被 B 覆盖，F 测的是**多个字段同时异常**时的处理。"""
    out = []
    req = _req_fields(contract)
    fs = contract.get("request_fields") or []
    if len(fs) < 2:
        return out

    if len(req) >= 2:
        a, b = req[0], req[1]
        p = {k: v for k, v in base.items() if k not in (a["name"], b["name"])}
        out.append(_mk("F-COMB-MISSING", "组合交叉：%s 与 %s 同时缺失（应被拒绝且错误信息不误导）"
                       % (a["name"], b["name"]), "F", "组合交叉", p,
                       {"success": False}, action=action, priority="P2",
                       source="generator:combination", note=_COMB_NOTE))

    strs = _str_fields(contract)
    if len(strs) >= 2:
        a, b = strs[0], strs[1]
        p = dict(base)
        over = lambda f: ("OVERLONG_" * ((f.get("max_len") or 60) // 9 + 2))[: (f.get("max_len") or 60) + 5]
        p[a["name"]] = over(a)
        p[b["name"]] = over(b)
        out.append(_mk("F-COMB-OVERLEN", "组合交叉：%s 与 %s 同时超长（应被拒绝）"
                       % (a["name"], b["name"]), "F", "组合交叉", p,
                       {"success": False}, action=action, priority="P2",
                       source="generator:combination", note=_COMB_NOTE))

    if req and strs:
        r = req[0]
        other = next((f for f in strs if f["name"] != r["name"]), None)
        if other:
            p = dict(base)
            p.pop(r["name"], None)
            p[other["name"]] = ["type_error"]
            out.append(_mk("F-COMB-MIXED", "组合交叉：%s 缺失 + %s 类型错（应被拒绝且不崩溃）"
                           % (r["name"], other["name"]), "F", "组合交叉", p,
                           {"success": False}, action=action, priority="P2",
                           source="generator:combination", note=_COMB_NOTE))
    return out


# ---------------------------------------------------------------- V 请求追踪

def gen_trace_cases(contract, base, action):
    """V 维度：请求标识与追踪。

    只在响应/契约里能识别到追踪字段时才硬验「两次调用标识不同」；识别不到就记观察——
    没有 trace 字段是设计选择，不是缺陷，不能硬判。
    """
    out = []
    out.append(_mk("V01", "请求追踪：调用应返回请求标识，且两次调用标识不同",
                   "V", "请求追踪", dict(base), {"success": True},
                   check="trace_id", check_args={"expect_distinct": True},
                   action=action, priority="P2", source="generator:trace",
                   note="响应含 trace/request id 时验证其存在、非空且逐次不同（用于串联日志）。"
                        "契约未声明追踪字段时记观察，不判缺陷"))
    out.append(_mk("V02", "请求追踪：异常响应也应携带请求标识（便于排障）",
                   "V", "请求追踪", dict(base), {"success": True},
                   check="trace_id", check_args={"expect_distinct": False},
                   action=action, priority="P2", source="generator:trace",
                   note="错误响应缺少 trace id 会让线上问题无法定位；契约未声明时记观察"))
    return out


# ---------------------------------------------------------------- X 批量操作

def gen_batch_cases(contract, base, action):
    """X 维度：只在契约**确有数组字段**时生成——单条接口没有批量语义可测。"""
    out = []
    arrs = _array_fields(contract)
    if not arrs:
        return out
    f = arrs[0]
    p = dict(base)
    p[f["name"]] = ["item_%d" % i for i in range(100)]
    out.append(_mk("X-BATCH-01", "批量操作：%s 传入 100 条（应被接受或明确拒绝，不得超时/部分静默）"
                   % f["name"], "X", "批量操作", p, {"success": True},
                   action=action, priority="P2", source="generator:batch",
                   note="批量上限是业务约定：超限被拒属符合预期；"
                        "被接受但只处理了一部分且无提示才是缺陷"))
    p2 = dict(base)
    p2[f["name"]] = ["valid_item", None, "valid_item_2"]
    out.append(_mk("X-BATCH-02", "批量操作：%s 混入非法元素（应整体拒绝或明确部分失败语义）"
                   % f["name"], "X", "批量操作", p2, {"success": False},
                   action=action, priority="P2", source="generator:batch",
                   note="「部分成功」与「整体回滚」都是合法设计，但必须在响应里说清；"
                        "静默丢弃非法项才是缺陷。业务允许部分成功时记「符合预期」"))
    return out


# ---------------------------------------------------------------- 业务域专项

# (维度, 字段名 token, 用例定义)。token 同时命中字段名与接口名，与 applicability 的
# DOMAIN_RULES 判定口径保持一致，避免出现「判了选测但生成器不认」的错位。
DOMAIN_FIELD_DIMS = [
    ("N", ("parent", "pid", "parent_id", "folder", "folder_id", "level"),
     "层级资源", "层级归属"),
    ("Q", ("role", "permission", "privilege", "auth_level"),
     "多角色关系", "角色权限"),
    ("W", ("file", "attachment", "attach", "image", "url", "icon"),
     "文件附件", "附件字段"),
    ("S", ("category", "cate", "group_id", "container"),
     "容器分类", "分类归属"),
    ("T", ("user", "member", "account", "staff", "userid"),
     "账号身份", "账号关联"),
    ("O", ("notify", "push", "send", "remind", "message"),
     "异步通知", "通知推送"),
    ("P", ("share", "shared", "public"),
     "分享链接", "分享设置"),
    ("R", ("report", "report_id", "summary"),
     "报告分享", "报告标识"),
    ("U", ("import", "batch_file", "source_file"),
     "批量导入", "导入来源"),
]

# 语义角色 → 维度。字段名不含 token、但语义确实等价时，用语义层读出的角色激活维度。
#
# 只映射语义确实等价的角色，宁可少映射：映射过宽会让维度在无关接口上被激活，产出一批
# 断言站不住的用例。且只认请求字段——响应侧字段无法构造请求，激活了也产不出用例。
ROLE_DIM_MAP = {
    "operator_identity": "Q",   # 操作者身份：带角色约束时即角色权限维度
    "url_relative": "W",        # 相对路径 / 嵌入地址：资源类字段
    "embed_url": "W",
}

# 零产出原因分类。选测维度点名时必须带原因：只列维度字母时，「契约确实没有该特征」
# 与「有特征但生成器没接上 / 没实现」在报告里长得一模一样，看报告的人不会去追，
# 于是能力缺口被永久记成「本接口无需测」。
REASON_NO_FEATURE = "no_feature"
REASON_RESPONSE_SIDE_ONLY = "response_side_only"
REASON_NOT_IMPLEMENTED = "not_implemented"

REASON_TEXT = {
    REASON_NO_FEATURE: "契约无该维度特征（未满足生成器触发条件）",
    REASON_RESPONSE_SIDE_ONLY: "有特征，但字段仅在响应侧，无法构造请求",
    REASON_NOT_IMPLEMENTED: "该维度无生成器实现（能力缺口）",
}

# run_all 能产出的维度全集。不在此集合的维度即使被判选测也注定零产出，点名时
# 必须说明是能力缺口，否则会被误读成「本接口无需测」。
IMPLEMENTED_DIMS = {"I", "F", "V", "X", "M", "L"} | {d[0] for d in DOMAIN_FIELD_DIMS}

# 两种通用探测值：不存在的标识（应被拒）与明显非法的格式（应被拒）
_MISSING_ID = -1
_INVALID_FMT = "__invalid_ref__"

_DOMAIN_NOTE = ("引用完整性探测：指向不存在的%s应被拒绝且错误信息明确指向该字段。"
                "若业务允许该值（如顶级节点 parent 为空、公开分享无归属），"
                "被拒以外的情况记「符合预期」而非缺陷")


def _role_dim_hits(contract):
    """语义角色 → 可传参的请求字段。没有并入语义事实时返回空，纯规则链路行为不变。"""
    out = {}
    for f in (contract.get("request_fields") or []):
        dim = ROLE_DIM_MAP.get((f.get("llm_role") or "").strip())
        if dim and dim not in out:
            out[dim] = f
    return out


def _domain_dim_resolve(contract):
    """业务域维度判定的单一事实源：生成与零产出解释共用，避免两处逻辑各自漂移。

    返回 {dim: {"hit": 请求字段或 None, "via_role": bool, "reason": 原因或 None}}。
    hit 为 None 时 reason 必不为 None——即「为什么这个维度产不出用例」的唯一出处。
    """
    names = " ".join((f.get("name") or "") for f in
                     ((contract.get("request_fields") or []) +
                      (contract.get("response_fields") or []))).lower()
    iname = (contract.get("name") or "").lower()
    text = names + " " + iname
    role_hits = _role_dim_hits(contract)
    res = {}
    for dim, tokens, _dim_name, _cat in DOMAIN_FIELD_DIMS:
        if dim in role_hits:
            res[dim] = {"hit": role_hits[dim], "via_role": True, "reason": None}
            continue
        if not any(t in text for t in tokens):
            res[dim] = {"hit": None, "via_role": False, "reason": REASON_NO_FEATURE}
            continue
        hit = None
        for f in (contract.get("request_fields") or []):
            if any(t in (f.get("name") or "").lower() for t in tokens):
                hit = f
                break
        # token 命中了（响应侧字段名或接口名），但请求侧没有可传参的字段
        res[dim] = ({"hit": hit, "via_role": False, "reason": None} if hit else
                    {"hit": None, "via_role": False, "reason": REASON_RESPONSE_SIDE_ONLY})
    return res


def explain_zero_output(contract, dims):
    """给零产出的选测维度标注原因，返回 {dim: reason}。

    只解释「为什么没产出」，不决定是否产出——判定仍由各生成器负责。
    """
    resolved = _domain_dim_resolve(contract)
    out = {}
    for d in dims or []:
        if d not in IMPLEMENTED_DIMS:
            out[d] = REASON_NOT_IMPLEMENTED
        elif d in resolved:
            out[d] = resolved[d]["reason"] or REASON_NO_FEATURE
        else:
            # 有生成器但不走业务域判定（I/F/V/X/M）：触发条件在各自生成器内，
            # 走到这里说明契约没满足触发条件
            out[d] = REASON_NO_FEATURE
    return out


def gen_domain_field_cases(contract, base, action):
    """N/Q/W/S/T/O/P/R/U：按字段特征命中才生成，每个命中维度 2 条。

    命中口径有两条，语义优先于命名：字段名猜不出 token 时，若语义层把某个请求字段
    判成了等价角色（如 creater 判为 operator_identity），照样激活该维度。
    """
    out = []
    resolved = _domain_dim_resolve(contract)
    for dim, tokens, dim_name, cat in DOMAIN_FIELD_DIMS:
        r = resolved.get(dim) or {}
        hit = r.get("hit")
        if not hit:
            continue
        via_role = bool(r.get("via_role"))
        nm = hit["name"]
        src = "generator:domain_%s%s" % (dim, "(llm_role)" if via_role else "")
        # 语义激活的维度必须在 note 里写明依据：契约未必明说服务端会校验该引用存在性，
        # 这类断言比字段名直接命中 token 的情形弱一档，不能让读报告的人以为是契约写的
        role_note = ("判定依据来自语义层：%s（契约原文：「%s」）。契约未明说服务端会校验"
                     "该引用的存在性，被接受时请先人工确认是否为预期，再决定是否记缺陷。"
                     % (hit.get("llm_role"), (hit.get("llm_evidence") or "")[:80])
                     if via_role else "")
        if (hit.get("type") or "").lower() in ("bool", "boolean"):
            # 开关型字段没有"引用不存在"语义，注入 -1 是无意义噪音。
            # 改为验证两种取值都可用——开关类字段的典型缺陷是只实现了 true 分支。
            for val in (True, False):
                p = dict(base)
                p[nm] = val
                out.append(_mk("%s-SWITCH-%s" % (dim, str(val).lower()),
                               "%s·开关取值：%s=%s（应被接受）" % (dim_name, nm, str(val).lower()),
                               dim, cat, p, {"success": True}, action=action,
                               priority="P2", source=src,
                               note=role_note + "开关字段两种取值都应可用；只实现 true 分支会导致关不掉"))
            continue
        p = dict(base)
        p[nm] = _MISSING_ID
        out.append(_mk("%s-REF-01" % dim, "%s·引用不存在：%s=%s（应被拒绝）" % (dim_name, nm, _MISSING_ID),
                       dim, cat, p, {"success": False}, action=action,
                       priority="P2", source=src,
                       note=role_note + (_DOMAIN_NOTE % ("关联对象" if dim != "W" else "资源")))
                  )
        p2 = dict(base)
        p2[nm] = _INVALID_FMT
        out.append(_mk("%s-REF-02" % dim, "%s·引用格式非法：%s=%s（应被拒绝且错误信息明确）"
                       % (dim_name, nm, _INVALID_FMT), dim, cat, p2,
                       {"success": False}, action=action, priority="P2",
                       source=src,
                       note=role_note + "格式非法的引用若返回 500 或空错误，"
                                        "属于错误信息根因被掩盖（Top5 缺陷模式）"))
    return out


# ---------------------------------------------------------------- M 导出下载

# ---------------------------------------------------------------- L 跨客户端兼容

# 黑盒测试拿不到多版本 SDK，只能靠改 User-Agent 让服务端走到不同的客户端分支。
# 这是降级版：真跨 SDK 兼容（不同版本SDK的序列化差异、字段弃用兼容）仍需多端环境，
# 用例 note 里必须写明，不能让人以为是完整覆盖。
CLIENT_VARIANTS = {
    "移动端": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
    "PC端": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "小程序": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) MiniProgram",
}


def gen_client_variant_cases(contract, base, action):
    """L 维度：同一请求在不同客户端特征下的响应结构一致性。

    典型缺陷是服务端按 UA 分支返回不同字段集，导致某一端解析失败——这类缺陷只在
    某一端暴露，单端回归永远抓不到。没有响应字段时无法做结构对照，不生成。
    """
    if not (contract.get("response_fields") or []):
        return []
    names = " ".join((f.get("name") or "") for f in
                     ((contract.get("request_fields") or []) +
                      (contract.get("response_fields") or []))).lower()
    multi_end = any(t in names for t in ("pc", "mobile", "app", "mini", "wap", "h5", "ios", "android"))
    return [_mk("L01", "跨客户端兼容：同一请求在移动端/PC端/小程序三种客户端特征下，"
                       "响应字段集合应一致", "L", "兼容", dict(base), {"success": True},
                check="client_variant", check_args={"user_agents": CLIENT_VARIANTS},
                action=action, priority="P1", source="generator:client_variant",
                note="黑盒降级版：靠改 User-Agent 模拟客户端特征，无法覆盖真多版本 SDK 的"
                     "序列化差异与字段弃用兼容，那些仍需多端环境。"
                     "服务端按 UA 分支返回不同字段集时，只有对应那端会解析失败，"
                     "单端回归抓不到——%s"
                     % ("契约字段含多端特征（pc/mobile/app 等），该风险更高。"
                        if multi_end else "本接口未发现多端特征字段，命中概率较低。"))]


def gen_export_cases(contract, base, action, op_type):
    """M 维度：仅导出/下载类接口。断言的是「响应里得给出可用的下载凭据」。"""
    if str(op_type or "") != "导出":
        return []
    out = []
    out.append(_mk("M01", "导出下载：响应应返回可用的下载标识（URL/文件 token）",
                   "M", "导出下载", dict(base), {"success": True},
                   check="export_credential", action=action, priority="P1",
                   source="generator:export",
                   note="导出成功的判据不是 result=true，而是拿到能真正下载的东西；"
                        "返回空 URL 或过期 token 都是缺陷"))
    out.append(_mk("M02", "导出下载：重复导出两次应各自成功且互不影响",
                   "M", "导出下载", dict(base), {"success": True},
                   check="idempotent", check_args={"times": 2},
                   action=action, priority="P2", source="generator:export",
                   note="导出类接口常见缺陷是第二次导出返回上一次的文件或空文件"))
    return out


# ---------------------------------------------------------------- 统一入口

def run_all(contract, base, action, ctx=None):
    """补齐维度生成入口，返回用例列表。ctx 提供 op_type 等上下文。"""
    ctx = ctx or {}
    out = []
    out += gen_encoding_cases(contract, base, action)
    out += gen_combination_cases(contract, base, action)
    out += gen_trace_cases(contract, base, action)
    out += gen_batch_cases(contract, base, action)
    out += gen_domain_field_cases(contract, base, action)
    out += gen_client_variant_cases(contract, base, action)
    out += gen_export_cases(contract, base, action, ctx.get("op_type"))
    return out
