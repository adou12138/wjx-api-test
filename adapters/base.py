# -*- coding: utf-8 -*-
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


class TransportError(RuntimeError):
    pass


@dataclass
class Request:
    method: str
    url: str
    body: Dict[str, Any] = field(default_factory=dict)
    headers: Dict[str, str] = field(default_factory=dict)
    timeout: float = 30.0


@dataclass
class Response:
    status_code: int
    body: Any
    headers: Dict[str, str] = field(default_factory=dict)
    request_id: Optional[str] = None
    trace_id: Optional[str] = None


class ApiAdapter:
    def build_request(self, case, env) -> Request:
        raise NotImplementedError

    def send(self, request) -> Response:
        raise NotImplementedError

    def classify_response(self, response):
        return {"status": "unknown", "response": response}

    def create_fixture(self, case, env):
        return {"status": "unsupported", "reason": "fixture factory is not configured"}

    def verify_resource(self, resource, case, env):
        return {"status": "unsupported", "reason": "verification is not configured"}

    def cleanup_fixture(self, resource, env):
        return {"status": "unsupported", "reason": "cleanup is not configured"}
