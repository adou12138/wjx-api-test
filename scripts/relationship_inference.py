# -*- coding: utf-8 -*-
"""从契约模型中推断资源关系和状态语义。"""
from __future__ import annotations
import re
from typing import List
from contract_model import ContractModel, RelationModel, Evidence


def infer_relations(model: ContractModel) -> List[RelationModel]:
    text = " ".join([model.name, model.operation or "", model.path]).lower()
    fields = {f.name.lower() for f in model.fields}
    resource = next((f.name[:-3] for f in model.fields if f.name.lower().endswith("_id")), "resource")
    relations = []
    for kind, words in (("create", ("create", "创建", "add", "新增")), ("update", ("update", "更新", "edit", "修改")), ("delete", ("delete", "删除", "remove")), ("read", ("get", "query", "详情", "查询"))):
        if any(w in text for w in words):
            relations.append(RelationModel(kind, resource, confidence=0.75, reason="operation/path/name keyword"))
    if any("status" in f for f in fields):
        relations.append(RelationModel("state_machine", resource, confidence=0.85, reason="status field present"))
    return relations
