#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""大模型语义层事实 → 用例（兑付层）。

`llm_semantics` 负责把事实**校验后并入**契约，本脚本负责把并入的事实**兑付成用例**。
两者分开的理由：并入回答「有没有」，兑付回答「用不用」。分开后可以单独度量兑付率
（并入 N 条事实 → 产出 M 条用例），避免事实并进了却没人消费、还以为接好了。

消费面（每类只在契约确实给出该事实时才产出，无事实则整类不出现）：
  fallback_chain  取值兜底链：目标字段未传时的取值来源与最终默认值（relation 级与
                  字段级两种表达，同一对字段只兑付一次）
  alias           兼容别名：别名与主字段等价，两条路径都应生效（方向由 verify 的
                  role/alias_of 机械判定；判不出转 unknowns 留痕）
  forbid          禁用词：传入契约禁止的内容应被拒绝（仅 evidence 有据的词）
  default         默认值：字段未传应落到声明默认值（仅 evidence 有据的默认值）
  publish_flag    开关字段：契约声明的默认值自动断言，另一取值只留痕（契约未说可否显式传入）
  state           状态字段枚举在契约外：只能留痕，禁止编造枚举值
  url_compose     域名 + 相对路径拼接：单字段都不是绝对 URL，拼接后才可验可达性

硬约束：凡契约没说的（`llm_unknowns` 里标 `must_not_assert` 的），一律不生成自动断言，
只生成 `needs=manual` 的留痕用例并在 note 里引用契约原文。
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from llm_semantics import _val_in_evidence  # noqa: E402,F401  单一事实源：有据判定不复制两份

# 响应侧表示状态/审核状态的角色
_ROLE_STATE = ("state", "audit_state")


def _mk(cid, name, dim, category, params, expect, **kw):
    """与 case_generator._mk 字段集保持一致（不含 cleanup，由 _enrich_cases 统一补）。"""
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


def _req_fields(contract):
    return contract.get("request_fields") or []


def _resp_fields(contract):
    return contract.get("response_fields") or []


def _relations(contract, rtype):
    return [r for r in (contract.get("llm_relations") or []) if r.get("type") == rtype]


# 默认值必须按**语义位置**定位。不能取第一个引号对——原文里常先出现别的引号内容
# （如禁用词「问卷星」），取第一个会把无关值当默认值输出。
_DEFAULT_HINT = re.compile(
    r'(?:默认|缺省|未传|为空|回退|fallback|使用|取值)[^"“”]{0,20}["“]([^"”]{1,40})["”]')


def _quoted(text):
    """从 evidence 原文里提取默认值字面量；提取不到返回空串。

    只在「默认 / 未传 / 为空 / 使用」等语义关键词之后取引号内容。取不到就返回空串，
    由调用方降级为「需回读确认」——不猜一个默认值出来，也不拿无关引号内容充数。
    """
    m = _DEFAULT_HINT.search(text or "")
    return m.group(1).strip() if m else ""


def _unknown_mentions(contract, *names):
    """unknowns 里是否有条目同时点到这些字段名（说明该组合的行为契约没说）。"""
    for u in (contract.get("llm_unknowns") or []):
        item = u.get("item") or ""
        if all(n in item for n in names):
            return u
    return None


# ---------------------------------------------------------------- 取值兜底链

