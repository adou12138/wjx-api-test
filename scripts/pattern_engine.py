# -*- coding: utf-8 -*-
"""缺陷模式规则引擎：把 references/defect_patterns.md 的 19 条模式从「给人看的清单」
变成「对契约自动匹配并生成用例」的可执行规则。

每条规则 = match(contract, ctx) -> bool  +  gen(contract, ctx) -> [Case]
Case 为声明式 dict（schema 见 references/case_schema.md），由 case_runner.py 执行：
  正常/异常判定走 expect.success + expect.err_keywords；
  深度校验走 check + check_args（对应 api_test_runner.py 里的断言 helper）。

模式编号与 defect_patterns.md 一一对应（Top 1–12 + 补充 7 条），
另有 AA 静默错误专项（筛选无效是否静默返回全量——历史真实缺陷）。
"""
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from field_semantics import unique_key_field, is_absolute_url, pick_by_family  # noqa: E402
import discovery_profile  # noqa: E402

# 生成超长值时的上限（默认档），避免为 200000 上限的字段真去造 20 万字符。
# 生效值走发现档（discovery 可放宽），见 s5_gen。
BIG_VALUE_CAP = discovery_profile.DEFAULTS["big_cap"]

# 状态字段覆盖上限（v4.26.0）：E02/E04 逐个状态字段生成，多状态字段接口不再只测第一个。
STATE_MAX_FIELDS = 4


def base_params(contract):
    """取请求示例里的业务参数（剔除公共参数），作为用例变异的基线。"""
    common = {"encode", "appid", "action", "ts", "sign", "corpid", "sysid"}
    ex = contract.get("request_example") or {}
    if isinstance(ex, dict) and ex:
        return {k: v for k, v in ex.items() if k not in common}
    # 无示例时按字段类型造默认值
    out = {}
    for f in contract.get("request_fields", []) or []:
        if f.get("name") in common:
            # 公共参数由引擎在发请求时统一注入，不能当业务参数重复携带
            continue
        t = (f.get("type") or "").lower()
        if t in ("int", "integer", "number"):
            out[f["name"]] = 0
        elif t == "bool":
            out[f["name"]] = False
        else:
            out[f["name"]] = "test"
    return out


def _req_fields(contract):
    return [f for f in (contract.get("request_fields") or []) if f.get("required")]


def _opt_fields(contract):
    return [f for f in (contract.get("request_fields") or []) if not f.get("required")]


def _enum_fields(contract):
    return [f for f in (contract.get("request_fields") or []) if f.get("enum")]


def _mk(cid, name, dim, category, params, expect, **kw):
    case = {
        "id": cid, "name": name, "dimension": dim, "category": category,
        "action": kw.pop("action", None), "applicability": kw.pop("applicability", "适用"),
        "params": params, "expect": expect,
        "priority": kw.pop("priority", "P1"),
        "source": kw.pop("source", ""),
        "enabled": kw.pop("enabled", True),
        "needs": kw.pop("needs", ""),
        "note": kw.pop("note", ""),
    }
    case.update(kw)
    return case


# ---------------------------------------------------------------- Top 1
def p01_match(c, ctx):
    return bool(c.get("response_fields"))


def p01_gen(c, ctx):
    names = [f["name"] for f in (c.get("response_fields") or [])]
    return [_mk("K01", "响应字段双向 diff（缺失/冗余）", "K", "契约-字段完整",
                dict(ctx["base"]), {"success": True},
                check="contract_diff", check_args={"documented_fields": names},
                priority="P0", source="pattern:P01",
                note="Top1：文档声明字段与实际响应双向比对，缺字段/多字段均为缺陷")]


# ---------------------------------------------------------------- Top 2
def p02_match(c, ctx):
    return True


def p02_gen(c, ctx):
    return [_mk("AB01", "接口行为与页面基线一致性", "AB", "前端-接口口径",
                dict(ctx["base"]), {"success": True},
                check="ui_parity", priority="P0", source="pattern:P02",
                needs="ui_baseline",
                note="Top2：以页面基线为预期值。无基线只能记缺口，禁止凭空填预期")]


# ---------------------------------------------------------------- Top 3
def p03_match(c, ctx):
    text = (c.get("name") or "") + " " + " ".join(f.get("name", "") for f in (c.get("request_fields") or []))
    key = any(k in text.lower() for k in ("事件", "webhook", "订阅", "推送", "event", "callback"))
    return key or ctx.get("op_type") in ("创建", "修改", "删除")


