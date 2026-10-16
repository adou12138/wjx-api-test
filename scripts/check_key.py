# -*- coding: utf-8 -*-
"""用例身份证（check_key）与约束对账。

解决的问题
----------
维度级覆盖率门禁只能回答「边界维度有没有用例跑过」，回答不了
「契约声明了 age 的 max_len=120，有没有一条 above_maximum:age 用例」。
字段约束的机械展开是否完整，此前**没有任何机械验证手段**——生成器漏掉
某个字段的某个约束时，该字段所在维度仍被其他用例覆盖，门禁照常通过。
这是「不报错的错」：报告看起来完整，实际缺口静默存在。

三层能力
--------
1. ``required_checks(contract)``  从契约机械算出**约束全集**（对账分母）
2. ``derive_key(case)``           给每条用例派发身份证
3. ``reconcile(cases, contract)`` 对账：约束全集 vs 已兑现，缺口即出声

分级原则（关键，避免造出永不通过的假门禁）
------------------------------------------
- **约束型 key**（``missing_required:name`` 等）兑现字段约束，进对账分母，
  缺了是真缺口。
- **来源型 key**（``semantic:xxx`` / ``pattern:xxx`` / ``manual:xxx``）给语义、
  人工、模式匹配、业务类用例提供可追溯身份证，让「全量用例都在编号体系内」，
  但它们**不兑现字段约束**，因此**不计入对账分母**——否则门禁永远不可能通过。

为什么派生而不是在 84 个生成点手工发 key
----------------------------------------
用例由 9 个生成器产出、``_mk()`` 调用点 84 处，逐点加参数既易漏又会把
「发 key」这件事和「生成逻辑」耦合。派生只读用例 id 与 source，集中一处，
天然覆盖全量用例（含语义/人工），且新增生成器无需改本模块即可被纳入体系。
"""

import re
import sys

import discovery_profile

CONSTRAINT_TYPES = (
    "missing_required",   # 必填缺失 → 应被拒绝
    "wrong_type",         # 类型错误 → 应被拒绝
    "above_maximum",      # 超过上限（长度/数值）→ 应被拒绝
    "below_minimum",      # 低于下限 → 应被拒绝
    "invalid_enum",       # 枚举越界值 → 应被拒绝
    "over_validation",    # 可选/可空形态 → 不应被过度拒绝
    "at_maximum",         # 恰好等于上限 → 应成功（边界口径）
    "at_minimum",         # 恰好等于下限 → 应成功（边界口径）
    "null_value",         # 显式 null
)

# 机械用例 ID 前缀 → 约束类型。
# 长前缀必须排在短前缀之前：否则 "B-LEN-OVER-x" 可能被更短的通用规则先截走。
_MECH_PREFIXES = (
    ("B-LEN-UNDER-", "below_minimum"),
    ("B-LEN-MIN-", "at_minimum"),
    ("B-LEN-OVER-", "above_maximum"),
    ("B-LEN-EDGE-", "at_maximum"),
    ("B-MISS-", "missing_required"),
    ("B-TYPE-", "wrong_type"),
    ("B-OVER-", "over_validation"),
    ("B-NULL-", "null_value"),
    ("B-ENUM-", "invalid_enum"),
)

# B-OVER-{field}-{state} 的状态后缀，解析字段名时要剥掉
_OVER_STATES = ("empty_string", "missing", "null")
# B-NULL-{field}-{label} 的四态标签后缀，同上剥掉（与 pattern_engine.p07_gen 对齐）
_NULL_STATES = ("缺失", "null", "空串", "空数组")

# 生成器能构造非法值的类型 token，与 case_generator.TYPE_BAD 的键对齐。
# 这里重复定义一份而非 import，是因为 case_generator 已 import 本模块（反向导入
# 会成环）；一致性由单测 test_check_key.py 守住，漂移即测试失败。
_BAD_TYPE_TOKENS = frozenset((
    "int", "int32", "int64", "integer", "number", "float", "double",
    "bool", "boolean", "string", "array", "list", "object",
))