def _fallback_chain_cases(contract, base, action, ctx):
    """目标字段未传时的取值来源与默认值。

    两个分支的取舍：
    - 「来源有值」分支的参数与 B 维度的 missing 态完全相同，只有在能回读时才值得
      单独发一次请求（否则只是重复一个断言相同的请求），故无回读通道时不生成。
    - 「来源也为空」分支走默认值，是 B 维度覆盖不到的分支，无论能否回读都生成。

    relation 级是首选表达；字段级 `fallback_chain` 是兜底表达（有些模型习惯挂在
    字段上）——同一对 (target, src) 两种来源只兑付一次，不去重会产出参数相同的
    重复用例。
    """
    out = []
    can_readback = bool(ctx.get("read_action") or ctx.get("read_params"))
    seen = set()

    def emit(target, src, evidence):
        if not target or not src or target == src:
            return
        if (target, src) in seen:
            return
        if target not in base or src not in base:
            return
        seen.add((target, src))
        default_val = _quoted(evidence)

        if can_readback:
            p = dict(base)
            p.pop(target, None)
            out.append(_mk(
                "LLM-FALLBACK-%s-FROMSRC" % target.upper(),
                "取值兜底·来源派生：%s 未传时应从 %s 派生取值" % (target, src),
                "C", "取值兜底链", p, {"success": True},
                action=action, priority="P1", source="llm_fact:fallback_chain",
                evidence=[evidence],
                note="契约原文：「%s」。本条验证 %s 未传时不被拒绝且取值来自 %s；"
                     "具体取值需回读确认，只验响应 success 不足以证明兜底生效。"
                     % (evidence, target, src)))

        p2 = dict(base)
        p2.pop(target, None)
        p2[src] = ""
        out.append(_mk(
            "LLM-FALLBACK-%s-EMPTY" % target.upper(),
            "取值兜底·终态默认：%s 与 %s 均为空时应落到契约声明的默认值"
            % (target, src),
            "C", "取值兜底链", p2, {"success": True},
            action=action, priority="P1", source="llm_fact:fallback_chain",
            evidence=[evidence],
            note="契约原文：「%s」。%s"
                 % (evidence,
                    "预期默认值为「%s」，需回读确认" % default_val if default_val
                    else "默认值未在原文中给出确切字面量，需回读确认实际落值")))

    for rel in _relations(contract, "fallback_chain"):
        pair = rel.get("between") or []
        if len(pair) < 2:
            continue
        emit(pair[0], pair[1], (rel.get("evidence") or "").strip())

    for f in _req_fields(contract):
        chain = (f.get("llm_constraints") or {}).get("fallback_chain")
        if not isinstance(chain, list) or not chain:
            continue
        target = f["name"]
        src = next((str(s).strip() for s in chain if str(s).strip() != target), None)
        emit(target, src, (f.get("llm_evidence") or "").strip())
    return out


# ---------------------------------------------------------------- 兼容别名

def _alias_cases(contract, base, action):
    """兼容别名与主字段等价：两条路径都应生效。

    别名方向以字段上的 `alias_of` 约束为准（指向主字段），不依赖 relations 里的
    数组顺序——顺序是模型输出习惯，不是契约事实。
    """
    out = []
    for f in _req_fields(contract):
        alias_of = (f.get("llm_constraints") or {}).get("alias_of")
        if not alias_of:
            continue
        an, cn = f["name"], alias_of
        if an not in base or cn not in base:
            continue
        evidence = (f.get("llm_evidence") or "").strip()

        p1 = dict(base)
        p1.pop(cn, None)
        out.append(_mk(
            "LLM-ALIAS-%s-ONLY" % an.upper(),
            "兼容别名·只走别名：仅传 %s（不传 %s）应同样生效" % (an, cn),
            "C", "兼容别名", p1, {"success": True},
            action=action, priority="P1", source="llm_fact:alias",
            evidence=[evidence],
            note="%s 被声明为 %s 的兼容参数（契约依据：「%s」）。只走别名路径若失败，"
                 "说明兼容层未实现——这类缺陷只测主字段永远抓不到。" % (an, cn, evidence)))

        p2 = dict(base)
        p2.pop(an, None)
        out.append(_mk(
            "LLM-ALIAS-%s-ONLY" % cn.upper(),
            "兼容别名·只走主字段：仅传 %s（不传 %s）应生效" % (cn, an),
            "C", "兼容别名", p2, {"success": True},
            action=action, priority="P2", source="llm_fact:alias",
            evidence=[evidence],
            note="对照组：两条路径都应可用，只跑一条无法区分「别名失效」与「接口整体不可用」"))

        unk = _unknown_mentions(contract, an, cn)
        if unk:
            p3 = dict(base)
            p3[an] = "alias_probe_value"
            out.append(_mk(
                "LLM-ALIAS-BOTH",
                "兼容别名·同时传入：%s 与 %s 传不同值时以谁为准（契约未声明）" % (an, cn),
                "C", "兼容别名", p3, {"success": True},
                action=action, priority="P2", source="llm_fact:alias",
                enabled=False, needs="manual",
                evidence=[evidence],
                consumed_unknowns=[unk.get("item")],
                note="契约未声明两者同时传入时的优先级（%s），属 must_not_assert："
                     "不得替契约假定某一方优先。需人工确认后补断言。" % (unk.get("reason") or "")))
    return out