def p03_gen(c, ctx):
    return [_mk("AD01", "事件推送：不丢失/不重复/载荷准确", "AD", "事件与回调",
                dict(ctx["base"]), {"success": True},
                check="event_delivery", priority="P1", source="pattern:P03",
                needs="manual",
                note="Top3：需注入 query_fn 查询推送日志；触发 N 次断言收到 N 条")]


# ---------------------------------------------------------------- Top 4
def p04_match(c, ctx):
    return True


def p04_gen(c, ctx):
    return [_mk("K02", "按文档示例参数原样调用（文档-实现一致）", "K", "契约-文档一致",
                dict(ctx["base"]), {"success": True, "contract_keys": [
                    f["name"] for f in (c.get("response_fields") or []) if f.get("required")]},
                priority="P0", source="pattern:P04",
                note="Top4：文档示例参数应能直接跑通；枚举释义逐条比对实现")]


# ---------------------------------------------------------------- Top 5
def p05_match(c, ctx):
    return True


def p05_gen(c, ctx):
    reqs = _req_fields(c)
    a = dict(ctx["base"])
    a["sign"] = "xwrongsignx"                       # 成因 A：凭证错误
    b = dict(ctx["base"])
    if reqs:                                         # 成因 B：必填缺失
        b.pop(reqs[0]["name"], None)
    else:
        b["__unknown_field__"] = 1
    return [_mk("H01", "不同成因错误：错误信息必须不同（根因不掩盖）", "H", "错误处理-根因",
                dict(ctx["base"]), {"success": False},
                check="error_root_cause",
                check_args={"payload_a": a, "payload_b": b, "cause_a": "凭证错误", "cause_b": "参数缺失"},
                priority="P0", source="pattern:P05",
                note="Top5：两种成因错误信息相同即为根因掩盖缺陷(H6)")]


# ---------------------------------------------------------------- Top 6
def p06_match(c, ctx):
    return bool(_enum_fields(c))


def p06_gen(c, ctx):
    out = []
    for f in _enum_fields(c):
        vals = [v for v, _ in f["enum"]]
        bad = max(vals) + 1000 if vals else 9999
        p = dict(ctx["base"])
        p[f["name"]] = bad
        out.append(_mk("B-ENUM-%s" % f["name"], "枚举越界 %s=%s（应被拒绝）" % (f["name"], bad),
                       "B", "参数校验-枚举", p,
                       {"success": False, "err_keywords": [f["name"]]},
                       priority="P0", source="pattern:P06",
                       note="Top6：越界值未被拒绝即缺陷(B3)"))
        out.append(_mk("K-ENUM-%s" % f["name"], "枚举释义遍历 %s（文档 vs 实现）" % f["name"],
                       "K", "契约-枚举释义", dict(ctx["base"]), {"success": True},
                       check="enum_exhaustive",
                       check_args={"field": f["name"], "documented": vals,
                                   "enum_map": {str(v): lbl for v, lbl in f["enum"]}},
                       priority="P1", source="pattern:P06",
                       note="Top6：读写一致性——列表能返回的值，写接口必须能接受"))
    return out


# ---------------------------------------------------------------- Top 7
def p07_match(c, ctx):
    return bool(_opt_fields(c))


def p07_gen(c, ctx):
    out = []
    # 限量按字段族保代表：族内字段共享后端校验分支，按位置取前 N 会把
    # 排在后面的整个族砍掉，等于一类分支零覆盖
    for f in pick_by_family(_opt_fields(c), discovery_profile.get("null_state_fields")):
        for label, val in (("缺失", "__OMIT__"), ("null", None), ("空串", ""), ("空数组", [])):
            p = dict(ctx["base"])
            if val == "__OMIT__":
                p.pop(f["name"], None)
            else:
                p[f["name"]] = val
            out.append(_mk("B-NULL-%s-%s" % (f["name"], label),
                           "可选字段 %s = %s（应成功，过度校验即缺陷）" % (f["name"], label),
                           "B", "参数校验-空值四态", p, {"success": True},
                           priority="P0", source="pattern:P07",
                           assertion_mode="null_triad",
                           note="Top7：可选字段被拒即 B8 过度校验缺陷"))
    return out


# ---------------------------------------------------------------- Top 8
def p08_match(c, ctx):
    # 幂等/并发重复提交对所有写操作都成立，不只是创建类——
    # 更新接口重复提交导致结果漂移、删除接口重复提交应幂等，都是真实缺陷面。
    return ctx.get("op_type") in ("创建", "修改", "删除")