# 生成器只对 max_len 不超过该上限的字段构造长度用例（超长 payload 会拖慢请求、
# 被网关拒收，属有意跳过——实测 1000107/1000108 的 html_content max_len=200000）。
# 分母必须与这条生成器承诺对齐：超限字段声明 above_maximum 会产生永远无法
# 兑现的假缺口，且恰好落在高频接口上每轮必刷。
#
# 该上限由 discovery_profile 集中管理（发现档会放宽到数万）。这里保留常量是给
# 单测锁定"默认档一致"这条不变量；真正的对账走 _active_max_len_cap()，
# 取**当前生效档位**的值，从而在 discovery 下分母与生成器承诺仍然同步放宽。
_MAX_LEN_CAP = discovery_profile.DEFAULTS["big_cap"]


def _active_max_len_cap():
    """当前生效档位的超长上限（含 discovery 放宽）。"""
    return discovery_profile.get("big_cap")

def _declares_typed(ftype):
    """契约是否声明了「生成器能构造非法值」的类型（决定进不进 wrong_type 分母）。

    复合类型（``null / boolean``、``integer / string(int32)``）按其中的类型 token
    判定：契约确实声明了类型，生成器理应能构造非法值，漏了是真缺口。

    中文/URL 之类的值（``问卷id``、``www.wjx.cn``、``showdoc 导出``）不含任何类型
    token——那是解析器列错位把说明当成了类型，并非契约声明的类型。把它们算进分母
    会让门禁被解析噪音淹没（实测 25 篇契约里约 11 条属此类），故不计入。

    【W26 口径统一】token 切分必须与 case_generator.type_tokens 一致（[^a-z0-9]+，
    保留数字）——旧版这里用 [^a-z]+ 会把 "int32" 切成 "int"+"32"，把数字段丢给
    生成器侧看不到的判定，两侧对同一类型串得出不同结论。分母虚增/假缺陷由此而来。
    （本模块刻意不 import case_generator（反向导入成环），一致性由单测锁定。）
    """
    toks = re.split(r"[^a-z0-9]+", str(ftype or "").lower())
    return any(t in _BAD_TYPE_TOKENS for t in toks if t)


def _chk(ctype, field, reason):
    return {"key": "%s:%s" % (ctype, field), "type": ctype,
            "field": field, "reason": reason}


def _strip_dup_suffix(cid):
    """剥离去重后缀：``B-MISS-name#2`` → ``B-MISS-name``。

    用例 id 撞号时会被改写成 ``原id#N``，不剥离会让同一字段的约束
    被解析成两个不同的字段名，制造出永不兑现的假缺口。
    """
    i = cid.rfind("#")
    if i > 0 and cid[i + 1:].isdigit():
        return cid[:i]
    return cid


