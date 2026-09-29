# -*- coding: utf-8 -*-
"""把适用性、风险和输入可信度汇总为可执行测试计划。"""
from __future__ import annotations
from typing import Any, Dict, List


def build_plan(model, applicability=None, risk=None, assets=None,
               dependencies=None, dependency_unknowns=None):
    """汇总为可执行计划。

    `dependencies` 只承载契约正文**显式声明**的跨接口依赖（见 dependency_scan），
    不做名称相似性推断；`dependency_unknowns` 是"提到要先调接口但定不位"的留痕项。
    """
    applicability = applicability or {}
    risk = risk or {"score": 0, "level": "low", "breakdown": {}}
    assets = assets or []
    required = list(applicability.get("coverage_plan") or [])
    optional = list(applicability.get("optional_plan") or [])
    blocked = []
    # AB（前端-接口口径一致性）依赖页面基线：没有基线就不能凭空编预期值。
    # 注意必须同时检查 required 与 optional——历史缺陷是只比对了 optional，
    # 而 AB 实际落在 required 里，导致这段阻塞逻辑从未触发过（死代码）。
    if "AB" in required + optional and not any(
            getattr(a, "kind", "") == "ui_baseline" for a in assets):
        if "AB" in optional:
            optional.remove("AB")
        blocked.append({
            "dimension": "AB",
            "reason": "缺少页面基线",
            "action": "保留在必测维度并记【覆盖率缺口】；登记基线（references/ui_baseline.md）后方可执行硬断言",
        })
    inferred = []
    for field in model.fields:
        if field.evidence and max(e.confidence for e in field.evidence) < 0.8:
            inferred.append({"field": field.name, "confidence": max(e.confidence for e in field.evidence), "action": "exploratory_only"})
    return {
        "plan_id": "plan_auto",
        "operation": model.operation,
        "required": required,
        "optional": optional,
        "exploratory": [x["field"] for x in inferred],
        "blocked": blocked,
        "na_proofs": applicability.get("na_proofs", []),
        "risk": risk,
        "inferred_items": inferred,
        "dependencies": list(dependencies or []),
        "dependency_unknowns": list(dependency_unknowns or []),
    }
