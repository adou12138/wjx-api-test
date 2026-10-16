# -*- coding: utf-8 -*-
"""接口适用性自动判定：从契约推导「哪些维度适用」，并自动生成 COVERAGE_PLAN 与 N/A 证明。

解决的核心问题：coverage_gate（反静默缺漏门禁）依赖人手声明 COVERAGE_PLAN，
不声明就等于门禁失效、"做少了"照样静默产出完整报告。本模块把 applicability_matrix.md 的
判断逻辑代码化，让「契约 → 适用维度 → COVERAGE_PLAN」自动完成，门禁即可默认生效。

产出：
  {
    "op_type":"创建", "business_domain":"资源管理", "is_intl":false,
    "dimensions": {"A":"必测", "AA":"不适用", ...},
    "coverage_plan": ["A","B","C",...],      # 必测 + 选测，直接喂给 coverage_gate
    "na_proofs": [{"dim":"AA","reason":"..."}],  # N/A 自动证明（第 1.5 步要求列出证明）
    "notes": [...]
  }
维度编号对照见 framework.md §1：A 鉴权权限 / B 参数校验 / C 字段语义真值 / D 业务主链路 /
E 状态机 / F 组合交叉 / G 安全深化 / H 错误处理 / I 编码国际化 / J 性能稳定 / K 契约测试 /
L 兼容性 / M–Z 业务域专项 / AA 分页 / AE 认证传输补充 / AB 前端口径 / AC 幂等唯一 /
AD 事件回调 / AF 业务场景（由契约生成的业务语义用例，默认关闭、启用后必测）。
"""
import os
import re

ALL_DIMS = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L",
            "M", "N", "O", "P", "Q", "R", "S", "T", "U", "V", "W", "X", "Y", "Z",
            "AA", "AE", "AB", "AC", "AD", "AF"]

# 操作类型识别（顺序敏感：更具体的先匹配）
OP_RULES = [
    ("导入", ["导入", "import", "批量导入"]),
    ("导出", ["导出", "下载", "export", "download"]),
    ("删除", ["删除", "移除", "清空", "delete", "remove"]),
    ("修改", ["更新", "修改", "编辑", "设置", "保存", "update", "edit", "modify", "save"]),
    ("创建", ["创建", "新增", "添加", "新建", "复制", "create", "add", "new"]),
    ("查询", ["查询", "获取", "详情", "列表", "读取", "搜索", "list", "get", "query", "detail", "search"]),
]

# 业务域专项识别（命中即把该维度提升为选测）。
# 注意：只按「接口名 + 字段名」匹配，不扫描述/备注——描述里出现"title标签""短链接编号"
# 会把 N(层级) / P(分享) 误命中，进而把无关维度塞进计划、让门禁产生假缺口。
DOMAIN_RULES = [
    ("M", ["导出", "下载", "excel", "download", "export"]),
    ("N", ["层级", "部门", "通讯录", "文件夹", "树形", "父级", "parent"]),
    ("O", ["发送", "通知", "提醒", "推送", "短信", "邮件", "notify"]),
    ("P", ["分享", "share"]),
    ("Q", ["评价", "评分", "关系", "evaluat"]),
    ("R", ["报告", "report"]),
    ("S", ["分类", "容器", "分组", "category"]),
    ("T", ["账号", "成员", "用户", "身份", "member", "user"]),
    ("U", ["导入", "import"]),
    ("W", ["附件", "文件", "上传", "图片", "attachment", "file"]),
    ("X", ["批量", "batch"]),
    ("Y", ["详情", "明细", "detail"]),
]

# 操作类型 -> (必测, 选测)。未列出的维度一律判「不适用」并给出自动证明。
OP_MATRIX = {
    "创建": (
        ["A", "B", "C", "D", "H", "K", "Y", "AB", "AC", "AE"],
        ["E", "F", "G", "I", "J", "L", "V", "W", "Z", "AD", "N", "Q"],
    ),
    "修改": (
        ["A", "B", "C", "D", "E", "H", "K", "Y", "AB", "AC", "AE"],
        ["F", "G", "I", "J", "L", "V", "W", "Z", "AD", "N", "Q"],
    ),
    "删除": (
        ["A", "B", "D", "E", "H", "K", "AB", "AC", "AE"],
        ["C", "F", "G", "I", "J", "V", "AD", "N"],
    ),
    "查询": (
        ["A", "B", "C", "D", "H", "K", "Y", "AB", "AE"],
        ["F", "G", "I", "J", "L", "V", "W", "Z", "AA", "N", "T"],
    ),
    "导出": (
        ["A", "B", "C", "D", "H", "K", "M", "AB", "AE"],
        ["AA", "F", "G", "I", "J", "V", "W", "Z"],
    ),
    "导入": (
        ["A", "B", "C", "D", "H", "K", "U", "AB", "AC", "AE"],
        ["F", "G", "I", "J", "V", "W", "X", "Z", "AD"],
    ),
}