def p08_gen(c, ctx):
    uniq = unique_key_field(c)
    if not uniq:
        # 契约没说清唯一键是什么 → 不猜（猜错的代价是一条 severity=高 的假缺陷）。
        # 但不因此把整条 AC 维度空着：幂等与并发重复提交**不依赖唯一键**也能测，
        # 只是判定要降为观察——创建类接口本就允许多条，硬判会把正常接口判成 bug。
        out = [
            _mk("AC03", "幂等：相同请求连续重复提交 2 次（结果应一致，不应产生重复对象）",
                "AC", "幂等与唯一性", dict(ctx["base"]), {"success": True},
                check="idempotent", check_args={"times": 2},
                priority="P1", source="pattern:P08",
                note="契约未声明唯一键，故只做观察不硬判：重复提交创建出多个不同对象时，"
                     "若接口声明了幂等键（request_id/幂等号）即为缺陷，否则属业务允许多条。"
                     "会真实产生测试数据，受写操作门禁约束"),
            _mk("AC04", "并发重复提交：同一请求并发 3 次（观察成功条数）",
                "AC", "幂等与唯一性", dict(ctx["base"]), {"success": True},
                check="concurrency", check_args={"times": 3},
                priority="P2", source="pattern:P08",
                note="未声明唯一键 → 只记观察（并发成功条数），不判缺陷；"
                     "若业务要求唯一，请在契约给唯一键字段标 unique: true，AC02 会自动硬判"),
        ]
        out.append(_mk("TODO-AC-唯一键", "AC 维度：契约未声明业务唯一键，需人工指明后补齐重复提交用例",
                       "AC", "待补充", dict(ctx["base"]), {"success": False},
                       enabled=False, needs="manual", priority="P1", source="pattern:P08",
                       note="契约无显式唯一键（字段名含 unique/uniq、契约标 unique=true、"
                            "或备注写明「唯一」/「不可重复」三者之一）。"
                            "在契约里给唯一键字段补 unique: true 即自动生成 AC01/AC02；"
                            "禁止拿 name 字段猜——同名对象是否合法是业务未知量"))
        # 唯一性是创建语义，非创建类接口不点这个待补（避免噪声）
        if ctx.get("op_type") != "创建":
            out = [x for x in out if x["id"] != "TODO-AC-唯一键"]
        return out
    p = dict(ctx["base"])
    return [
        _mk("AC01", "重复提交 %s（应被拒绝且回显冲突值）" % uniq["name"], "AC", "幂等与唯一性",
            p, {"success": False},
            check="uniqueness", check_args={"field": uniq["name"]},
            priority="P1", source="pattern:P08",
            note="Top8：重复未校验即缺陷(AC2)；错误信息须回显冲突值且对象名正确(AC4)"),
        _mk("AC02", "并发重复提交 %s：唯一键只应成功 1 次" % uniq["name"], "AC", "幂等与唯一性",
            dict(ctx["base"]), {"success": True},
            check="concurrency", check_args={"times": 3, "unique_field": uniq["name"]},
            priority="P1", source="pattern:P08",
            note="Top8 并发面：同一唯一键并发提交产生多条成功即缺陷(AC5/J2)。"
                 "给了 unique_field 才硬判——未声明唯一键时并发本就可能合法，硬判会造假缺陷"),
    ]


# ---------------------------------------------------------------- Top 9
def p09_match(c, ctx):
    return True


def p09_gen(c, ctx):
    return [_mk("J01", "限流可观测性：连发观测阈值与被限流时的响应语义", "J", "性能与稳定性",
                dict(ctx["base"]), {"success": True},
                check="rate_limit", check_args={"times": 5},
                priority="P2", source="pattern:P09",
                note="Top9：真实阈值需压测授权后测定；本用例先验证限流是否可被观测到"
                     "（含 429 / 明确提示 / 恢复时间）")]


# ---------------------------------------------------------------- Top 10
def p10_match(c, ctx):
    return bool(ctx.get("is_intl")) or any(
        set(f.get("semantics") or []) & {"time", "tz"}
        for f in (c.get("request_fields") or []) + (c.get("response_fields") or []))


