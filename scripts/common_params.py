# -*- coding: utf-8 -*-
"""公共参数（encode / appid / action / sign / ts）拼装规则的单一来源。

深度流程（api_test_runner）与冒烟（smoke_runner）共用同一套规则：
完整请求报文 = 公共参数 + action + 业务参数，且后传的业务参数覆盖公共参数。
独立脚本可用 complete_request_body() 补齐台账「请求Body」列，免于漏掉公共参数（陷阱 18）。

本模块只依赖标准库，不 import 任何运行器模块，避免循环依赖与导入期副作用。
"""

import time

# 台账「请求Body」中 ts 的占位写法：ts 每次请求都不同，不写死具体取值。
TS_PLACEHOLDER = "<请求时unix时间戳>"


def generate_ts():
    """当前 unix 时间戳（字符串），供需要动态 ts 的调用方复用。"""
    return str(int(time.time()))


def build_request_payload(action, *, encode=None, appid=None, sign=None,
                          ts_value=None, action_param="action", extra=None):
    """组装完整请求报文：公共参数 → action → 业务参数（后传覆盖前值）。

    只写入「已提供」的公共参数（值为 None 视为未提供、不落键），
    因此空配置不会凭空塞入 null 字段——与各调用方的既有语义保持一致。
    """
    payload = {}
    for key, value in (("encode", encode), ("appid", appid),
                       (action_param, action), ("ts", ts_value), ("sign", sign)):
        if value is not None:
            payload[key] = value
    payload.update(extra or {})
    return payload


def complete_request_body(body, *, encode=None, appid=None, sign=None,
                          ts_placeholder=TS_PLACEHOLDER):
    """给「原始请求报文」（含 action 键）补齐**缺失**的公共参数，不覆盖已显式提供的值。

    返回补齐后的新 dict，不修改入参；非报文（无 action 键）原样返回。
    """
    if not isinstance(body, dict) or "action" not in body:
        return body
    merged = dict(body)
    for k, v in (("encode", encode), ("appid", appid), ("sign", sign), ("ts", ts_placeholder)):
        if k not in merged:
            merged[k] = v
    return merged
