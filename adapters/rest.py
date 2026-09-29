# -*- coding: utf-8 -*-
from .base import ApiAdapter, Request, Response, TransportError


class RestAdapter(ApiAdapter):
    """通用 REST 适配器。

    transport 可延迟注入：编排层（如 flow.py）可能只需要适配器做
    「写操作门禁 / 请求构造」判断而不真正发送请求，此时可不传 transport；
    一旦调用 send() 而未注入 transport，会明确报错而不是静默失败。
    """

    def __init__(self, transport=None):
        self.transport = transport

    def build_request(self, case, env):
        return Request(
            method=env.get("method", "POST"),
            url=env["base_url"],
            body=dict(case.get("params") or {}),
            headers=dict(env.get("headers") or {"Content-Type": "application/json"}),
            timeout=float(env.get("timeout", 30)),
        )

    def send(self, request):
        try:
            return self.transport(request)
        except Exception as exc:
            raise TransportError(str(exc)) from exc

    def classify_response(self, response):
        if response.status_code >= 500:
            return {"status": "server_error"}
        if response.status_code == 429:
            return {"status": "rate_limited"}
        if 400 <= response.status_code < 500:
            return {"status": "client_error"}
        return {"status": "ok"}