# ---------------------------------------------------------------- 禁用词

def _forbid_cases(contract, base, action):
    """禁用词应被拒绝。

    只对 evidence 里真有该词的 forbid 约束生成自动断言——forbid 值在 evidence
    中找不到的，说明是模型从行业惯例推的（比如「标题字段通常禁脚本」），采信它
    就会产出契约里不存在的禁用词断言，即假缺陷。这类约束留在事实里不兑付，
    由 fact_utilization 的 archived 计数点名。
    """
    out = []
    for f in _req_fields(contract):
        forbid = (f.get("llm_constraints") or {}).get("forbid")
        if not isinstance(forbid, list) or not forbid:
            continue
        nm = f["name"]
        if nm not in base:
            continue
        evidence = (f.get("llm_evidence") or "").strip()
        made = 0
        for word in forbid:
            w = str(word).strip()
            if not w or not _val_in_evidence(evidence, w):
                continue
            made += 1
            p = dict(base)
            p[nm] = w
            out.append(_mk(
                "LLM-FORBID-%s-%02d" % (nm.upper(), made),
                "禁用词：%s 传入契约禁止的「%s」应被拒绝" % (nm, w),
                "B", "内容安全", p, {"success": False, "err_keywords": [nm]},
                action=action, priority="P1", source="llm_fact:forbid",
                evidence=[evidence],
                note="契约原文：「%s」明确禁止「%s」。传禁用词若被原样接受并入库，"
                     "即直接违反契约声明的内容约束。" % (evidence, w)))
    return out


# ---------------------------------------------------------------- 默认值

def _default_cases(contract, base, action):
    """契约声明的默认值：字段未传时应落到默认值。

    与 B 维度 missing 态的差异：参数虽然相同，但 B-OVER 只断「可选字段不传也应
    成功」，本条携带**具体默认值**这一契约事实——回读时可以直接比对落值是否
    等于声明的默认值，而不是只知道「成功」。同样只在 evidence 有据时兑付；
    无据 default（模型凭经验写的）不产出，避免把经验值当成契约值去比对。
    """
    out = []
    for f in _req_fields(contract):
        dv = (f.get("llm_constraints") or {}).get("default")
        if dv is None or dv == "":
            continue
        nm = f["name"]
        if nm not in base:
            continue
        evidence = (f.get("llm_evidence") or "").strip()
        if not _val_in_evidence(evidence, dv):
            continue
        p = dict(base)
        p.pop(nm, None)
        out.append(_mk(
            "LLM-DEFAULT-%s" % nm.upper(),
            "默认值：%s 未传时应落到契约声明的默认值「%s」" % (nm, dv),
            "C", "字段默认值", p, {"success": True},
            action=action, priority="P2", source="llm_fact:default",
            evidence=[evidence],
            note="契约原文：「%s」声明默认值为 %s。本条只验未传不被拒绝；"
                 "实际落值是否等于默认值需回读确认，不一致即文档或实现漂移。"
                 % (evidence, dv)))
    return out


# ---------------------------------------------------------------- 开关字段

