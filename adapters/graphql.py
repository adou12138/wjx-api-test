# -*- coding: utf-8 -*-
"""GraphQL 适配器：走 HTTP POST，零新增依赖。

GraphQL 的关键差异：HTTP 200 不代表成功——业务错误放在 body.errors 里。
因此 classify_response 必须检查 errors 键，否则会把失败判成通过（假通过）。

case["params"] 约定：
  {"query": "...", "variables": {...}}   # query 必填，variables 可选
env 约定：
  {"base_url": "https://host/graphql", "headers": {...}, "timeout": 30}
"""
import json as _json

from .base import ApiAdapter, Request, Response, TransportError


class GraphqlAdapter(ApiAdapter):

    def __init__(self, transport=None):
        self.transport = transport

    def build_request(self, case, env):
        params = case.get("params") or {}
        query = params.get("query")
        if not query:
            raise ValueError("GraphQL 用例必须提供 params.query（GraphQL 语句）")
        body = {"query": query}
        if params.get("variables") is not None:
            body["variables"] = params["variables"]
        headers = dict(env.get("headers") or {})
        headers.setdefault("Content-Type", "application/json")
        return Request(
            method="POST",
            url=env["base_url"],
            body=body,
            headers=headers,
            timeout=float(env.get("timeout", 30)),
        )

    def send(self, request):
        if self.transport is None:
            raise TransportError("GraphqlAdapter 未注入 transport，无法发送请求")
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
        # HTTP 200 仍可能携带业务错误：GraphQL 把错误放在 body.errors
        body = response.body
        if isinstance(body, (bytes, bytearray)):
            try:
                body = _json.loads(body.decode("utf-8", errors="replace"))
            except ValueError:
                body = None
        elif isinstance(body, str):
            # 【审查修复·P1-7】主链路注入点传的是已解码 str（api_test_runner 把
            # raw 直接挂在 _Resp.body 上）——此前只认 bytes，str 既不解码也不是
            # dict，恒判 ok：「HTTP 200 + errors」照旧假通过，W36 防假通过机制
            # 整体失效。str 与 bytes 同口径尝试解析。
            try:
                body = _json.loads(body)
            except ValueError:
                body = None
        if isinstance(body, dict) and body.get("errors"):
            return {"status": "graphql_error", "errors": body["errors"]}
        return {"status": "ok"}
