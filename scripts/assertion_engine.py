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
    ok = status_code == wanted if wanted is not None else 200 <= status_code < 300
    if status_code >= 500:
        ok = False
    return AssertionResult(ok, "HTTP_SEMANTICS", "status=%s expected=%s" % (status_code, wanted), {"status_code": status_code, "expected": wanted})


def assert_contract(body: Dict[str, Any], required_fields: List[str]) -> AssertionResult:
    missing = [name for name in required_fields if name not in body]
    return AssertionResult(not missing, "RESPONSE_CONTRACT", "missing=%s" % missing, {"missing": missing})


def assert_error_keywords(body: Any, keywords: List[str]) -> AssertionResult:
    text = str(body)
    missing = [word for word in keywords if word not in text]
    return AssertionResult(not missing, "ERROR_SEMANTICS", "missing=%s" % missing, {"missing": missing})
