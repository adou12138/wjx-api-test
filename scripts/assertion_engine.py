# -*- coding: utf-8 -*-
from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class AssertionResult:
    ok: bool
    code: str
    message: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)


def assert_http_semantics(status_code: int, semantic: str) -> AssertionResult:
    expected = {"validation": 400, "unauthenticated": 401, "forbidden": 403, "not_found": 404,
                "conflict": 409, "rate_limit": 429, "server_error": 500}
    wanted = expected.get(semantic)
    # 【审查修复·P2】旧版「wanted 精确匹配后又被 status>=500 兜底一律翻 False」自相
    # 矛盾：声明 semantic="server_error" 的容错类用例拿到真 500 也恒 FAIL——
    # 预期与实际完全一致却被记缺陷，整类假缺陷。5xx 兜底只应作用于**未声明精确
    # 语义**的分支（此时 2xx 才算成功，5xx 不该被当成功）。
    if wanted is not None:
        ok = status_code == wanted
    else:
        ok = 200 <= status_code < 300
        if status_code >= 500:
            ok = False
    return AssertionResult(ok, "HTTP_SEMANTICS", "status=%s expected=%s" % (status_code, wanted), {"status_code": status_code, "expected": wanted})


def assert_contract(body: Dict[str, Any], required_fields: List[str]) -> AssertionResult:
    # 【反假通过 W4】空集合循环 0 次恒 ok=True——「什么都没验」不等于「验证通过」。
    # 与 assert_non_empty 同源陷阱：空 required_fields 返回 VACUOUS，由调用方记覆盖缺口。
    if not required_fields:
        return AssertionResult(False, "VACUOUS",
                               "required_fields 为空：断言未验证任何字段，不计通过",
                               {"required_fields": []})
    missing = [name for name in required_fields if name not in body]
    return AssertionResult(not missing, "RESPONSE_CONTRACT", "missing=%s" % missing, {"missing": missing})


def assert_error_keywords(body: Any, keywords: List[str]) -> AssertionResult:
    # 【反假通过 W4】同上：空 keywords 恒 PASS 是假通过，返回 VACUOUS。
    if not keywords:
        return AssertionResult(False, "VACUOUS",
                               "keywords 为空：断言未验证任何关键词，不计通过",
                               {"keywords": []})
    text = str(body)
    missing = [word for word in keywords if word not in text]
    return AssertionResult(not missing, "ERROR_SEMANTICS", "missing=%s" % missing, {"missing": missing})
