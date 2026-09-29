# -*- coding: utf-8 -*-
"""可审计、确定性的接口风险评分。"""
from __future__ import annotations
from contract_model import ContractModel


def score(model: ContractModel, history=None):
    history = history or []
    breakdown = {}
    text = " ".join([model.name, model.operation or "", model.path]).lower()
    if any(w in text for w in ("create", "update", "delete", "publish", "创建", "更新", "删除", "发布")):
        breakdown["write_operation"] = 25
    if any(w in text for w in ("delete", "publish", "删除", "发布")):
        breakdown["destructive_or_state_change"] = 25
    if any(any(k in f.name.lower() for k in ("role", "user", "account", "permission", "appid")) for f in model.fields):
        breakdown["auth_sensitive_field"] = 20
    if any(f.name.lower() in ("status", "state") for f in model.fields):
        breakdown["state_field"] = 15
    # 只认**真的**跨接口依赖：契约正文显式声明的 depends_on / 配套读接口。
    # 过去这里的判据是"relations 非空"，而 relationship_inference 依据接口名里的
    # create/update/get 等字样就会产出关系——那度量的是"本接口自身是写入类"，
    # 与"要依赖另一个接口"是两回事，导致大部分契约白拿 10 分、失去区分度，
    # 还会让计划里出现"0 条依赖却计 10 分跨接口依赖"的自相矛盾。
    if any(getattr(r, "relation_type", "") in ("depends_on", "companion_read")
           for r in model.relations):
        breakdown["cross_api_dependency"] = 10
    if history:
        breakdown["historical_issue"] = 15
    total = sum(breakdown.values())
    return {"score": total, "level": "high" if total >= 50 else "medium" if total >= 25 else "low", "breakdown": breakdown}
