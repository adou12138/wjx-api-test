# -*- coding: utf-8 -*-
"""用例生成器：契约 → 声明式用例（不再手写 Python main）。

流水线：解析契约 → 字段语义推断 → 适用性自动判定（出 COVERAGE_PLAN 与 N/A 证明）
      → 19 条缺陷模式匹配生成 → 字段级变异生成（必填缺失/类型错/枚举越界/长度边界/注入）
      → 输出 cases JSON（schema 见 references/case_schema.md）

产物直接喂给 case_runner.py 执行，并自动把 COVERAGE_PLAN 装进覆盖率门禁，
从而让「做少了」被代码拦住，而不是靠人记。

用法：
  python case_generator.py --contract <契约路径> [--out cases.json] [--action 1000107]
  python case_generator.py --contract <路径> --print     # 只打印摘要不落盘
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from contract_parser import load_contract          # noqa: E402
from field_semantics import annotate, suggest_for_contract  # noqa: E402
from applicability import judge                     # noqa: E402
from companion_scan import resolve as resolve_companion  # noqa: E402
import dim_generators                                    # noqa: E402
import llm_fact_cases                                    # noqa: E402
import pattern_engine                               # noqa: E402
import check_key                                    # noqa: E402
import discovery_profile                            # noqa: E402

# 超长用例上限的**默认档**数值。真正的生效值走 discovery_profile（发现档可放宽），
# 这里保留常量是为了让 check_key._MAX_LEN_CAP 与之一致（单测守这条不变量）。
BIG_CAP = discovery_profile.DEFAULTS["big_cap"]
TYPE_BAD = {
    "int": "abc", "integer": "abc", "number": "abc", "float": "abc", "double": "abc",
    "bool": "not_a_bool", "boolean": "not_a_bool",
    "string": 12345,
    "array": "not_a_list", "list": "not_a_list", "object": "not_an_object",
}

# string 字段的类型错误形态池。旧版所有 string 字段一律填 12345，导致
# 「creater=12345 / title=12345 / html_content=12345」这批用例打的是同一个参数校验
# 分支，产生一批重复的 PASS——既冗余，也测不到后端对其他非法类型的处理差异
# （有的后端把数字隐式转成字符串，有的直接报类型错，两者是可区分的实现缺陷）。
# 按字段序轮流取用，既不增加用例数，又让每条打在不同分支上。
_STRING_TYPE_ERRORS = [12345, ["type_error"], {"type_error": True}]
_STRING_TOKENS = ("string", "str", "text", "varchar", "char")
# 数值语义的 string 字段固定用数字：数字→数值字段的隐式转换是最容易出问题的分支
_NUMERIC_SEMS = {"id", "enum", "count", "page", "size", "amount"}


def _long_value(field, n):
    """造长度为 n 的**业务形态**内容，替代旧版的 `'x' * n`。

    `'x'*n` 只能探到「长度校验」这一个分支；业务形态的内容（标签结构、URL、标题短语）
    顺带覆盖字符集、转义、截断与存储——同样是一条用例，探测面更宽，且被接受时
    能回读验证内容是否完整（纯重复字符无法区分「截断」与「正常存储」）。

    刻意用 **ASCII 业务形态**而不是中文：中文的「按字符 vs 按字节」长度口径不一致，
    100 个中文字符是 300 字节，会让「恰好 max_len 应成功」这条边界用例在按字节校验的
    后端上必然失败——那是长度口径差异，不是缺陷，混进来会污染边界结论。
    """
    sem = set(field.get("semantics") or [])
    if sem & {"html", "content"}:
        unit = "<p>overlong</p>"
    elif sem & {"url"}:
        unit = "https://example.com/seg"
    elif sem & {"name"}:
        unit = "Overlong title "
    else:
        unit = "overlong_value_"
    if n <= 0:
        return ""
    return (unit * (n // len(unit) + 1))[:n]


# JSON 值对各类型 token 的"是否合法"。复合类型要选一个**对所有成员都非法**的值，
# 否则注入的值恰好命中某个分支被判合法 → 得到"期望拒绝却成功"的假缺陷。
_PRIMITIVE_VALID = {
    "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "float": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "double": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "bool": lambda v: isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "string": lambda v: isinstance(v, str),
    "str": lambda v: isinstance(v, str),
    "text": lambda v: isinstance(v, str),
    "varchar": lambda v: isinstance(v, str),
    "char": lambda v: isinstance(v, str),
    "array": lambda v: isinstance(v, list),
    "list": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}

# 候选非法值，按"结构性越界程度"排序：容器类优先（对 null/bool/数值/字符串全部非法）。
_BAD_CANDIDATES = ([], {}, "__type_error__", 12345, True)


def type_tokens(ftype):
    """把类型串切成类型 token（与 check_key._declares_typed 同一口径）。

    ``null / boolean`` → ``['null', 'boolean']``；``integer / string(int32)`` →
    ``['integer', 'string', 'int32']``；``问卷id`` → ``['id']``（中文被切掉）。
    """
    return [t for t in re.split(r"[^a-z0-9]+", str(ftype or "").lower()) if t]


def _compound_bad_value(tokens):
    """复合类型：挑一个对**所有**成员都非法的值。

    只要存在某个成员类型能接受该值，就不能用它（会得到"期望拒绝却成功"的假缺陷，
    把合法的联合类型取值误报成缺陷）。所有候选都至少被一个成员接受时返回 None，
    宁可跳过也不造假缺陷。
    """
    for tok in tokens:
        if tok not in _PRIMITIVE_VALID:
            return None  # 有无法判定的成员 → 不冒险
    for cand in _BAD_CANDIDATES:
        if all(not _PRIMITIVE_VALID[t](cand) for t in tokens):
            return cand
    return None


def type_bad_for(field, ftype, idx=0):
    """按字段类型（含复合）与语义序位挑类型错误值。

    支持复合类型：契约写 ``null / boolean``、``integer / string(int32)`` 时，
    按成员 token 选一个对所有分支都非法的值；对单一 string 字段仍按序轮换形态
    （避免所有 string 字段统一 12345 打在同一校验分支）。
    """
    toks = [t for t in type_tokens(ftype) if t in TYPE_BAD]
    if not toks:
        return None
    if len(toks) == 1 and toks[0] in _STRING_TOKENS:
        if set(field.get("semantics") or []) & _NUMERIC_SEMS:
            return 12345
        return _STRING_TYPE_ERRORS[idx % len(_STRING_TYPE_ERRORS)]
    if len(toks) == 1:
        return TYPE_BAD[toks[0]]
    return _compound_bad_value(toks)


def _mk(cid, name, dim, category, params, expect, **kw):
    case = {
        "id": cid, "name": name, "dimension": dim, "category": category,
        "action": kw.pop("action", None), "applicability": kw.pop("applicability", "适用"),
        "params": params, "expect": expect,
        "priority": kw.pop("priority", "P1"), "source": kw.pop("source", ""),
        "enabled": kw.pop("enabled", True), "needs": kw.pop("needs", ""),
        "note": kw.pop("note", ""),
        "risk": kw.pop("risk", "medium"),
        "evidence": kw.pop("evidence", []),
        "cleanup": kw.pop("cleanup", {"required": False, "actions": []}),
    }
    case.update(kw)
    return case


# 机械探测用例的 ID 前缀：由「字段 × 固定算子」笛卡尔展开或载荷清单遍历产生，
# 不带业务语义。分层只影响报告统计口径，不影响执行与门禁。
TIER_PROBE_PREFIXES = ("B-MISS-", "B-NULL-", "B-OVER-", "B-TYPE-", "B-LEN-", "B-BIG-", "B-COMBO-", "G-")


def _tier_of(case):
    """用例分层：probe / business / manual。

    probe    机械探测——「字段 × 算子」展开与载荷遍历。有覆盖价值（确实探到了某类校验
             是否存在），但不代表业务场景被测过。
    business 业务与语义驱动——往返验证、交叉验证、状态机、越权矩阵、业务场景等。
    manual   待人工补充的占位（TODO-* / needs=manual）。

    为什么必须分层：机械展开会把「用例数」和「维度覆盖率」撑高，让人误判业务覆盖充分。
    分层的目的不是删掉 probe 用例（它们照常执行、照常判 PASS/FAIL），而是让报告能
    单独给出「业务覆盖了多少」这个真实数字。
    """
    cid = str(case.get("id") or "")
    if cid.startswith("TODO-") or case.get("needs") == "manual":
        return "manual"
    if cid.startswith(TIER_PROBE_PREFIXES):
        return "probe"
    return "business"


def tier_stats(cases):
    """按 tier 统计用例数，供报告给出「业务覆盖 vs 机械探测」的真实构成。"""
    out = {"business": 0, "probe": 0, "manual": 0}
    for c in cases:
        t = c.get("tier") or _tier_of(c)
        out[t] = out.get(t, 0) + 1
    return out


def _enrich_cases(cases, contract):
    """为旧生成分支补齐智能元数据，保持原有字段和断言方向不变。"""
    source = contract.get("source") or "contract"
    for case in cases:
        case.setdefault("tier", _tier_of(case))
        case.setdefault("risk", "high" if case.get("priority") == "P0" else "medium")
        case.setdefault("evidence", [source])
        case.setdefault("cleanup", {"required": bool(case.get("expect", {}).get("success")), "actions": []})
    return cases


def _resp_contract_keys(contract):
    return [f["name"] for f in (contract.get("response_fields") or []) if f.get("required")]


def gen_field_mutations(contract, base, action):
    """字段级变异：必填缺失 / 类型错 / 长度边界 / 内容注入。"""
    out = []
    str_seq = 0
    for f in (contract.get("request_fields") or []):
        name, ftype = f["name"], (f.get("type") or "").lower()
        sem = f.get("semantics") or []

        # 1) 必填缺失 → 应被拒绝
        if f.get("required"):
            p = dict(base)
            p.pop(name, None)
            out.append(_mk("B-MISS-%s" % name, "必填缺失：%s（应被拒绝）" % name, "B",
                           "参数校验-必填", p,
                           {"success": False, "err_keywords": [name]},
                           action=action, priority="P0", source="generator:required_missing",
                           evidence=["contract:required"], note="必填项缺失必须被拒绝；错误文案应指明字段"))

        # 可选/可空字段的反向校验四态：合法缺省值不应被过度拒绝。
        if not f.get("required") or f.get("nullable"):
            for state, value in (("missing", None), ("null", None), ("empty_string", "")):
                p = dict(base)
                if state == "missing":
                    p.pop(name, None)
                else:
                    p[name] = value
                out.append(_mk("B-OVER-%s-%s" % (name, state), "可选字段%s态：%s（不应过度校验）" % (state, name), "B",
                               "参数校验-过度校验", p, {"success": True}, action=action,
                               priority="P1", source="generator:over_validation", assertion_mode="over_validation",
                               mutation_state=state,                                evidence=["contract:optional_or_nullable"],
                               cleanup={"required": True, "actions": ["delete_fixture"]},
                               note="合法可选/可空形态被拒绝时记录过度校验缺陷"))

        # 2) 类型错误 → 应被拒绝。字符串字段轮流取不同非法形态，避免全部打同一分支
        bad = type_bad_for(f, ftype, str_seq)
        if bad is not None:
            if ftype in _STRING_TOKENS:
                str_seq += 1
            p = dict(base)
            p[name] = bad
            out.append(_mk("B-TYPE-%s" % name, "类型错误：%s=%r（应被拒绝）" % (name, bad), "B",
                           "参数校验-类型", p,
                           {"success": False, "err_keywords": [name]},
                           action=action, priority="P0", source="generator:type_error",
                           evidence=["contract:type"], cleanup={"required": True, "actions": ["delete_fixture"]}))

        # 3) 长度边界：超长应被拒绝；恰好等于上限应成功。
        #    生效上限走发现档：discovery 放宽到数万，让大字段也能做真边界用例。
        cap = discovery_profile.get("big_cap")
        if f.get("max_len") and f["max_len"] <= cap:
            p = dict(base)
            p[name] = _long_value(f, f["max_len"] + 1)
            out.append(_mk("B-LEN-OVER-%s" % name, "长度超限：%s %d+1 字符（应被拒绝）" % (name, f["max_len"]),
                           "B", "参数校验-边界", p,
                           {"success": False, "err_keywords": [name]},
                           action=action, priority="P0", source="generator:length_over",
                           note="内容用业务形态填充（非重复 x）：顺带覆盖转义与截断，"
                                "且刻意用 ASCII 以免字符/字节口径差异污染边界判定"))
            p2 = dict(base)
            p2[name] = _long_value(f, f["max_len"])
            out.append(_mk("B-LEN-EDGE-%s" % name, "长度边界内：%s 恰好 %d 字符（应成功）" % (name, f["max_len"]),
                           "B", "参数校验-边界", p2, {"success": True},
                           action=action, priority="P1", source="generator:length_edge",
                           note="边界内值被拒即为边界判断错误（off-by-one）。"
                                "若后端按字节校验而本用例按字符构造，会在此暴露长度口径差异——"
                                "记「待确认」而非直接判缺陷"))
        elif f.get("max_len") and discovery_profile.get("overlong_probe"):
            # 大字段（max_len 超出上限，如 html_content=200000）：够不到 max_len+1，
            # 退而构造**一条有界的大内容健壮性探针**——探"大体积合法内容后端吞不吞得下"
            # （5xx / 静默截断 / 超时）。这不是边界用例，故预期为成功，且只探一次。
            p = dict(base)
            p[name] = _long_value(f, cap)
            out.append(_mk("B-BIG-%s" % name,
                           "大内容健壮性：%s 传 %d 字符合法内容（应成功，不 5xx/不静默截断）" % (name, cap),
                           "B", "参数校验-大内容", p, {"success": True},
                           action=action, priority="P1", source="generator:big_content",
                           note="发现档专属：max_len 超出常规上限的大字段（如富文本）无法构造"
                                "边界+1 用例，此条只探大体积合法内容是否被正常接受。"
                                "5xx 或返回成功但回读内容被截断即缺陷；超时先归 ENV/网络"))

        # 3b) 长度下边界（min_len）：低于下限应被拒绝、恰好等于下限应成功。
        #     与 max_len 同样刻意用 ASCII 业务形态，避免中文按字符/按字节的口径差异污染边界判定。
        #     仅在下限 >= 2 时构造"低于下限"用例：ml-1 >= 1 才是明确的"非空但过短"；
        #     ml == 1 时低于值即空串，与可选空值/必填缺失语义重叠，交给 over_validation/missing 覆盖。
        #     "恰好等于下限应成功"仅在字段无枚举时构造——有枚举时业务形态字符串不在枚举内，
        #     会被后端以枚举理由拒绝，那不是边界缺陷，构造了就是假缺陷（分母侧同理，below_minimum
        #     只对 min_len>=2 声明，与这里的 B-LEN-UNDER 严格对齐）。
        if f.get("min_len"):
            ml = int(f["min_len"])
            if ml >= 2:
                p = dict(base)
                p[name] = _long_value(f, ml - 1)
                out.append(_mk("B-LEN-UNDER-%s" % name, "长度低于下限：%s %d-1 字符（应被拒绝）" % (name, ml),
                               "B", "参数校验-边界", p,
                               {"success": False, "err_keywords": [name]},
                               action=action, priority="P0", source="generator:length_under",
                               note="低于声明的最小长度应被拒绝；ASCII 业务形态填充，避免字符/字节口径差异"))
            if not f.get("enum"):
                p2 = dict(base)
                p2[name] = _long_value(f, ml)
                out.append(_mk("B-LEN-MIN-%s" % name, "长度等于下限：%s 恰好 %d 字符（应成功）" % (name, ml),
                               "B", "参数校验-边界", p2, {"success": True},
                               action=action, priority="P1", source="generator:length_min",
                               note="恰好等于最小长度应被接受（off-by-one）；后端按字节校验会在此暴露口径差异，记待确认"))

        # 4) 内容类字段的安全注入用例已升级为清单驱动：见 gen_security_payload_cases()
        #    （assets/security_payloads.json，按语义标签选载荷），此处不再内联生成。
    return out


# 机械组合覆盖上限（v4.25.0）：参与组合的字段代表数 + 组合用例总数，防 n² 爆炸。
COMBO_MAX_FIELDS = 8
COMBO_MAX_CASES = 24


def gen_combination_cases(contract, base, action):
    """机械组合覆盖（v4.25.0）：多个字段同时取各自**单个就非法**的值 → 应被拒绝。

    只做 sound 的组合：每个参与字段取它单独提交就应被拒的值（类型错），组合后
    **必然应被拒绝**——不猜互斥/条件必填（契约极少声明，猜了就是假缺陷）。测的是
    "后端是否逐个校验所有参数"，而非只校验第一个就返回。

    有界：字段代表数 + 组合总数双上限；不要求错误提及特定字段（后端可能只报第一个），
    只断言"应被拒绝"。B-COMBO-* 归 probe 层、走来源型身份证、**不进对账分母**。
    """
    out = []
    fields = [f for f in (contract.get("request_fields") or [])
              if f.get("name") and type_bad_for(f, (f.get("type") or "").lower()) is not None]
    reps = fields[:COMBO_MAX_FIELDS]
    n = 0
    for i in range(len(reps)):
        for j in range(i + 1, len(reps)):
            if n >= COMBO_MAX_CASES:
                return out
            fa, fb = reps[i], reps[j]
            na, nb = fa["name"], fb["name"]
            ba = type_bad_for(fa, (fa.get("type") or "").lower())
            bb = type_bad_for(fb, (fb.get("type") or "").lower())
            if ba is None or bb is None:
                continue
            p = dict(base)
            p[na] = ba
            p[nb] = bb
            out.append(_mk("B-COMBO-%s__%s" % (na, nb),
                           "组合非法：%s 与 %s 同时取非法值（应被拒绝）" % (na, nb),
                           "B", "参数校验-组合", p, {"success": False},
                           action=action, priority="P2", source="generator:combination",
                           note="两字段各自单独即非法，组合后应被拒绝；测后端是否逐个校验所有"
                                "参数（而非只报第一个）。不要求错误提及特定字段，只断言应被拒绝。"))
            n += 1
    return out


SECURITY_PAYLOADS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "assets", "security_payloads.json")


def _load_security_payloads():
    """加载安全载荷清单；文件缺失/损坏时返回 None（G 维度退回人工，不静默伪造用例）。"""
    try:
        with open(SECURITY_PAYLOADS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (IOError, OSError, ValueError):
        return None


_TYPE_TOKENS = ("string", "str", "text", "varchar", "char")


def _payload_categories_for(field, lib):
    """按各载荷类别的 applies_to 为字段匹配类别（去重保序）。

    清单是唯一事实来源：类别只要写对 applies_to 就自动生效，不必改本文件——
    否则「加了类别却没人接线」等于静默失效（正是本 skill 最防的失败模式）。
    applies_to 条目分两种：类型名（string/str/text/varchar/char）按字段类型判定，
    语义标签（html/url/name…）与字段 semantics 求交。

    只对字符串型字段注入——清单内载荷全是字符串，给 int/bool 注入只会得到
    类型错误，那不是安全缺陷而是参数校验，避免污染 G 维度结论。
    """
    ftype = (field.get("type") or "").lower()
    if ftype not in _TYPE_TOKENS and ftype != "":
        return []
    sem = set(field.get("semantics") or [])
    out = []
    for cat in (lib.get("categories") or []):
        cid = cat.get("id")
        if not cid or cid in out:
            continue
        for token in (cat.get("applies_to") or []):
            if token in _TYPE_TOKENS or token in sem:
                out.append(cid)
                break
    return out


# 注入风险只存在于「内容会被存储 / 回显 / 拼接 / 渲染」的字段上。
CONTENT_SEMS = {"html", "name", "url", "content"}
NON_CONTENT_SEMS = {"id", "enum", "bool", "count", "page", "size", "status",
                    "time", "amount", "currency", "tz", "i18n"}
_CONTENT_HINT = re.compile(
    r"content|html|text|body|desc|remark|title|name|intro|detail|summary|"
    r"正文|内容|描述|备注|标题|说明|简介|详情", re.I)


def _is_content_field(field):
    """该字段是否值得注入安全载荷。

    判据是「字段内容会不会被存储 / 回显 / 拼接 / 渲染」。标识类字段
    （creater / page_type / publish / 各类 id）即使被注入并成功落库也不会产生注入风险，
    注入它们只会得到一批重复的 PASS：实测单个接口可产生 50 条载荷用例，占全部用例 38%，
    把 G 维度和报告撑大，稀释真正的问题。

    保守原则：**判定不了时按内容型处理**（宁可多测，不可漏测安全）；
    被跳过的字段一律记入 payload_skipped 留痕，不静默消失。
    """
    sem = set(field.get("semantics") or [])
    if sem & NON_CONTENT_SEMS:
        return False
    if sem & CONTENT_SEMS:
        return True
    name = (field.get("name") or "")
    doc = "%s %s" % (field.get("desc", ""), field.get("remark", ""))
    return bool(_CONTENT_HINT.search(name + " " + doc))


def gen_security_payload_cases(contract, base, action, skipped=None):
    """G 维度：清单驱动的安全载荷用例（assets/security_payloads.json）。

    替代旧版单条 script 注入：按字段语义标签/类型挑选载荷类别，每类每字段
    最多取 max_payloads_per_field_per_category 条，防止用例爆炸。
    断言方向：应被拦截（success=False + 错误关键词）；被原样接受即缺陷，
    被正确拦截记「符合预期」非缺陷（陷阱3）。

    skipped：可选列表，收集被判定为「非内容型」而未注入载荷的字段，供报告留痕。
    """
    lib = _load_security_payloads()
    if not lib:
        return []
    cats = {c["id"]: c for c in lib.get("categories", [])}
    # 生效条数走发现档：discovery 可放宽（如 2 → 4，含"字面 + 变体绕过"两种样本）；
    # 默认档 None → 沿用清单自带 limits，与接入前一致。
    lib_limit = int((lib.get("limits") or {}).get("max_payloads_per_field_per_category", 2))
    limit = discovery_profile.get("payload_per_field_per_category") or lib_limit
    out = []
    for f in (contract.get("request_fields") or []):
        name = f["name"]
        if not _is_content_field(f):
            if skipped is not None:
                skipped.append({
                    "field": name,
                    "semantics": f.get("semantics") or [],
                    "reason": "标识/结构类字段，内容不被存储或渲染，注入无风险含义",
                })
            continue
        for cid in _payload_categories_for(f, lib):
            cat = cats.get(cid)
            if not cat:
                continue
            for i, payload in enumerate((cat.get("payloads") or [])[:limit]):
                p = dict(base)
                p[name] = payload
                short = payload if len(payload) <= 40 else payload[:37] + "..."
                out.append(_mk(
                    "G-%s-%s-%d" % (cid, name, i + 1),
                    "%s：%s=%r（应被拦截）" % (cat["name"], name, short),
                    "G", "安全-%s" % cat["name"], p,
                    {"success": False, "err_keywords": cat.get("err_keywords", [])},
                    action=action, priority="P0", source="generator:security_payload",
                    note=cat.get("note") or "载荷被原样接受即缺陷；被正确拦截记「符合预期」非缺陷（陷阱3）"))
    return out


def gen_baseline(contract, base, action):
    """基线用例：主链路成功 + 契约校验。"""
    keys = _resp_contract_keys(contract)
    return [
        _mk("D01", "主链路：正常创建（校验响应契约字段）", "D", "业务-核心流程", dict(base),
            {"success": True, "contract_keys": keys},
            action=action, priority="P0", source="generator:baseline"),
    ]


def _identity_field(contract):
    """找「创建者 / 操作者」类身份字段——A 维度越权矩阵的身份注入点。

    刻意只认**字段名**，不用 semantics 里的 id 标签：id 覆盖 vid/sid/corpid 等
    资源标识，拿它当身份去注入子账号，是把合法的资源 ID 换成登录名，
    得到的是参数校验错误而不是越权结论。
    """
    tokens = ("creater", "creator", "userid", "user_id", "uid", "openid",
              "account", "username", "member", "staff")
    for f in (contract.get("request_fields") or []):
        n = (f.get("name") or "").lower()
        if any(t in n for t in tokens):
            return f
    return None


_ID_READ_TOKENS = ("id", "vid", "sid", "uid", "no", "code", "key")


def _read_probe_params(read_params):
    """读面越权用例的探测参数：优先用调用方给的 read_params。

    为空时退回一个空 dict——读列表/计数类接口本身就不需要入参，
    带着空参数去问"跨企业身份能不能读到"依然是有效探测。
    """
    return dict(read_params or {})


def gen_auth_cases(contract, base, action, read_action=None, read_params=None):
    """A 维度：鉴权与越权矩阵（历史高发，人最容易漏）。

    矩阵按「身份 × 操作面」铺开，而不是只给一条签名错误：
      身份轴：本企业主账号（对照）/ 子账号 / 跨企业 appid / 跨企业+主账号（双重矛盾）
      操作面：写（本接口 base 请求）/ 读（配套读接口 read_action）
    越权是单接口测试里价值最高的一项，且不依赖任何其他接口的业务知识——
    身份与跨企业 appid 都可由配置直接构造，生成期就能铺满。

    身份与跨企业 appid 一律用占位符承载，执行期由 personal_config 解析：
    包内零个人数据，换环境只换配置，用例本身不变。
    守卫：占位符解析不到时（如未配 cross_appid），执行期点名跳过而不是发一条残缺请求。
    """
    out = []
    p = dict(base)
    p["sign"] = "xwrongsignx"
    out.append(_mk("A01", "错误 sign（签名校验应生效）", "A", "鉴权-签名", p,
                   {"success": False}, action=action, priority="P0",
                   source="generator:auth_sign",
                   note="约定不校验 sign 的环境应仍成功——此时记「待确认」而非缺陷"))

    ident = _identity_field(contract)
    ident_name = ident["name"] if ident else None
    rp = _read_probe_params(read_params)
    rp_has_id = any(any(t in k.lower() for t in ("id", "vid", "sid", "uid")) for k in rp)

    def _write(cid, name, appid_ph, creater_ph, expect_ok, priority, note):
        q = dict(base)
        if appid_ph:
            q["appid"] = appid_ph
        if creater_ph and ident_name:
            q[ident_name] = creater_ph
        return _mk(cid, name, "A", "越权矩阵", q,
                   {"success": bool(expect_ok)}, action=action, priority=priority,
                   source="generator:auth_matrix", note=note)

    def _read(cid, name, priority, note):
        """读面越权用例：主请求用**本企业身份**自证可读（对照组），
        跨企业身份的比对交给 check 做——否则读接口因缺参数失败会被误判成"越权被拦住"。
        """
        return _mk(cid, name, "A", "越权矩阵-读面", dict(rp),
                   {"success": True}, action=read_action, priority=priority,
                   check="cross_tenant_read",
                   check_args={"read_action": read_action, "read_params": dict(rp)},
                   source="generator:auth_matrix", note=note)

    # --- 写面：身份轴三档（跨企业 / 子账号 / 主账号对照）+ 双重矛盾 ---
    # build_payload 的 kwargs 覆盖语义保证 case params 里的 appid 不会被覆盖回当前 appid。
    out.append(_write(
        "A02", "越权矩阵·跨企业写：用跨企业 appid 提交写请求（应被拒绝）",
        "<跨企业appid>", None, False, "P0",
        "A 维度最高价值项：跨企业身份改写本企业数据即越权缺陷。"
        "需 personal_config.cross_appid（或 WJX_CROSS_APPID）；未配置时执行期点名跳过。"
        "按环境纪律只允许在异常场景显式跨环境取参"))

    if ident_name:
        out.append(_write(
            "A04", "越权矩阵·子身份写：%s 换为子账号提交（应被拒绝或降权）" % ident_name,
            None, "<子账号>", False, "P1",
            "纵向越权：子账号身份执行主账号才允许的操作应被拒绝或降权。"
            "约定允许管理员代建（creater 可任填）的接口应仍成功——此时记「待确认」而非缺陷。"
            "需 personal_config.sub_account（或 WJX_SUB）"))
        out.append(_write(
            "A05", "越权矩阵·主身份写（A04 的对照）：%s 显式填主账号（应成功）" % ident_name,
            None, "<主账号>", True, "P2",
            "对照组：与 A04 配对判读。A04 被拒而本条成功 → 降权生效；"
            "两条都成功 → 服务端未校验身份一致性，需人工确认是否属于设计内。"
            "需 personal_config.main_account（或 WJX_MAIN）"))
        out.append(_write(
            "A06", "越权矩阵·双重越权：跨企业 appid + 主账号 %s（应被拒绝）" % ident_name,
            "<跨企业appid>", "<主账号>", False, "P1",
            "身份与资源归属双重矛盾：跨企业凭证却声明本企业主账号身份，"
            "若被接受说明服务端既不校验租户也不校验身份一致性"))

        # 契约写明的权限约束——只有语义层读得出来，规则层看着字段名只能猜"这是个人名"
        roles = ((ident or {}).get("llm_constraints") or {}).get("required_role_for_subaccount") or []
        if (ident or {}).get("llm_role") == "operator_identity" and roles:
            out.append(_write(
                "A08", "权限约束·子账号缺角色：%s 填无「%s」角色的子账号（应被拒绝）"
                       % (ident_name, "」/「".join(roles)),
                None, "<无权限子账号>", False, "P0",
                "契约写明的权限约束，不是靠字段名猜的越权（evidence：%s）。"
                "需 personal_config 补充一个不具备上述角色的子账号；未配置时执行期点名跳过。"
                % ((ident or {}).get("llm_evidence") or "未提供")))
    else:
        # 没有身份字段可注入 → 纵向越权这一整列无法自动构造。点名待补，不静默缺失。
        out.append(_mk(
            "TODO-A-越权", "A 维度：契约无身份字段，纵向越权（子/主账号切换）与横向越权需人工补充",
            "A", "待补充", dict(base), {"success": False},
            action=action, enabled=False, needs="manual", priority="P0",
            source="generator:auth_placeholder",
            note="契约里没有 creater/creator/userid/account 等身份字段，自动生成器无法构造"
                 "「换一个身份操作」的请求，也无法构造「他人资源」的越权请求"
                 "（需要另一个账号真实创建的对象 ID）。"
                 "请人工补：① 身份冒用用例 ② 横向越权（拿他人资源 id 操作）用例"))

    # --- 读面：越权读取比越权写入更容易被漏掉（写入常被参数校验挡住，读取直接泄数据） ---
    if read_action:
        out.append(_read(
            "A03", "越权矩阵·跨企业读：本企业可读为对照，跨企业 appid 调 %s 应取不到数据" % read_action,
            "P0",
            "配对判定：本企业身份先自证可读，再用跨企业 appid 读同一资源。"
            "跨企业也读到 = 越权缺陷；本企业也读不到 = 无法归因，只记观察不判越权。"
            "需 cross_appid 与配套读接口（read_action=%s）" % read_action))
        if rp_has_id:
            out.append(_read(
                "A07", "越权矩阵·跨企业读指定资源：带本企业资源 id 用跨企业 appid 读（应取不到）",
                "P0",
                "横向越权读：带上本企业资源标识去读，跨企业身份应取不到该资源。"
                "read_params 里必须带资源 id，否则与 A03 等价"))
        else:
            out.append(_mk(
                "TODO-A-读面资源", "A 维度：read_params 未带资源标识，跨企业读指定资源的横向越权需人工补充",
                "A", "待补充", dict(rp), {"success": False},
                action=read_action, enabled=False, needs="read_params", priority="P1",
                source="generator:auth_placeholder",
                note="配套读参数里没有 id/vid/sid/uid 类键，无法构造"
                     "「拿本企业资源 id 去跨企业读」的横向越权请求。"
                     "给 read_params 补上资源 id 后本条会自动生成为 A07"))
    else:
        out.append(_mk(
            "TODO-A-读面", "A 维度：缺配套读接口，越权矩阵读面（跨企业读/横向越权读）无法自动构造",
            "A", "待补充", dict(base), {"success": False},
            action=action, enabled=False, needs="read_action", priority="P0",
            source="generator:auth_placeholder",
            note="越权**读取**往往比越权写入更致命（写入常被参数校验挡住，读取直接泄数据），"
                 "但读面用例必须打到配套读接口上。请用 --read-action <action> 指定，"
                 "或在契约里声明 read_action 后重跑——读面 A03/A07 会自动补上"))
    return out


def gen_structure_and_transport(contract, base, action):
    """Y 复杂结构返回 / AE 认证与传输补充——两者在适用性矩阵里对创建类接口为必测，
    若不生成对应用例，覆盖率门禁会把它们判成「漏测」假缺口。"""
    out = []
    if contract.get("response_fields"):
        out.append(_mk("Y01", "复杂结构返回：各子类型结构完整、无缺层", "Y", "复杂结构返回",
                       dict(base), {"success": True},
                       check="structure_complete",
                       check_args={"documented_fields": [f["name"] for f in contract["response_fields"]]},
                       action=action, priority="P1", source="generator:structure"))
    out.append(_mk("AE01", "认证与传输契约：请求头/传输语义", "AE", "认证传输补充",
                   dict(base), {"success": True},
                   check="transport_contract", action=action, priority="P1",
                   source="generator:transport",
                   note="AE：Content-Type/Accept/gzip/trace-id 等传输层约定"))
    return out


def _resp_id_field(contract):
    """响应里的资源标识字段名（交叉验证到列表里定位资源时用）。"""
    for f in (contract.get("response_fields") or []):
        n = (f.get("name") or "").lower()
        if n in ("vid", "id", "sid", "uid", "page_id"):
            return f["name"]
    for f in (contract.get("response_fields") or []):
        n = (f.get("name") or "").lower()
        if n.endswith(("id", "_id")):
            return f["name"]
    return None


def gen_cross_verify_cases(contract, base, action, ctx):
    """C1 交叉验证：不只看主接口响应，调独立接口看数据到底落没落（framework §9.4.2）。

    「响应 success 但没落库 / 落错库」只看主接口永远抓不到。列表接口与主接口越独立，
    这条断言越可信，所以只在拿到配套**列表**接口时才生成；拿不到就诚实留空，
    由 TODO 占位在报告里点名，不拿主接口的响应假装验过。
    """
    out = []
    la = ctx.get("list_action")
    op = ctx.get("op_type")
    if not la or op not in ("创建", "删除"):
        return out
    present = (op == "创建")
    delta = 1 if present else -1
    idf = _resp_id_field(contract)
    cargs_list = {"list_action": la, "expected_present": present, "list_field": "list"}
    if idf:
        cargs_list["id_field"] = idf
    out.append(_mk(
        "C-CROSS-LIST", "交叉验证·列表侧：%s后资源应在独立列表接口 %s 中%s" % (op, la, "出现" if present else "消失"),
        "C", "交叉验证", dict(base), {"success": True},
        check="cross_list", check_args=cargs_list,
        action=action, priority="P0", source="generator:cross_verify",
        note="§9.4.2：主接口说成功不算数，去独立列表接口里按资源 id 找。"
             "列表里找不到 = 没落库或落错库，是只看响应永远抓不到的缺陷。"
             "资源 id 取自主接口响应%s" % ("的 %s 字段" % idf if idf else "（未识别到 id 字段，"
                                            "可用 check_args.id_field 指定）")))
    out.append(_mk(
        "C-CROSS-COUNT", "交叉验证·计数侧：%s后列表总数应 %+d" % (op, delta),
        "C", "交叉验证", dict(base), {"success": True},
        check="count_delta",
        check_args={"count_action": la, "expected_delta": delta, "count_field": "total"},
        action=action, priority="P1", source="generator:cross_verify",
        note="§9.4.2 计数行。基线在主请求**发出前**采集（+1/-1 是操作前后的差），"
             "check 内两次取数都已落在操作后，故无法自采基线。"
             "若列表接口不返回 total，本条只记观察不判缺陷"))
    return out


# 语义事实键 → 是否有消费方。抽取出来的键不等于被用上的键：「并入 N 条事实」的口径
# 比「兑现 M 条」丰满，报告必须把两者分开，否则看起来接得很满、其实一半只是存档。
_FACT_CONSUMERS = {
    "role": "llm_fact_cases(publish_flag/state) · dim_generators(ROLE_DIM_MAP) · A08",
    "constraints.alias_of": "llm_fact_cases._alias_cases（驱动别名硬断言）",
    "constraints.required_role_for_subaccount": "case_generator A08（越权矩阵）",
    "constraints.default": "llm_fact_cases._flag_cases（publish_flag）+ _default_cases（evidence 有据）",
    "constraints.max_len": "仅交叉校验；不驱动生成（B 维度用契约原生 max_len）",
    "constraints.enum": "仅交叉校验；不驱动生成（规则层遍历契约原生 enum）",
    "constraints.forbid": "llm_fact_cases._forbid_cases（仅 evidence 有据的禁用词）",
    "constraints.relative": "不消费",
    "constraints.is_url": "不消费（URL 判定用 field_semantics 语义标签）",
    "constraints.fallback_chain": "llm_fact_cases._fallback_chain_cases（字段级兜底表达，与 relation 级去重）",
    "relations": "llm_fact_cases(fallback_chain / alias / url_compose)",
    "unknowns": "llm_fact_cases（留痕 + consumed_unknowns 去重）",
    "semantics": "不消费（供人工阅读）",
    "evidence": "报告留痕，不驱动生成",
    "confidence": "不消费（供人工阅读）",
    "test_implications": "不消费（模型的用例建议当前不自动采纳）",
}


def _load_facts_json(path):
    """读事实 JSON，容忍模型把 JSON 包在代码块或附加解释里。

    提示词要求「只输出 JSON」，但模型经常仍然加 ```json 围栏或前后缀说明。
    直接 json.loads 会把这种情况算成「语义层没接」——而它其实只是格式没听话，
    白白丢掉一整次抽取结果。故逐级退化：原样 → 剥围栏 → 截取最外层花括号。
    """
    with open(path, encoding="utf-8") as fh:
        raw = fh.read()
    try:
        return json.loads(raw), ""
    except ValueError:
        pass
    m = re.search(r"```(?:json)?\s*(.+?)\s*```", raw, re.S)
    if m:
        try:
            return json.loads(m.group(1)), "已从 ```json 代码块中提取"
        except ValueError:
            pass
    i, j = raw.find("{"), raw.rfind("}")
    if i != -1 and j > i:
        try:
            return json.loads(raw[i:j + 1]), "已从混杂文本中截取最外层 JSON 对象"
        except ValueError:
            pass
    raise ValueError("事实 JSON 解析失败（已尝试原样、剥代码块、截取花括号三级退化）")