def p10_gen(c, ctx):
    if not ctx.get("is_intl"):
        return [_mk("Z01", "时区一致性（国内版 N/A）", "Z", "国际化", dict(ctx["base"]),
                    {"success": True}, applicability="N/A", enabled=False,
                    source="pattern:P10",
                    note="Top10：N/A（非国际版）——未满足国际版启用条件，不计入覆盖率分母")]
    tf = next((f for f in (c.get("response_fields") or [])
               if set(f.get("semantics") or []) & {"time", "tz"}), None)
    if not tf:
        # 传空字段名去做检查会让断言恒过，等于假覆盖；留痕点名而不是假装测了
        return [_mk("Z01", "时区一致性（无时间字段可验）", "Z", "国际化",
                    dict(ctx["base"]), {"success": True},
                    enabled=False, needs="manual",
                    priority="P2", source="pattern:P10",
                    note="国际版接口但响应里没有可识别的时间字段，无法确定时区断言目标。"
                         "留痕而非传空字段执行——空字段的时区检查恒过，是假覆盖")]
    return [_mk("Z01", "时区一致性（UTC/+08:00/-05:00 各验一遍）", "Z", "国际化",
                dict(ctx["base"]), {"success": True},
                check="timezone", check_args={"field": tf["name"]},
                priority="P0", source="pattern:P10",
                note="Top10：与页面显示差固定小时数即为缺陷，不要归为 ENV")]


# ---------------------------------------------------------------- Top 11
def p11_match(c, ctx):
    return bool(c.get("response_fields"))


def p11_gen(c, ctx):
    return [_mk("K03", "响应字段命名风格一致性", "K", "契约-命名规范",
                dict(ctx["base"]), {"success": True},
                check="field_naming", priority="P1", source="pattern:P11",
                note="Top11：同一 API 混用驼峰/下划线即缺陷(K7)")]


# ---------------------------------------------------------------- Top 12
def p12_match(c, ctx):
    return bool(_opt_fields(c))


def p12_gen(c, ctx):
    """往返验证：把读接口返回的结构原样回写给写接口，必须能成功（最高发触发点）。

    read_action 从 ctx 取（--read-action 显式传入 / 契约声明），不自动猜——
    猜错读接口会调一个不存在的 action，把 ENV 失败伪装成"往返已验证"，
    比不做往返更糟。拿不到 read_action 时诚实退化为"写入后回读一致性"。
    """
    ra = ctx.get("read_action")
    check_args = {"fields": [f["name"] for f in (c.get("request_fields") or [])][:8]}
    note = ("Top12：历史最高发——读回写触发『XX field is required』。")
    if ra:
        rp = ctx.get("read_params") or {}
        check_args = {"read_action": ra, "read_params": rp}
        if rp:
            note += "本次做真往返：读接口 %s（带参数 %s）→ 原样回写本接口。" % (ra, sorted(rp))
        else:
            note += ("本次做真往返：读接口 %s。未提供 read_params，读接口若需资源 ID "
                     "会取不到数据，届时记观察不判缺陷。" % ra)
    else:
        note += ("当前按「写入后回读一致性」退化执行；用 --read-action 指定配套读接口 "
                 "即可升级为完整的读→原样回写→再回读往返验证。")
    return [_mk("B-RT01", "往返验证：读接口返回结构原样回写写接口（应成功）", "B", "参数校验-过度校验",
                dict(ctx["base"]), {"success": True},
                check="roundtrip", check_args=check_args,
                priority="P0", source="pattern:P12",
                assertion_mode="roundtrip", note=note)]


# ---------------------------------------------------------------- 补充 1 并发
def s1_match(c, ctx):
    return ctx.get("op_type") in ("创建", "修改", "删除")


def s1_gen(c, ctx):
    return [_mk("J02", "并发重复提交：不产生重复数据/不丢失", "J", "性能与并发",
                dict(ctx["base"]), {"success": True},
                check="concurrency", check_args={"times": 5},
                priority="P2", source="pattern:S1",
                note="补充：并发操作产生重复或丢失数据即缺陷(J2/AC5/AD9)")]


# ---------------------------------------------------------------- 补充 2 批量
def s2_match(c, ctx):
    return any((f.get("type") or "") in ("array", "list")
               or f["name"].lower().endswith(("ids", "list", "_ids"))
               for f in (c.get("request_fields") or []))


def s2_gen(c, ctx):
    """数组字段的部分成功语义。遍历所有命中字段——只取第一个会漏掉其余数组字段。"""
    hits = [f for f in (c.get("request_fields") or [])
            if (f.get("type") or "") in ("array", "list")
            or f["name"].lower().endswith(("ids", "list", "_ids"))]
    out = []
    for i, f in enumerate(hits):
        p = dict(ctx["base"])
        p[f["name"]] = ["valid_value", "invalid_value_@@"]
        out.append(_mk("X01" if i == 0 else "X%02d" % (i + 1),
                       "批量：%s 含一项非法时，其余项应仍生效（部分成功语义）" % f["name"],
                       "X", "批量操作", p, {"success": True},
                       check="batch_partial", priority="P1", source="pattern:S2",
                       note="补充：批量中一项有问题导致其余全改不了即缺陷(X1/Z3/U2)"))
    return out