def _flag_cases(contract, base, action):
    """发布/开关类字段：只对契约有依据的取值下自动断言。

    契约通常只声明默认值（如「默认不发布问卷」）。默认值必然是合法取值，可自动断言；
    另一个取值契约没说能不能显式传入，而 B 维度的四态（missing/null/empty/oversize）
    也不覆盖 true/false 双取值这一层。

    **不能用通用经验替契约下判断**：「只实现一个分支会造成能开不能关」是常见缺陷模式，
    但它不构成「契约允许传另一个取值」的依据。若服务端设计为「创建只建草稿、发布走
    独立接口」，对另一取值断言成功就是一条假缺陷——与本 skill「契约没说的一律不猜」
    的原则直接冲突。故另一取值一律降为留痕，人工确认后再补断言。
    """
    out = []
    for f in _req_fields(contract):
        if f.get("llm_role") != "publish_flag":
            continue
        nm = f["name"]
        if nm not in base:
            continue
        evidence = (f.get("llm_evidence") or "").strip()
        ev_note = evidence or "见 llm_evidence"
        default_val = (f.get("llm_constraints") or {}).get("default")

        if isinstance(default_val, bool):
            # 契约声明的默认值分支：默认值必然合法，可自动断言
            p = dict(base)
            p[nm] = default_val
            out.append(_mk(
                "LLM-FLAG-%s-%s" % (nm.upper(), str(default_val).lower()),
                "开关取值：%s=%s（契约默认值，应被接受）" % (nm, str(default_val).lower()),
                "C", "开关字段", p, {"success": True},
                action=action, priority="P2", source="llm_fact:publish_flag",
                evidence=[evidence] if evidence else [],
                note="契约声明默认值为 %s（契约依据：「%s」），默认值必然是合法取值，"
                     "故断言请求被接受；是否真生效仍需回读确认"
                     % (str(default_val).lower(), ev_note)))

        # 另一取值（或契约未给默认值时的任一取值）：契约未声明合法性，只留痕
        other = (not default_val) if isinstance(default_val, bool) else True
        p2 = dict(base)
        p2[nm] = other
        out.append(_mk(
            "LLM-FLAG-%s-GAP" % nm.upper(),
            "开关字段 %s：取值 %s 的合法性契约未声明，需人工确认" % (nm, str(other).lower()),
            "C", "开关字段", p2, {"success": True},
            action=action, priority="P2", source="llm_fact:publish_flag",
            enabled=False, needs="manual",
            evidence=[evidence] if evidence else [],
            note="契约原文：「%s」。该原文只声明了%s，**未声明**创建时可显式传 %s。"
                 "此取值是否合法属 must_not_assert：不得用「只实现一个分支会造成能开不能关」"
                 "这类通用经验替契约下判断——断言成功可能是误报。需人工确认后补断言。"
                 % (ev_note,
                    "默认值为 %s" % str(default_val).lower() if isinstance(default_val, bool)
                    else "该字段是开关语义",
                    str(other).lower())))
    return out


# ---------------------------------------------------------------- 状态字段枚举缺口

def _state_gap_cases(contract, base, action):
    """状态字段的枚举全集在契约外：只能留痕，禁止编造枚举值。

    契约已给出枚举时交给规则层遍历（那部分解析得很好且零成本），这里只处理
    「明知道有状态语义但取不到枚举」的情形——它是覆盖缺口，必须被点名而不是静默。
    """
    out = []
    for f in _resp_fields(contract):
        if f.get("llm_role") not in _ROLE_STATE:
            continue
        if f.get("enum"):
            continue
        nm = f["name"]
        evidence = (f.get("llm_evidence") or "").strip()
        # 字段名可能是子串（status 命中 verify_status 条目），优先挑真正讲枚举取值的那条
        hits = [u for u in (contract.get("llm_unknowns") or [])
                if nm in (u.get("item") or "")]
        unk = next((u for u in hits if "枚举" in (u.get("item") or "")),
                   hits[0] if hits else None)
        gap = ("未覆盖项：%s（%s）" % (unk.get("item"), unk.get("reason") or "契约未声明")
               if unk else "契约未给出取值全集")
        out.append(_mk(
            "LLM-STATE-%s" % nm.upper(),
            "状态枚举缺口：响应字段 %s 的取值全集在契约外，需取回后补枚举遍历" % nm,
            "E", "状态机", dict(base), {"success": True},
            action=action, priority="P2", source="llm_fact:state_gap",
            enabled=False, needs="manual",
            evidence=[evidence] if evidence else [],
            consumed_unknowns=[unk.get("item")] if unk else [],
            note="该字段被判定为状态语义（契约依据：「%s」），但取值全集在契约内不可得。"
                 "%s。属 must_not_assert：不得自行枚举取值去断言状态流转，"
                 "需先取回枚举全集再补用例。"
                 % (evidence or "见 llm_evidence", gap)))
    return out


# ---------------------------------------------------------------- URL 拼接