def _fact_utilization(clean):
    """「抽取到的键」与「真正有消费方的键」分开列，避免并入量虚高。"""
    produced = set()
    for f in (clean.get("fields") or {}).values():
        for k in (f.get("constraints") or {}):
            produced.add("constraints.%s" % k)
        for k in ("role", "semantics", "evidence", "confidence", "test_implications"):
            if f.get(k) not in (None, "", [], {}):
                produced.add(k)
    if clean.get("relations"):
        produced.add("relations")
    if clean.get("unknowns"):
        produced.add("unknowns")
    consumed = {k for k in produced
                if k in _FACT_CONSUMERS and "不消费" not in _FACT_CONSUMERS[k]}
    return {
        "produced": sorted(produced),
        "consumed": sorted(consumed),
        "archived": sorted(produced - consumed),
    }


def _enrich_llm_facts(contract, facts_path, contract_text):
    """并入大模型语义层事实（可选，默认关闭）。

    没有事实文件时原样返回——接入是**增量**不是替换，纯规则链路行为完全不变。
    校验不通过的事实整条丢弃并留痕：半对半错的事实比没有更危险。
    """
    path = facts_path or os.environ.get("WJX_LLM_FACTS") or ""
    if not path:
        return contract, None
    try:
        import llm_semantics
        facts, note = _load_facts_json(path)
        # 顶层必须是对象：模型可能直接返回数组或裸字符串。这类输入无法校验，
        # 必须降级返回 error，不能让 AttributeError 把整个生成流程打断。
        if not isinstance(facts, dict):
            return contract, {"path": path,
                              "error": "事实 JSON 顶层必须是对象，实际是 %s"
                                       % type(facts).__name__}
        rows, clean = llm_semantics.verify(facts, contract, contract_text or "")
        llm_semantics.merge(contract, clean)
    except (OSError, ValueError, ImportError, AttributeError, TypeError) as e:
        return contract, {"path": path, "error": str(e)}
    rejected = [r for r in rows if r[2] not in llm_semantics.PASS_STATES]
    # 「按契约值核验一致」「放行采信」「仅验证据」三种结论必须分开记账：
    # 前两者有契约值参与判定，第三者只校验了字段存在与 evidence 是原文子串。
    # 混在一起会让「核验通过 N 条」被读成 N 项都验过值。
    verified = [r for r in rows if r[2] == llm_semantics.PASS_VERIFIED]
    unverified = [r for r in rows if r[2] == llm_semantics.PASS_UNVERIFIED]
    evidence_only = [r for r in rows if r[2] == llm_semantics.PASS_EVIDENCE_ONLY]
    return contract, {"path": path,
                      "note": note,
                      "merged_fields": len(clean["fields"]),
                      "merged_relations": len(clean["relations"]),
                      "unknowns": len(clean["unknowns"]),
                      "verified_count": len(verified),
                      "unverified_count": len(unverified),
                      "unverified": unverified,
                      "evidence_only_count": len(evidence_only),
                      "evidence_only": evidence_only,
                      "rejected_count": len(rejected),
                      "rejected": rejected,
                      "fact_utilization": _fact_utilization(clean)}