# ---------------------------------------------------------------- 补充 3 终态
def _status_fields(c, key):
    return [f for f in (c.get(key) or []) if "status" in (f.get("semantics") or [])]


def s3_match(c, ctx):
    # 请求侧带状态字段同样命中：能写入状态就意味着存在转换语义，
    # 光看响应侧会把"只进不出"的状态写接口整条 E 维度漏掉。
    return ctx.get("op_type") == "删除" or any(
        "status" in (f.get("semantics") or [])
        for f in (c.get("response_fields") or []) + (c.get("request_fields") or []))


def s3_gen(c, ctx):
    out = [_mk("E01", "终态重复操作：已删除再删应明确提示，不得静默返回 true", "E", "状态机",
               dict(ctx["base"]), {"success": False},
               check="terminal_repeat", priority="P1", source="pattern:S3",
               needs="manual",
               note="补充：终态重复操作静默成功即缺陷(E6/E7)。"
                    "自动执行会对 base 参数指向的真实对象重复操作（删除/停用不可逆），"
                    "故不自动跑：请人工在受控数据上执行，或补一个专用测试对象后改 enabled=true")]

    # E02：状态字段往返一致性——写了什么状态就该读回什么状态。
    # 属于接口侧可自动判定的部分（不需要知道"哪些转换合法"，只需知道"值没被吞掉"）。
    req_status = _status_fields(c, "request_fields")[:STATE_MAX_FIELDS]
    for idx, sf in enumerate(req_status):
        name = sf["name"]
        ra = ctx.get("read_action")
        cargs = {"read_action": ra, "read_params": ctx.get("read_params") or {}} if ra else {"fields": [name]}
        out.append(_mk("E02" if idx == 0 else "E02-%s" % name,
                       "状态字段往返一致性：%s 写入值应原样回读" % name, "E", "状态机",
                       dict(ctx["base"]), {"success": True},
                       check="roundtrip", check_args=cargs,
                       priority="P1", source="pattern:S3",
                       note="状态字段被静默改写/忽略（写了 A 回读 B、或状态参数失效）即缺陷；"
                            "本用例只验「值有没有被吞掉」，不判定转换是否合法"))

    # E03：状态转换矩阵——哪些 (当前状态 → 目标状态) 合法是业务语义，自动生成器无法判定。
    # 但可以把契约里的状态取值枚举出来当矩阵骨架，人工只需勾选合法项，不必从零设计。
    resp_status = [f for f in _status_fields(c, "response_fields") if f.get("enum")]
    if resp_status:
        f = resp_status[0]
        vals = [v for v, _ in f["enum"]]
        out.append(_mk("E03", "状态转换矩阵：%s 的合法/非法转换组合（需人工确认后执行）" % f["name"],
                       "E", "待补充", dict(ctx["base"]), {"success": False},
                       enabled=False, needs="manual", priority="P1", source="pattern:S3",
                       note="矩阵骨架（契约声明的状态取值）：%s。"
                            "对每一对 (当前状态 → 目标状态) 判定合法/非法："
                            "非法转换须被拒绝且状态不变；合法转换后状态须可回读。"
                            "哪些转换合法属业务语义，自动生成器不猜——补齐后改 enabled=true" % vals))

    # E04：把转换矩阵**跑成事实表**（逐个目标状态提交 + 回读），人不从零设计。
    # 合法性仍不猜（那是 E03 的人工活），只硬判"接受但回读值被改写"这一确定缺陷。
    # v4.26.0：覆盖**所有**状态字段（而非只第一个），每个字段用它**自己**声明的枚举当目标值
    #（修掉旧版"请求字段名配响应枚举"的错配），受 STATE_MAX_FIELDS 上限约束。
    st_candidates = []
    for f in _status_fields(c, "request_fields") + _status_fields(c, "response_fields"):
        vals = [v for v, _ in (f.get("enum") or [])]
        if vals:
            st_candidates.append((f, vals))
    any_status = bool(_status_fields(c, "request_fields") or _status_fields(c, "response_fields"))
    st_candidates = st_candidates[:STATE_MAX_FIELDS]
    if st_candidates:
        for idx, (st_field, st_vals) in enumerate(st_candidates):
            out.append(_mk("E04" if idx == 0 else "E04-%s" % st_field["name"],
                           "状态转换矩阵实测：%s 逐个目标状态提交并回读" % st_field["name"],
                           "E", "状态机", dict(ctx["base"]), {"success": True},
                           check="state_transition",
                           check_args={"field": st_field["name"], "values": st_vals[:8],
                                       "read_action": ctx.get("read_action"),
                                       "read_params": ctx.get("read_params") or {}},
                           priority="P1", source="pattern:S3",
                           note="用例本身按 base 参数跑（正常请求），矩阵由 check 逐个状态实测。"
                                "产出事实表：哪些目标状态被接受/被拒、接受后回读值；"
                                "合法性按业务状态图人工核对（见 E03）。"
                                "硬判的只有「接受但回读值被改写」——确定的状态参数失效缺陷"))
    elif any_status:
        # 有状态字段但契约没给取值枚举 → 矩阵无从构造。点名待补，不静默当"已覆盖 E"。
        name = (_status_fields(c, "request_fields") or _status_fields(c, "response_fields"))[0]["name"]
        out.append(_mk("TODO-E-状态枚举",
                       "E 维度：状态字段 %s 未声明取值枚举，转换矩阵需人工补充" % name,
                       "E", "待补充", dict(ctx["base"]), {"success": False},
                       enabled=False, needs="manual", priority="P1", source="pattern:S3",
                       note="契约里有状态字段但没写取值枚举（enum），自动生成器拿不到目标状态清单，"
                            "无法逐个提交跑转换矩阵。补上枚举（或在备注里写明取值如 0/1/2/3）后重跑，"
                            "E04 会自动生成"))
    return out