def _url_compose_cases(contract, base, action):
    """域名 + 相对路径：拼接后才构成可访问 URL。

    单字段都不是绝对 URL，直接拿去做可达性断言必然失败（这正是单字段误判的典型形态），
    所以这里给的是拼接公式与人工验证项，而不是一条会误报的自动用例。
    """
    out = []
    for rel in _relations(contract, "url_compose"):
        pair = rel.get("between") or []
        if len(pair) < 2:
            continue
        domain_f, path_f = pair[0], pair[1]
        evidence = (rel.get("evidence") or "").strip()
        # 拼接关系常是推断出来的（契约只分别说了「域名」和「相对路径」），不能当原文断言
        basis = ("契约未直接声明拼接，由「%s 是域名前缀」与「%s 是相对路径」两个事实"
                 "推出（confidence=%s）；相关原文：「%s」"
                 % (domain_f, path_f, rel.get("confidence") or "未标注", evidence)
                 if (rel.get("confidence") or "") == "inferred"
                 else "契约原文：「%s」" % evidence)
        out.append(_mk(
            "LLM-URLCOMPOSE-%s" % path_f.upper(),
            "URL 拼接：响应 %s + %s 拼接后应为可访问地址" % (domain_f, path_f),
            "C", "字段语义真值", dict(base), {"success": True},
            action=action, priority="P2", source="llm_fact:url_compose",
            enabled=False, needs="manual",
            evidence=[evidence],
            note="%s。%s 与 %s 都不是绝对 URL，单独做可达性断言必失败。"
                 "完整地址 = %s + %s，需拼接后验证；本轮无自动拼接断言，"
                 "故留痕而非生成会误报的用例。"
                 % (basis, domain_f, path_f, domain_f, path_f)))
    return out


def _unknown_gap_cases(contract, base, action, consumed):
    """未被其他兑付器消费的 unknowns：一律留痕，禁止断言。

    契约没说的事如果不在产物里点名，后面就可能被当成「可以猜」——猜出来的规则会变成
    假缺陷，而猜的人通常不知道自己在猜。留痕给的是「禁止断言什么 + 缺什么信息」，
    不是一条会误报的用例。
    """
    consumed = consumed or set()
    out = []
    for i, u in enumerate(contract.get("llm_unknowns") or []):
        item = (u.get("item") or "").strip()
        if not item or item in consumed:
            continue
        evidence = (u.get("evidence") or "").strip()
        out.append(_mk(
            "LLM-UNK-%02d" % (i + 1),
            "契约未声明项：%s（禁止断言，需补齐信息后补用例）" % item,
            "C", "契约缺口", dict(base), {"success": True},
            action=action, priority="P2", source="llm_fact:unknown_gap",
            enabled=False, needs="manual",
            evidence=[evidence] if evidence else [],
            note="语义层判定该项在契约内不可得：%s。属 must_not_assert："
                 "不得凭猜测补齐规则后再去断言——猜错就是一条假缺陷，而猜的人往往"
                 "不知道自己在猜。需人工确认或补充契约后再补用例。"
                 % ((u.get("reason") or "契约未声明").strip())))
    return out


def run(contract, base, action, ctx):
    """把所有已并入的语义事实兑付成用例。没有并入事实时返回空列表。

    判空必须看**全部三类事实**。只看 `llm_facts_count`（字段数）会让「只有关系事实」
    的输入整层静默不产出：`fallback_chain`、`url_compose` 的证据都挂在 relations 上，
    字段数完全可以是 0。这种漏不是少生成几条，而是这一层直接哑火且报告无提示。
    """
    if not (contract.get("llm_facts_count")
            or contract.get("llm_relations")
            or contract.get("llm_unknowns")):
        return []
    ctx = ctx or {}
    out = []
    out += _fallback_chain_cases(contract, base, action, ctx)
    out += _alias_cases(contract, base, action)
    out += _forbid_cases(contract, base, action)
    out += _default_cases(contract, base, action)
    out += _flag_cases(contract, base, action)
    out += _state_gap_cases(contract, base, action)
    out += _url_compose_cases(contract, base, action)
    # 已被上面兑付器引用过的 unknowns 不再重复留痕
    consumed = set()
    for c in out:
        consumed.update(c.get("consumed_unknowns") or [])
    out += _unknown_gap_cases(contract, base, action, consumed)
    return out
