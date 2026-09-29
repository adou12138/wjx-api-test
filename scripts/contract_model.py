# -*- coding: utf-8 -*-
"""证据驱动的接口契约中间表示。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from typing import Any, Dict, List, Optional


@dataclass
class Evidence:
    source: str
    line: Optional[int] = None
    confidence: float = 1.0
    excerpt: str = ""

    def __post_init__(self):
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("evidence confidence must be between 0 and 1")
        self.confidence = float(self.confidence)


@dataclass
class FieldModel:
    name: str
    location: str = "body"
    type: str = "string"
    required: bool = False
    nullable: bool = False
    default: Any = None
    enum: List[Any] = field(default_factory=list)
    bounds: Dict[str, Any] = field(default_factory=dict)
    semantics: List[str] = field(default_factory=list)
    evidence: List[Evidence] = field(default_factory=list)


@dataclass
class RelationModel:
    relation_type: str
    resource: str
    target_operation: Optional[str] = None
    confidence: float = 1.0
    reason: str = ""
    evidence: List[Evidence] = field(default_factory=list)


@dataclass
class ContractModel:
    operation: Optional[str] = None
    method: str = "POST"
    path: str = ""
    protocol: str = "http"
    name: str = ""
    endpoint: str = ""
    fields: List[FieldModel] = field(default_factory=list)
    relations: List[RelationModel] = field(default_factory=list)
    auth: Dict[str, Any] = field(default_factory=dict)
    response: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ContractModel":
        fields = [FieldModel(**{**item, "evidence": [Evidence(**e) for e in item.get("evidence", [])]})
                  for item in data.get("fields", [])]
        relations = [RelationModel(**{**item, "evidence": [Evidence(**e) for e in item.get("evidence", [])]})
                     for item in data.get("relations", [])]
        values = dict(data)
        values["fields"] = fields
        values["relations"] = relations
        return cls(**values)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "ContractModel":
        return cls.from_dict(json.loads(text))


def _evidence(source: str, confidence: float = 0.9, line: Optional[int] = None) -> List[Evidence]:
    return [Evidence(source=source, line=line, confidence=confidence)]


def load_contract_model(path: str, action: Optional[str] = None) -> ContractModel:
    """调用现有解析器并转换为中间模型；保留旧解析器作为单一文档入口。"""
    try:
        from contract_parser import load_contract
    except ImportError:
        from .contract_parser import load_contract
    contract = load_contract(path, action=action)
    endpoint = contract.get("endpoint") or ""
    path_part = endpoint
    if "://" in endpoint:
        path_part = "/" + endpoint.split("://", 1)[1].split("/", 1)[1] if "/" in endpoint.split("://", 1)[1] else "/"
    source = contract.get("source") or path
    fields: List[FieldModel] = []
    for item in contract.get("request_fields", []):
        fields.append(FieldModel(
            name=item.get("name", ""), type=item.get("type", "string"),
            required=bool(item.get("required")), default=item.get("default"),
            enum=[v for v, _ in item.get("enum", [])] if item.get("enum") else [],
            bounds={k: item[k] for k in ("min_len", "max_len") if item.get(k) is not None},
            semantics=list(item.get("semantics") or []), evidence=_evidence(source, 0.9),
        ))
    response_fields = contract.get("response_fields", [])
    return ContractModel(
        operation=str(contract.get("action") or action or "") or None,
        method=contract.get("method", "POST"), path=path_part,
        name=contract.get("name", ""), endpoint=endpoint, fields=fields,
        response={"fields": response_fields}, metadata={"source": source, "format": contract.get("format")},
    )