# ---------------------------------------------------------------- 补充 4 语义无效
_URLISH = ("url", "relative_url", "domain")


def s4_match(c, ctx):
    fs = c.get("response_fields") or []
    # 只有编号类（code）不算——那是标识符不是链接，直连必然失败。
    # 仅有相对路径/域名时也要匹配，用于产出人工占位留痕，不能静默跳过。
    return any(any(t in (f.get("semantics") or []) for t in _URLISH) for f in fs)


def s4_gen(c, ctx):
    fs = c.get("response_fields") or []
    absolute = [f for f in fs if is_absolute_url(f)]
    out = []
    for i, f in enumerate(pick_by_family(absolute, discovery_profile.get("url_fields")), 1):
        out.append(_mk("C%02d" % i, "响应 URL 字段语义有效性（真能打开）：%s" % f["name"],
                       "C", "字段语义真值", dict(ctx["base"]), {"success": True},
                       check="url_alive", check_args={"field": f["name"]},
                       priority="P0", source="pattern:S4",
                       note="补充：字段有值但无业务含义（URL 打不开）即缺陷(C4)"))
    if not out:
        # 全是相对路径或域名前缀：直连必然失败，自动验证只会报假缺陷，
        # 改为人工占位，把覆盖缺口点名出来而不是假装验过。
        names = [f["name"] for f in fs
                 if any(t in (f.get("semantics") or []) for t in ("relative_url", "domain"))]
        out.append(_mk(
            "C01", "响应 URL 字段语义有效性（需拼接域名后验证）", "C", "字段语义真值",
            dict(ctx["base"]), {"success": True},
            enabled=False, needs="manual", priority="P1", source="pattern:S4",
            note="响应中的 URL 字段（%s）均为相对路径或域名前缀，直连必然失败报假缺陷；"
                 "需与域名拼接后再验可达性" % "、".join(names)))
    return out


# ---------------------------------------------------------------- 补充 5 大字段
def s5_match(c, ctx):
    return any((f.get("max_len") or 0) >= 10000 for f in (c.get("request_fields") or []))


def s5_gen(c, ctx):
    """大字段性能。遍历所有命中字段——只取第一个会漏掉其余大字段。"""
    hits = [f for f in (c.get("request_fields") or []) if (f.get("max_len") or 0) >= 10000]
    out = []
    for i, f in enumerate(hits):
        p = dict(ctx["base"])
        p[f["name"]] = "x" * min(f["max_len"], discovery_profile.get("big_cap"))
        out.append(_mk("J03" if i == 0 else "J%02d" % (i + 3),
                       "大字段：%s 接近长度上限时保存不应超时/失败" % f["name"],
                       "J", "性能与稳定性", p, {"success": True},
                       priority="P2", source="pattern:S5",
                       note="补充：内容较长时保存极慢或一直不成功即缺陷(J1/J4)"))
    return out


# ---------------------------------------------------------------- 补充 6 HTTP 语义
def s6_match(c, ctx):
    return True