def generate(contract_path, action=None, read_action=None, list_action=None, read_params=None,
             auto_companion=True, llm_facts=None):
    """契约 → 用例文档。

    read_action / list_action / read_params 是**交叉验证的配套接口**声明，透传给缺陷模式
    引擎与越权矩阵：给了就能做真往返（读→原样回写）与列表侧「创建后出现/计数+1」校验。

    三个来源按优先级：命令行显式指定 > 契约文档显式声明 > 同级目录扫描（companion_scan）。
    扫描只在**业务对象名对得上**时才采用，并把候选清单与置信度写进产物供审计；猜不出来的
    时候宁可空着让人指定，也不猜一个出来把 ENV 失败伪装成"往返已验证"。
    """
    contract = annotate(load_contract(contract_path, action=action))
    app = judge(contract)
    base = pattern_engine.base_params(contract)

    raw = ""
    try:
        with open(contract_path, "r", encoding="utf-8", errors="ignore") as fh:
            raw = fh.read()
    except OSError:
        raw = ""

    contract, llm_report = _enrich_llm_facts(contract, llm_facts, raw)
    if llm_report:
        contract["llm_verify_report"] = llm_report
        if llm_report.get("error"):
            # 语义层接入失败必须出声。静默降级会让「这次没接语义层」和
            # 「接了但没产出」看起来一样，使用者以为事实已经生效。
            sys.stderr.write("[llm] 语义层接入失败，已按纯规则继续：%s\n"
                             % llm_report["error"])
        elif llm_report.get("note"):
            print("[llm] %s" % llm_report["note"])

    comp = resolve_companion(contract_path, contract_text=raw, cli_read=read_action,
                             cli_list=list_action, cli_read_params=read_params,
                             main_action=contract.get("action"), auto=auto_companion)
    ra = comp["read_action"] or contract.get("read_action")
    la = comp["list_action"] or contract.get("list_action")
    rp = comp["read_params"] or contract.get("read_params") or {}

    ctx = {
        "base": base,
        "op_type": app["op_type"],
        "is_intl": app["is_intl"],
        "is_list_query": app.get("is_list_query"),
        "coverage_plan": app["coverage_plan"],
        "read_action": ra,
        "read_params": rp,
        "list_action": la,
        "companion": comp,
    }
    cases = []
    cases += gen_baseline(contract, base, contract.get("action"))
    cases += gen_auth_cases(contract, base, contract.get("action"),
                            read_action=ra, read_params=rp)
    cases += gen_cross_verify_cases(contract, base, contract.get("action"), ctx)
    cases += gen_field_mutations(contract, base, contract.get("action"))
    cases += gen_combination_cases(contract, base, contract.get("action"))
    payload_skipped = []
    cases += gen_security_payload_cases(contract, base, contract.get("action"),
                                        skipped=payload_skipped)
    cases += gen_structure_and_transport(contract, base, contract.get("action"))
    cases += dim_generators.run_all(contract, base, contract.get("action"), ctx)
    cases += llm_fact_cases.run(contract, base, contract.get("action"), ctx)
    pcases, matched = pattern_engine.run(contract, ctx)
    cases += pcases

    # 必测维度兜底：生成器确实覆盖不了的维度，显式生成一条「需人工补充」占位，
    # 让它在报告里被点名，而不是静默缺失（N/A 必须列出证明已判断）。
    covered = {c.get("dimension") for c in cases}
    for d in app["coverage_plan"]:
        if d in covered:
            continue
        cases.append(_mk("TODO-%s" % d, "维度 %s：生成器未自动覆盖，需人工补充用例" % d, d,
                         "待补充", dict(base), {"success": True},
                         action=contract.get("action"), enabled=False, needs="manual",
                         priority="P1", source="generator:placeholder",
                         note="自动生成的占位：执行时跳过并在报告中点名，避免静默漏测"))

    # 选测维度零用例时只在产物里点名，不生成占位用例：占位会撑长清单却不执行，
    # 而「本轮哪些选测维度是空的」这个信息本身要可审计。
    #
    # 点名必须带原因。只列维度字母时，「契约确实没有该特征」与「有特征但响应侧无法
    # 构造请求」「维度压根没实现」三者在报告里长得一样，能力缺口会被读成「无需测」。
    uncovered_optional = [d for d in (app.get("optional_plan") or []) if d not in covered]
    _reasons = dim_generators.explain_zero_output(contract, uncovered_optional)
    uncovered_optional_detail = [
        {"dimension": d, "reason": _reasons.get(d, dim_generators.REASON_NO_FEATURE),
         "reason_text": dim_generators.REASON_TEXT.get(
             _reasons.get(d, dim_generators.REASON_NO_FEATURE), "")}
        for d in uncovered_optional]

    cases = _enrich_cases(cases, contract)

    # 去重 id
    seen, uniq = {}, []
    for c in cases:
        cid = c.get("id") or "CASE"
        if cid in seen:
            seen[cid] += 1
            c["id"] = "%s#%d" % (cid, seen[cid])
        else:
            seen[cid] = 0
        uniq.append(c)

    # 用例身份证：全量用例（含语义/人工）纳入编号体系后，再与契约约束对账。
    # 用派生而非逐点手改：9 个生成器、84 个 _mk 调用点，只有集中派生才不会漏。
    check_key.assign_keys(uniq)
    ck_rec = check_key.reconcile(uniq, contract)

    if not contract.get("request_fields") and not contract.get("response_fields"):
        sys.stderr.write(
            "警告：契约未解析出任何请求/响应字段（格式不符或空契约？）——本轮用例将以占位为主，"
            "不代表设计覆盖。\n"
            "  请检查：① 契约格式是否为 ShowDoc Markdown / OpenAPI 3.x / Swagger 2.0；\n"
            "  ② 或改用声明式 JSON 契约显式给出 request_fields；详见 references/auto_generation.md。\n")

    return {
        "action": contract.get("action"),
        "name": contract.get("name"),
        "endpoint": contract.get("endpoint"),
        "source": contract.get("source"),
        "op_type": app["op_type"],
        "is_intl": app["is_intl"],
        "business_domains": app["business_domains"],
        "coverage_plan": app["coverage_plan"],
        "optional_plan": app.get("optional_plan", []),
        "uncovered_optional": uncovered_optional,
        "uncovered_optional_detail": uncovered_optional_detail,
        "na_proofs": app["na_proofs"],
        "dimensions": app["dimensions"],
        "notes": app["notes"],
        "matched_patterns": matched,
        "field_suggestions": suggest_for_contract(contract),
        "base_params": base,
        "read_action": ctx["read_action"],
        "list_action": ctx["list_action"],
        "read_params": ctx["read_params"],
        "companion": comp,
        "tier_stats": tier_stats(uniq),
        "payload_skipped": payload_skipped,
        "check_key": ck_rec,
        "llm_verify_report": contract.get("llm_verify_report"),
        "cases": uniq,
    }