def required_checks(contract):
    """从契约机械算出「必须被用例兑现」的约束全集（门禁分母）。

    只看契约白纸黑字声明的约束：required / type / max_len / min_len /
    enum / nullable。契约没声明的一律不进分母——猜测出来的约束会变成
    永远无法兑现的假缺口，那正是本模块要防的失败模式。
    """
    out = []
    fields = list(contract.get("request_fields") or [])
    # 【审查修复·null_value 配额对齐】p07_gen 的 B-NULL 只对 pick_by_family(
    # 可选字段, null_state_fields 配额) 的**选集**生成（field_semantics 族保代表，
    # 默认配额 6/discovery 12）——分母必须用同一选集声明，否则可选字段超过配额时
    # 被砍字段的 null_value 每轮刷永久假缺口（8 可选字段实测每轮 2 条）。
    # 选集逻辑与 pattern_engine.p07_gen 严格同源（同函数同配额键）。
    from field_semantics import pick_by_family  # 局部导入：field_semantics 不依赖本模块，无环
    _null_selected = {f.get("name") for f in pick_by_family(
        [f for f in fields if not f.get("required")],
        discovery_profile.get("null_state_fields"))} if any(
        not f.get("required") for f in fields) else set()
    for f in fields:
        name = str(f.get("name") or "").strip()
        if not name:
            continue
        if f.get("required"):
            out.append(_chk("missing_required", name, "必填字段缺失应被拒绝"))
        # 可选或可空：只需一条 over_validation。写成两个 if 分别追加会让
        # 「可选且可空」的字段产生两条相同约束，把缺口数虚高一倍。
        if not f.get("required") or f.get("nullable"):
            out.append(_chk("over_validation", name,
                            "可选/可空字段的合法缺省形态不应被过度拒绝"))
        if _declares_typed(f.get("type")):
            out.append(_chk("wrong_type", name,
                            "类型错误应被拒绝（契约声明类型 %s）"
                            % str(f.get("type")).strip()))
        if f.get("max_len") and f["max_len"] <= _active_max_len_cap():
            out.append(_chk("above_maximum", name,
                            "超出长度上限 %s 应被拒绝" % f["max_len"]))
            # 【W11 补分母】生成器在同一条件下产 B-LEN-EDGE（恰好上限应成功，off-by-one
            # 缺陷高发区），分母必须同步声明 at_maximum——否则边界用例漏产时门禁无感。
            out.append(_chk("at_maximum", name,
                            "恰好等于长度上限 %s 应成功（边界 off-by-one）" % f["max_len"]))
        # min_len 下边界：仅对 min_len >= 2 声明 below_minimum。生成器同样只在
        # min_len >= 2 时构造"低于下限"用例（ml-1 >= 1 才是明确的非空过短值）；
        # ml == 1 时低于值即空串，与可选空值/必填缺失语义重叠，交给 over_validation /
        # missing_required 覆盖——分母与生成器承诺严格对齐，避免造出永不兑现的假缺口。
        if f.get("min_len") and int(f["min_len"]) >= 2:
            out.append(_chk("below_minimum", name,
                            "低于长度下限 %s 应被拒绝" % f["min_len"]))
            # 【W11 补分母】B-LEN-MIN（恰好下限应成功）仅对无枚举字段生成（有枚举时
            # 业务形态字符串会被枚举校验拒绝，构造即假缺陷）——分母与该承诺严格对齐。
            if not f.get("enum"):
                out.append(_chk("at_minimum", name,
                                "恰好等于长度下限 %s 应成功（边界 off-by-one）" % f["min_len"]))
        if f.get("enum"):
            out.append(_chk("invalid_enum", name,
                            "枚举越界值应被拒绝（契约声明 %d 个取值）"
                            % len(f["enum"])))
        # 【W12 激活 null_value】旧版 B-NULL 派生成 null_value key、required_checks
        # 却从不产出需求项——「显式 null 处理」漏测永远不出声。生成器对**可选**字段
        # 产 B-NULL-*（空值四态），这里对同一集合声明分母（required 字段的 null 语义
        # 由 missing_required 覆盖，不重复声明，避免虚增一倍缺口）。
        # 【审查修复】分母限定在 p07_gen 的同源选集内（见函数头部 _null_selected），
        # 配额外字段不声明——那批字段的 null 四态被族配额有意截断，声明即假缺口。
        if not f.get("required") and name in _null_selected:
            out.append(_chk("null_value", name,
                            "可选字段显式传 null 不应被过度拒绝也不应破坏处理"))
    return out


def _source_kind(case):
    """非机械用例的来源类别（来源型 key 的前半段）。"""
    if case.get("needs") == "manual":
        return "manual"
    src = str(case.get("source") or "").strip()
    head = src.split(":")[0].strip().lower() if src else ""
    if head in ("llm", "semantic", "fact"):
        return "semantic"
    if head == "pattern":
        return "pattern"
    return head or "other"