def s6_gen(c, ctx):
    p = dict(ctx["base"])
    p["__invalid_param__"] = 1
    return [_mk("H02", "HTTP 状态码语义：参数错误不应返回 200", "H", "错误处理-状态语义",
                p, {"success": False},
                check="http_semantics", check_args={"expected_http": 400},
                priority="P1", source="pattern:S6",
                note="补充：参数错误返回 200、401 与 403 混用即缺陷(§9.7)")]


# ---------------------------------------------------------------- 补充 7 命名三方比对
def s7_match(c, ctx):
    return bool(c.get("request_fields") and c.get("response_fields"))


def s7_gen(c, ctx):
    return [_mk("K04", "请求↔响应字段名三方比对（文档/接口/前端）", "K", "契约-命名规范",
                dict(ctx["base"]), {"success": True},
                check="name_consistency",
                check_args={"request": [f["name"] for f in (c.get("request_fields") or [])],
                            "response": [f["name"] for f in (c.get("response_fields") or [])]},
                priority="P2", source="pattern:S7",
                note="补充：接口字段名与文档/前端/数据库命名不一致即缺陷(K7)")]


# ---------------------------------------------------------------- AA 静默错误专项
def aa_match(c, ctx):
    return bool(ctx.get("is_list_query")) or "AA" in (ctx.get("coverage_plan") or [])


def aa_gen(c, ctx):
    """分页一致性。list_action 可由 --list-action 显式指定：
    被测接口本身是列表接口时无需指定（执行期自动回退到 case.action），
    被测的是"列表的筛选/排序入口"而非列表接口本身时才需要显式给。"""
    la = ctx.get("list_action")
    cargs = {"list_action": la} if la else {}
    note = "历史缺陷：筛选条件无效时严禁静默返回全量；深翻页与排序稳定性一并验"
    if la:
        note += "（列表接口=%s，由 --list-action 指定）" % la
    return [_mk("AA01", "分页边界与筛选真生效（防静默返回全量）", "AA", "分页与列表",
                dict(ctx["base"]), {"success": True},
                check="pagination", check_args=cargs,
                priority="P0" if ctx.get("is_list_query") else "P2",
                source="pattern:AA", note=note)]


# ------------------------------------- 接口侧 UI 代理信号（UI1，非视觉走查）
# 布局/间距/字体/颜色/响应式属视觉比对范畴，不在本 skill 内。
# 但以下四类**接口侧可自动发现**的问题会直接导致前端错版，且不需要页面基线：
#   ① 资源 URL 不可用 / 类型不符 / 尺寸为 0  → 图片裂开、版面塌陷
#   ② 富文本 / HTML / 特殊字符往返被转义或截断 → 内容错版、乱码
#   ③ 声明字段返回空值 → 空白区域、占位缺失、默认值错
#   ④ 超长值被静默截断 → 文本溢出或截断显示
UI_PROXY_PARAMS = (
    ("emoji", "😀🎉👍", "emoji 往返（乱码/落库被截断会直接显示异常）"),
    ("zero_width", "a\u200bb\u200dc", "零宽字符往返（前端宽度计算异常导致错位）"),
    ("html", "<b>加粗</b>&amp;", "HTML 往返（被转义则前端显示原始标签）"),
    ("long", "长文本内容" * 40, "超长值往返（被静默截断则前端显示不全）"),
)
UI_TOLERATE_REJECT = ["安全", "违规", "敏感", "禁止", "非法", "不允许", "script", "未通过",
                      "长度", "超长", "过长", "不能超过", "太长", "字符"]
UI_URLISH_HINTS = ("url", "link", "img", "image", "pic", "photo", "avatar", "cover",
                   "logo", "file", "attach", "download", "qr", "banner")


def ui1_match(c, ctx):
    return bool(c.get("request_fields"))


