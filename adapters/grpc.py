# -*- coding: utf-8 -*-
"""gRPC 适配器：懒加载依赖，缺失时明确报错（不静默降级）。

依赖：grpcio + grpcio-tools（编译 .proto 生成 stub）。未安装时实例化即报错，
符合本 skill「缺必需依赖启动即报错，不带默认值跑」的防呆哲学。

case["params"] 约定：
  {"service": "pkg.UserService", "method": "GetUser", "message": {...}}
env 约定：
  {"base_url": "grpc://host:50051", "proto_dir": "./protos", "timeout": 30}

说明：gRPC 没有 HTTP 状态码，classify_response 按 gRPC 状态码归类；
调用层需先用 grpcio-tools 把 .proto 编译为 stub（python -m grpc_tools.protoc）。
"""
from .base import ApiAdapter, Request, Response, TransportError


def _require_grpc():
    try:
        import grpc  # noqa: F401
        return grpc
    except ImportError:
        raise TransportError(
            "gRPC 适配器缺少依赖：pip install grpcio grpcio-tools，"
            "并用 python -m grpc_tools.protoc 编译 .proto 后重试")


class GrpcAdapter(ApiAdapter):

    def __init__(self, transport=None):
        # 实例化即校验依赖：缺 grpcio 立刻报错，而不是跑到一半才发现
        self._grpc = _require_grpc()
        self.transport = transport

    def build_request(self, case, env):
        params = case.get("params") or {}
        if not params.get("service") or not params.get("method"):
            raise ValueError("gRPC 用例必须提供 params.service 与 params.method")
        return Request(
            method="GRPC",
            url=env["base_url"],
            body={
                "service": params["service"],
                "method": params["method"],
                "message": params.get("message") or {},
                "proto_dir": env.get("proto_dir", ""),
            },
            headers=dict(env.get("headers") or {}),
            timeout=float(env.get("timeout", 30)),
        )

    def send(self, request):
        if self.transport is None:
            raise TransportError("GrpcAdapter 未注入 transport，无法发送请求")
        try:
            return self.transport(request)
        except Exception as exc:
            raise TransportError(str(exc)) from exc

    def classify_response(self, response):
        # gRPC 状态码映射到统一分类（status_code 由 transport 层填 gRPC code 数值）
        code = getattr(response, "status_code", 0)
        if code == 0:
            return {"status": "ok"}
        if code == 4:
            # 【W37】DEADLINE_EXCEEDED 是超时不是限流——旧版与 RESOURCE_EXHAUSTED
            # 一起归 rate_limited，会被「节流重试」逻辑吃掉，掩盖真实延迟缺陷。
            return {"status": "timeout", "grpc_code": 4,
                    "message": "DEADLINE_EXCEEDED（超时，应查延迟而非节流重试）"}
        if code == 8:               # RESOURCE_EXHAUSTED
            return {"status": "rate_limited"}
        if code in (3, 5, 7, 9, 16):  # INVALID_ARGUMENT/NOT_FOUND/PERMISSION_DENIED/FAILED_PRECONDITION/UNAUTHENTICATED
            return {"status": "client_error"}
        return {"status": "server_error"}