# N/A 自动证明：不适用维度的理由（按维度给默认理由，再由操作类型细化）
NA_REASON = {
    "AA": "非列表/分页接口，无页码与条数边界可测",
    "AB": "该接口无对应前端页面操作，无页面基线可比对",
    "AC": "非写接口，不存在重复提交与业务唯一性",
    "AD": "该操作不触发事件/回调，无推送可验",
    "AE": "认证与传输契约由公共层统一处理，本接口无特殊约定",
    "C": "无落库数据可回读，字段语义真值不适用",
    "E": "不产生状态流转，无状态机可测",
    "F": "单参数接口，无字段组合交叉可测",
    "G": "无内容/URL/附件类输入，安全深化场景有限",
    "I": "无多语言文案，编码与国际化仅做轻量校验",
    "J": "性能与并发稳定性为选测项（非压测）",
    "L": "兼容性（协议/SDK/上游）非本轮范围",
    "M": "不涉及导出下载",
    "N": "非层级资源接口",
    "O": "无异步通知/发送",
    "P": "无分享链接参数",
    "Q": "无多角色关系",
    "R": "非报告分享",
    "S": "非容器/分类管理",
    "T": "非账号身份信息接口",
    "U": "非批量导入",
    "V": "请求标识与追踪由公共层保证，本轮不单列",
    "W": "无文件/附件字段",
    "X": "非批量操作",
    "Y": "无复杂结构返回",
    "Z": "国内版测试，国际版维度 N/A（非国际版）",
    "AF": "未启用业务场景补充生成（默认关闭）；启用后 AF 为必测维度，受覆盖率门禁保护",
}

INTL_HINTS = ["国际版", "海外", "多语言", "国际化", "i18n", "language", "locale", "currency", "timezone"]


def _word_hit(keyword, text):
    """英文关键词按词边界匹配，中文仍用子串（W30）。

    误命中实测：add→address、get→target/forget、new→renew——把「地址管理」判成
    创建类、「获取目标」判成查询类，后续幂等/状态机用例整块错向。"""
    if not keyword or not text:
        return False
    if re.match(r"^[a-z][a-z0-9_-]*$", keyword):
        return re.search(r"(?:^|[^a-z0-9])%s(?:[^a-z0-9]|$)" % re.escape(keyword), text) is not None
    return keyword in text


def _detect_op_type(contract):
    """操作类型识别。

    【W30】英文关键词词边界匹配；全部未命中时**不再默认「查询」**——返回 None，
    由调用方记「操作类型待确认」（创建/删除被误判查询会让幂等/状态机整块 N/A 漏测，
    宁可待确认不可错向）。仅当接口名含明确的列表/分页信号时才保守判查询。"""
    name = (contract.get("name") or "") + " " + str(contract.get("action") or "")
    low = name.lower()
    for op, keys in OP_RULES:
        for k in keys:
            if _word_hit(k, low):
                return op
    # 无命中：列表/分页强信号才判查询，否则待确认
    if _is_list_query(contract):
        return "查询"
    return None


def _is_list_query(contract):
    """查询类接口细分：列表 or 详情。"""
    name = (contract.get("name") or "").lower()
    if any(k in name for k in ("列表", "清单", "list", "分页")):
        return True
    for f in contract.get("response_fields", []) or []:
        n = (f.get("name") or "").lower()
        s = f.get("semantics") or []
        if n in ("list", "items", "total", "rows", "records", "count") or "count" in s or "page" in s:
            return True
    return False


def _detect_domains(contract):
    """只按「接口名 + 字段名」判定业务域，避免描述里的词（如"标签""短链接"）造成误命中。"""
    text = (contract.get("name") or "") + " " + " ".join(
        (f.get("name") or "") for f in
        (contract.get("request_fields", []) or []) + (contract.get("response_fields", []) or [])
    )
    text = text.lower()
    hits = []
    for dim, keys in DOMAIN_RULES:
        if any(k.lower() in text for k in keys):
            hits.append(dim)
    return hits


def _detect_intl(contract):
    if any(k in (contract.get("name") or "") for k in INTL_HINTS):
        return True
    for f in (contract.get("request_fields", []) or []) + (contract.get("response_fields", []) or []):
        if set(f.get("semantics") or []) & {"i18n", "tz", "currency"}:
            return True
    return False