def summary(doc):
    by_dim = {}
    for c in doc["cases"]:
        by_dim.setdefault(c.get("dimension") or "-", 0)
        by_dim[c.get("dimension") or "-"] += 1
    return by_dim


def _print_tier(doc):
    """打印分层构成：业务驱动 vs 机械探测。

    机械探测用例照常执行，但报告必须让人看到「业务覆盖」的真实条数——
    否则「132 条用例、维度全绿」会掩盖「业务场景只覆盖了 58 条」这个事实。
    """
    ts = doc.get("tier_stats") or tier_stats(doc.get("cases") or [])
    total = sum(ts.values()) or 1
    biz = ts.get("business", 0)
    probe = ts.get("probe", 0)
    manual = ts.get("manual", 0)
    print("分层：业务驱动 %d（%.0f%%）· 机械探测 %d（%.0f%%）· 待人工 %d"
          % (biz, biz * 100.0 / total, probe, probe * 100.0 / total, manual))
    if probe > biz:
        print("  ⚠️ 机械探测多于业务驱动：用例总数与维度覆盖率会被撑高，"
              "真实业务覆盖以「业务驱动 %d 条」为准" % biz)


def main():
    ap = argparse.ArgumentParser(description="契约驱动的声明式用例生成器")
    ap.add_argument("--contract", required=True, help="契约文档路径（ShowDoc md / OpenAPI json）")
    ap.add_argument("--action", help="显式指定 action（契约里取不到时用）")
    ap.add_argument("--read-action", dest="read_action",
                    help="配套读接口 action：给了才能做真往返（读→原样回写→再回读）")
    ap.add_argument("--read-params", dest="read_params",
                    help="读接口所需参数，JSON 字符串（如 '{\"id\": 123}'）")
    ap.add_argument("--list-action", dest="list_action",
                    help="配套列表接口 action：用于「创建后验列表出现 / 计数+1」交叉验证")
    ap.add_argument("--no-auto-companion", dest="no_auto_companion", action="store_true",
                    help="关闭同级目录扫描自动推断配套接口（只用显式声明/命令行指定）")
    ap.add_argument("--llm-facts", dest="llm_facts",
                    help="语义层事实 JSON（scripts/llm_semantics.py 产出，可选）")
    ap.add_argument("--profile", default=None,
                    help="生成档位：conservative（默认）| discovery（放宽探测广度，"
                         "集中放宽超长上限/载荷条数/字段族配额）。也可用 WJX_PROFILE 指定")
    ap.add_argument("--out", default="generated_cases.json", help="输出用例文件")
    ap.add_argument("--print", dest="do_print", action="store_true", help="只打印摘要不落盘")
    args = ap.parse_args()

    # 档位必须在 generate() 之前激活——限额是生成期读取的。
    active = discovery_profile.activate(args.profile or discovery_profile.env_profile())
    if active == "discovery":
        print("发现档：探测广度已放宽（超长上限=%s 载荷/字段/类=%s 字段族配额已放大）；"
              "断言口径与门禁不变。" % (discovery_profile.get("big_cap"),
                                    discovery_profile.get("payload_per_field_per_category")))

    read_params = None
    if args.read_params:
        try:
            read_params = json.loads(args.read_params)
        except ValueError as e:
            ap.error("--read-params 需为合法 JSON：%s" % e)

    doc = generate(args.contract, args.action, read_action=args.read_action,
                   list_action=args.list_action, read_params=read_params,
                   auto_companion=not args.no_auto_companion,
                   llm_facts=args.llm_facts)
    comp = doc.get("companion") or {}
    if args.do_print:
        print("action=%s name=%s op=%s intl=%s" % (doc["action"], doc["name"], doc["op_type"], doc["is_intl"]))
        print("COVERAGE_PLAN(必测，门禁):", doc["coverage_plan"])
        print("optional_plan(建议):", doc["optional_plan"])
        if doc.get("uncovered_optional_detail"):
            print("  ⚠️ 选测维度本轮无用例（不进门禁，仅提示）：")
            for item in doc["uncovered_optional_detail"]:
                print("     %-3s %s" % (item["dimension"], item["reason_text"]))
                if item["reason"] == dim_generators.REASON_NOT_IMPLEMENTED:
                    print("          → 属能力缺口，不是本接口无需测；勿据此判定覆盖完整")
        print("命中模式:", [(m["id"], m["cases"]) for m in doc["matched_patterns"]])
        print("配套接口: read_action=%s list_action=%s（来源=%s 置信=%s）"
              % (doc["read_action"], doc["list_action"], comp.get("source"), comp.get("confidence")))
        cand = comp.get("candidates") or {}
        if (cand.get("read") or cand.get("list")) and comp.get("confidence") == "none":
            print("  扫描到候选但未自动采用：%s"
                  % {k: ["%s(%s)" % (x["action"], x["title"]) for x in v] for k, v in cand.items() if v})
        print("用例数=%d  按维度=" % len(doc["cases"]), summary(doc))
        _print_tier(doc)
        if doc.get("check_key"):
            check_key.report(doc["check_key"])
        return
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    print("已生成 %d 条用例 -> %s" % (len(doc["cases"]), args.out))
    print("COVERAGE_PLAN(必测，门禁): %s" % ", ".join(doc["coverage_plan"]))
    print("命中缺陷模式 %d 条：%s" % (len(doc["matched_patterns"]),
                                 ", ".join("%s(%d)" % (m["id"], m["cases"]) for m in doc["matched_patterns"])))
    print("按维度：%s" % summary(doc))
    _print_tier(doc)
    if doc.get("check_key"):
        check_key.report(doc["check_key"])
    if not doc["read_action"]:
        print("提示：无配套读接口（来源=%s），往返验证按「写入后回读一致性」退化执行，"
              "越权矩阵的读面以 TODO 占位点名" % comp.get("source"))
        for k in ("read", "list"):
            cs = (comp.get("candidates") or {}).get(k) or []
            if cs:
                print("  扫描到的 %s 候选（未采用，可用 --%s-action 指定）：%s"
                      % (k, k, ", ".join("%s(%s)" % (x["action"], x["title"]) for x in cs)))
    if not doc["list_action"]:
        print("提示：无配套列表接口，§9.4.2 的「创建后验列表出现 / 计数+1」交叉验证未生成"
              "（可用 --list-action 指定）")


if __name__ == "__main__":
    main()
