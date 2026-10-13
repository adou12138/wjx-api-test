# -*- coding: utf-8 -*-
"""统一异常归因，先排除环境和测试系统因素再判业务缺陷。

**主链路（case_runner）与冒烟链路（smoke_runner）共用本模块**，避免两处各写一套
判定、结论互相打架。早期版本只有冒烟链路调用它，主链路的失败一律直接进缺陷台账，
于是"前置条件未满足"这类假缺陷无从拦截——本模块因此补上 `PRECONDITION_UNMET`。

判定顺序（自上而下，先到先得）：前置条件未满足 → 环境/网络 → 限流 → 最终一致性
→ 契约证据不足 → 缺陷。顺序不是随意排的：越靠前的类别越"不该报成缺陷"，
把前置判定放在最前面，才能保证它优先于"断言被违反 → DEFECT"这条兜底。
"""

# 前置条件未满足：本接口没问题，只是要先调另一个接口/先把对象置于某状态。
# 与「缺陷」的根本区别是它**有外部依据**——来自契约声明或一次真实执行留下的
# 已确认依赖记录，而不是从错误文案里猜出来的。
PRECONDITION_UNMET = "PRECONDITION_UNMET"


def _text_of(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        import json
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def match_precondition(response, confirmed_deps, action=None):
    """本次失败是否命中一条**已确认**的运行实证依赖，命中则返回该依赖。

    只接受 `status=confirmed` 的依赖（调用方负责过滤，此处再兜一层）。判据顺序：
      1. `evidence` 是本次响应原文的连续子串——最硬，因为它本来就摘自同类响应；
      2. 退一步按 target_action 号在响应里出现来匹配（响应文案可能改写过）。
    两条都不中即返回 None，不做任何相似性推断。

    为什么必须挂"已确认"这道闸：这个判定会让一条失败**从缺陷台账里消失**。
    判错的代价是一个真缺陷被静默吞掉，且外观上完全合理。所以候选依赖不参与判定。
    """
    if not response or not confirmed_deps:
        return None
    text = _text_of(response)
    if not text:
        return None
    subject = str(action or "").strip()
    for dep in confirmed_deps:
        if dep.get("status") != "confirmed":
            continue
        dep_subject = str(dep.get("subject_action") or "").strip()
        # 依赖声明了主体就必须对得上，避免一个接口的依赖吞掉别的接口的失败
        if dep_subject and subject and dep_subject != subject:
            continue
        ev = str(dep.get("evidence") or "").strip()
        if ev and ev in text:
            return dep
        target = str(dep.get("target_action") or "").strip()
        if target and target in text:
            return dep
    return None


def attribute(error=None, response=None, assertion_ok=True, contract_confidence=1.0,
              retries=0, action=None, confirmed_deps=None):
    """归因。返回 category / confidence / reason / next_action。

    `confirmed_deps` 传入**已确认**的运行实证依赖（来自 precondition_scan），
    不传时行为与接入前完全一致——增量接入，不影响任何既有链路。
    """
    text = str(error or "") + " " + _text_of(response)
    lower = text.lower()

    # 1) 前置条件未满足：优先级最高，见模块说明。
    dep = match_precondition(response, confirmed_deps or [], action=action)
    if dep is not None:
        target = dep.get("target_action") or "（未定位）"
        cond = dep.get("condition") or "已确认的前置条件"
        params = dep.get("params") or {}
        ptxt = "、".join("%s=%s" % (k, v) for k, v in params.items())
        return {
            "category": PRECONDITION_UNMET,
            "confidence": float(dep.get("confidence", 0.8) or 0.8),
            "reason": "前置条件未满足：需先调用接口 %s%s（%s）"
                      % (target, ("，参数 " + ptxt) if ptxt else "", cond),
            "next_action": "先满足前置条件再重跑；该失败不计为缺陷",
            "evidence_action": target,
        }

    if error and retries:
        return {"category": "ENV", "confidence": 0.95,
                "reason": "network error remained after retry", "next_action": "check environment"}
    if any(word in lower for word in ("rate limit", "too many", "限流", "频率过快", "防重")):
        return {"category": "RATE_LIMIT", "confidence": 0.92,
                "reason": "rate-limit or duplicate-submit signal", "next_action": "re-run with throttle"}
    if any(word in lower for word in ("sync", "eventually", "同步", "稍后")):
        return {"category": "CONSISTENCY_PENDING", "confidence": 0.7,
                "reason": "possible eventual consistency window", "next_action": "poll independent read"}
    if not assertion_ok and contract_confidence < 0.8:
        return {"category": "NEEDS_CONFIRMATION", "confidence": 0.55,
                "reason": "low-confidence contract evidence", "next_action": "confirm contract"}
    return {"category": "DEFECT", "confidence": contract_confidence,
            "reason": "assertion violated trusted contract", "next_action": "create finding"}


def detect_flaky(results):
    """稳定性检测（v4.29.0）：同一用例在结果集里出现多次且终态不一致 → 记"不稳定"。

    sound 边界：只在**同一用例确有多条记录**时才判；单次结果无从比较、不判。
    不重新执行（无重跑基础设施），只分析已有结果集——回归/重跑把多轮结果并进来时自动生效。
    返回 {case_id: [出现的不同终态]}，空 dict 表示无不稳定项。
    """
    by_case = {}
    for r in results or []:
        if not isinstance(r, dict):
            continue
        cid = r.get("case_no") or r.get("id") or r.get("cid")
        st = r.get("status")
        if cid and st:
            by_case.setdefault(str(cid), []).append(str(st))
    flaky = {}
    for cid, sts in by_case.items():
        uniq = sorted(set(sts))
        if len(uniq) > 1:
            flaky[cid] = uniq
    return flaky