def judge(contract, with_business=None):
    """主入口：判定适用性，返回 dimensions / coverage_plan / na_proofs。

    with_business=True 时把 AF（业务场景）列入必测。AF 用例由 scripts/business_scenarios.py
    生成，未启用时 AF 标「不适用」且不进硬门禁——避免"没启用却被判覆盖率缺口"的假警报。
    未显式传参时读环境变量 WJX_WITH_BUSINESS，便于整条流水线启用而不改代码。
    """
    if with_business is None:
        with_business = os.environ.get("WJX_WITH_BUSINESS", "").strip().lower() not in ("", "0", "false", "no")
    op = _detect_op_type(contract)
    # 【W30 配套】操作类型待确认：不默认查询（错向 = 幂等/状态机整块漏测），
    # 用「查询」的保守基线做计划，但在 coverage_plan 留痕供人工复核。
    op_unconfirmed = op is None
    if op_unconfirmed:
        op = "查询"
    intl = _detect_intl(contract)
    domains = _detect_domains(contract)
    required, optional = OP_MATRIX.get(op, OP_MATRIX["查询"])

    required = list(required)
    optional = list(optional)

    if with_business and "AF" not in required:
        required.append("AF")

    # 列表类查询：AA 提升为必测
    if op == "查询" and _is_list_query(contract):
        if "AA" not in required:
            required.append("AA")
        if "AA" in optional:
            optional.remove("AA")

    # 【W30 配套】操作类型未确认时：幂等/终态（E 维）降为选测——在错误假设上
    # 硬测状态机比漏测更糟（假 FAIL 淹没真缺陷）。
    if op_unconfirmed and "E" in required:
        required.remove("E")
        if "E" not in optional:
            optional.append("E")

    # 业务域专项命中：提升到选测（若本应不适用）
    # 【W29 反门外】业务专项恰是「发现更多 bug」的高价值面，恒在门禁外=漏测只在
    # uncovered_optional 点名（没人看）。**请求侧字段命中**的业务域维度提升为必测
    # 入硬门禁——能构造请求验证，命中即应测；仅 response_side_only/no_feature 的
    # 保持选测（无法构造请求，硬测只会刷假缺口）。
    from dim_generators import _domain_dim_resolve
    try:
        _resolved = _domain_dim_resolve(contract)
    except Exception:
        _resolved = {}
    for d in domains:
        if d not in required and d not in optional and d in ALL_DIMS:
            info = _resolved.get(d) or {}
            if info.get("hit"):
                required.append(d)
            else:
                optional.append(d)

    # 国际版：Z 提升为必测；否则明确标 N/A（非国际版）
    if intl:
        if "Z" not in required:
            required.append("Z")
        if "Z" in optional:
            optional.remove("Z")
    else:
        if "Z" in required:
            required.remove("Z")
        if "Z" in optional:
            optional.remove("Z")

    dims, notes = {}, []
    for d in ALL_DIMS:
        if d in required:
            dims[d] = "必测"
        elif d in optional:
            dims[d] = "选测"
        else:
            dims[d] = "不适用"

    # COVERAGE_PLAN 只锁「必测」：门禁对必测维度缺用例才报缺口。
    # 选测单独列为建议（optional_plan），不进硬门禁——否则选测项没用例会刷出一堆假警报。
    plan = [d for d in ALL_DIMS if dims.get(d) == "必测"]
    optional_plan = [d for d in ALL_DIMS if dims.get(d) == "选测"]

    na_proofs = []
    for d in ALL_DIMS:
        if dims.get(d) != "不适用":
            continue
        reason = NA_REASON.get(d, "该操作类型不涉及此维度")
        if d == "AA" and op == "创建":
            reason = "创建接口无列表数据，分页筛选不适用（applicability_matrix §2 反例）"
        if d == "M" and op == "创建":
            reason = "创建后无下游数据，导出下载不适用（applicability_matrix §2 反例）"
        if d == "E" and op == "查询":
            reason = "查询接口不改变资源状态，状态机不适用（applicability_matrix §2 反例）"
        if d == "Z" and not intl:
            reason = "N/A（非国际版）：未满足国际版启用条件，多语言/时区/多币种维度不加载"
        na_proofs.append({"dim": d, "reason": reason})

    if dims.get("AB") == "必测":
        notes.append("AB 为必测：需先取页面基线（references/ui_baseline.md），无基线不得凭空填预期值")
    if intl:
        notes.append("判定为国际版场景：Z 及 §1.3 国际版扩展维度已加载")
    else:
        notes.append("默认按国内版处理：国际版维度统一标 N/A（非国际版），不计入覆盖率分母")

    if op_unconfirmed:
        notes.append("操作类型待确认：接口名未命中任何操作关键词（也不含列表信号），"
                     "按「查询」保守基线出计划，E 维（终态/幂等）已降选测——"
                     "请人工确认真实操作类型后重判，错向的幂等/状态机用例会成片假 FAIL")

    return {
        "op_type": op,
        "op_unconfirmed": op_unconfirmed,
        "is_list_query": _is_list_query(contract) if op == "查询" else False,
        "business_domains": domains,
        "is_intl": intl,
        "dimensions": dims,
        "coverage_plan": plan,
        "optional_plan": optional_plan,
        "na_proofs": na_proofs,
        "notes": notes,
    }