def derive_key(case):
    """给单条用例派生身份证。

    机械用例（``B-MISS-`` / ``B-TYPE-`` / ``B-LEN-`` 等预知前缀）→ 约束型
    ``<约束类型>:<字段名>``，进对账分母。
    其余（语义 / 人工 / 模式 / 业务）→ 来源型 ``<来源类别>:<用例id>``，
    只提供可追溯性，不进对账分母。
    """
    cid = _strip_dup_suffix(str(case.get("id") or ""))
    for prefix, ctype in _MECH_PREFIXES:
        if cid.startswith(prefix):
            field = cid[len(prefix):]
            if ctype == "over_validation":
                for st in _OVER_STATES:
                    if field.endswith("-" + st):
                        field = field[:-(len(st) + 1)]
                        break
            if ctype == "null_value":
                # 【审查修复】B-NULL-{field}-{label}（label ∈ 缺失/null/空串/空数组，
                # pattern_engine.p07_gen 四态）——与 B-OVER 同源的状态后缀必须剥掉，
                # 否则交付键 null_value:f-null 永远匹配不上分母键 null_value:f，
                # reconcile 每份 null_value 都假报缺口（本批审查发现）。
                for st in _NULL_STATES:
                    if field.endswith("-" + st):
                        field = field[:-(len(st) + 1)]
                        break
            if field:
                return "%s:%s" % (ctype, field)
            break
    return "%s:%s" % (_source_kind(case), cid or "UNNAMED")


def is_constraint_key(key):
    """是否约束型 key（决定进不进对账分母）。"""
    return str(key or "").split(":")[0] in CONSTRAINT_TYPES


def assign_keys(cases):
    """给全量用例补 check_key 字段（原地修改并返回）。

    语义与人工用例同样纳入编号体系：它们虽不兑现字段约束，但必须能被
    唯一定位——否则「编号体系」只覆盖一半用例，溯源时仍会出现无主用例。
    """
    for c in cases or []:
        c["check_key"] = derive_key(c)
    return cases


def reconcile(cases, contract):
    """对账：契约约束全集 vs 用例已兑现集合。

    返回 dict：required（约束全集）/ delivered（已兑现）/ missing（缺口）。
    missing 即「契约声明了、但没有任何用例去兑现」的约束——静默缺口。
    """
    required = required_checks(contract)
    delivered = set()
    for c in cases or []:
        k = c.get("check_key") or derive_key(c)
        if is_constraint_key(k):
            delivered.add(k)
    missing = [r for r in required if r["key"] not in delivered]
    return {
        "required": required,
        "required_keys": sorted({r["key"] for r in required}),
        "delivered": sorted(delivered),
        "missing": missing,
        "missing_keys": sorted({m["key"] for m in missing}),
    }


def report(rec, stream=None):
    """打印对账结论并返回缺口条数。

    缺口明细截断到 10 条：大接口可达上百条（实测创建问卷 108 条），全量打印
    会把"用例数 / 分层 / 覆盖计划"这些更常用的摘要挤出屏幕。完整清单始终
    在产物 check_key.missing 里，与执行期 check_key_gate 的口径一致。
    """
    out = stream or sys.stdout
    req, got = len(rec["required_keys"]), len(rec["required_keys"]) - len(rec["missing_keys"])
    out.write("\n[约束对账] 契约声明 %d 条字段约束，用例已兑现 %d 条，缺口 %d 条\n"
              % (req, got, len(rec["missing_keys"])))
    if rec["missing_keys"]:
        out.write("  缺口（契约声明但无任何用例兑现）：\n")
        for m in rec["missing"][:10]:
            out.write("    - %s  %s\n" % (m["key"], m["reason"]))
        if len(rec["missing"]) > 10:
            out.write("    ... 另 %d 条，完整清单见产物 check_key.missing\n"
                      % (len(rec["missing"]) - 10))
        out.write("  说明：这些约束静默缺失——所在维度仍被其他用例覆盖，"
                  "维度级门禁看不出来。\n")
    return len(rec["missing_keys"])