def ui1_gen(c, ctx):
    fields = c.get("request_fields") or []
    str_fields = [f for f in fields
                  if (f.get("type") or "string").lower() in ("string", "str", "text", "")]
    rich_hint = ("content", "html", "rich", "text", "desc", "body", "remark")
    str_fields.sort(key=lambda f: 0 if any(h in f["name"].lower() for h in rich_hint) else 1)
    # 目标字段同样按族选：两个名额优先给不同族（如 html_content 与 title），
    # 而不是把两个名额花在同族的两个字段上
    targets = [f["name"] for f in pick_by_family(str_fields, discovery_profile.get("ui_proxy_fields"))] \
        or ([fields[0]["name"]] if fields else [])
    resp_fields = [f["name"] for f in (c.get("response_fields") or [])]
    cases = []
    for target in targets:
        fdef = next((f for f in fields if f["name"] == target), {})
        max_len = fdef.get("max_length") or fdef.get("maxLength") or fdef.get("max")
        for key, value, why in UI_PROXY_PARAMS:
            if key == "long" and max_len:
                try:
                    value = value[:int(max_len)]
                except Exception:
                    pass
            params = dict(ctx["base"])
            params[target] = value
            cases.append(_mk("UI1-%s-%s" % (key, target), "UI 代理：%s" % why, "C", "接口侧UI代理",
                             params,
                             {"success": True, "tolerate_reject": UI_TOLERATE_REJECT},
                             check="roundtrip", check_args={"fields": [target]},
                             priority="P1", source="pattern:UI1",
                             note="特殊字符被转义/截断是前端错版与乱码的高发前因；"
                                  "被内容安全/长度策略拦下按「符合预期」结算；"
                                  "响应不回显该字段时记缺口（可在 check_args 补 read_action 做真往返）"))
    urlish = [f for f in resp_fields if any(h in f.lower() for h in UI_URLISH_HINTS)]
    if urlish:
        cases.append(_mk("UI1-url", "UI 代理：资源链接可用性与类型/尺寸（图片裂开前因）",
                         "C", "接口侧UI代理", dict(ctx["base"]), {"success": True},
                         check="url_alive",
                         check_args={"field": urlish[0],
                                     "expect_kind": "image" if any(
                                         h in urlish[0].lower()
                                         for h in ("img", "image", "pic", "photo",
                                                   "avatar", "cover", "logo", "qr", "banner"))
                                     else ""},
                         priority="P1", source="pattern:UI1",
                         note="图片 404 / Content-Type 不符 / Content-Length=0 会直接导致前端裂图或版面塌陷"))
    if resp_fields:
        cases.append(_mk("UI1-degrade", "UI 代理：声明字段是否返回空值（空白区域/占位缺失前因）",
                         "Y", "接口侧UI代理", dict(ctx["base"]), {"success": True},
                         check="render_degrade", check_args={"fields": resp_fields},
                         priority="P1", source="pattern:UI1",
                         note="字段存在但为 null/空串，前端渲染为空白或占位缺失"))
    return cases


PATTERNS = [
    ("UI1", "接口侧 UI 还原代理信号（错版前因）", "C", ui1_match, ui1_gen),
    ("P01", "响应字段缺失或冗余", "K", p01_match, p01_gen),
    ("P02", "页面与接口行为不一致", "AB", p02_match, p02_gen),
    ("P03", "事件推送丢失/重复/载荷不准", "AD", p03_match, p03_gen),
    ("P04", "文档与实际不符", "K", p04_match, p04_gen),
    ("P05", "错误信息语言/渲染/根因掩盖", "H", p05_match, p05_gen),
    ("P06", "枚举释义与取值范围不符", "B", p06_match, p06_gen),
    ("P07", "空值被错误接受或错误拒绝", "B", p07_match, p07_gen),
    ("P08", "重复数据未校验/未去重", "AC", p08_match, p08_gen),
    ("P09", "限流配额归属与阈值不符", "J", p09_match, p09_gen),
    ("P10", "时区未转换（国际版专项）", "Z", p10_match, p10_gen),
    ("P11", "字段命名风格不统一", "K", p11_match, p11_gen),
    ("P12", "可选字段被强制必填", "B", p12_match, p12_gen),
    ("S1", "并发重复与丢失", "J", s1_match, s1_gen),
    ("S2", "批量部分失败导致整体失败", "X", s2_match, s2_gen),
    ("S3", "终态重复操作静默成功", "E", s3_match, s3_gen),
    ("S4", "响应字段语义无效", "C", s4_match, s4_gen),
    ("S5", "大字段导致超时", "J", s5_match, s5_gen),
    ("S6", "HTTP 状态码语义错用", "H", s6_match, s6_gen),
    ("S7", "接口与数据库命名不一致", "K", s7_match, s7_gen),
    ("AA", "分页筛选静默失效", "AA", aa_match, aa_gen),
]


def run(contract, ctx):
    """对契约跑一遍全部模式，返回 (cases, matched)。matched 为命中的模式摘要，便于报告溯源。"""
    cases, matched = [], []
    for pid, pname, dim, m, g in PATTERNS:
        try:
            if not m(contract, ctx):
                continue
        except Exception:
            continue
        try:
            got = g(contract, ctx) or []
        except Exception:
            got = []
        for c in got:
            c["action"] = c.get("action") or contract.get("action")
        cases += got
        matched.append({"id": pid, "name": pname, "dimension": dim, "cases": len(got)})
    return cases, matched
