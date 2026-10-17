# -*- coding: utf-8 -*-
"""发现档（discovery profile）：把"探测广度"的全部限额集中到一处开关。

解决的问题
----------
"想多探一点"此前要分别改三处、且容易漏：
- 超长用例上限（``case_generator.BIG_CAP``）
- 安全载荷每字段每类条数（``assets/security_payloads.json`` 的 limits）
- 字段族配额（``field_semantics`` 里 ``pick_by_family`` 的 6 / 5 / 2）
- AF 业务场景条数上限（``business_scenarios.af_max_cases()``）
限额散落在生成链的不同阶段，没有单一入口，放宽时既费事又容易只改一半。
本模块把它们收成一个 profile：``--profile discovery`` / ``WJX_PROFILE=discovery``
一处生效，全链放宽。

三条硬边界（与本 skill 一贯的"反假缺陷"纪律一致）
--------------------------------------------------
1. **只放宽探测广度，不改断言口径**——放宽后用例的预期方向不变，否则假阳性暴涨；
2. **默认档的限额与接入前完全一致**——``conservative`` 各项数值即历史默认值，
   不传 profile 时探测广度零变化（增量接入；生成逻辑自身的修复不在此承诺范围内）；
3. **放宽产生的用例不享受任何豁免**——照常进分层统计、照常计入覆盖率门禁、
   照常可能被判缺陷。"多探"是增加探测面，不是放松判定标准。

与其它模块的一致性由单测守住：``DEFAULTS["big_cap"]`` 必须等于
``case_generator.BIG_CAP`` 与 ``check_key._MAX_LEN_CAP``。
"""

import os

# 默认档（conservative）的限额。任何一项都必须等于"接入前"的历史默认值，
# 否则"不传 profile 时行为不变"这条增量承诺就不成立。
DEFAULTS = {
    # 生成器对 max_len 不超过该上限的字段构造「超长 + 恰好边界」两条用例。
    # 超长 payload 会拖慢请求、被网关拒收，故设上限（实测 html_content max_len=200000）。
    "big_cap": 5000,
    # 安全载荷每字段每类取几条。None → 沿用 assets/security_payloads.json 自带 limits（默认 2）。
    "payload_per_field_per_category": None,
    # 空值四态（B-NULL）最多覆盖多少个字段（按字段族保代表）。
    "null_state_fields": 6,
    # 响应 URL 字段（S4）最多覆盖多少个。
    "url_fields": 5,
    # UI 代理字段（UI1）最多覆盖多少个。
    "ui_proxy_fields": 2,
    # AF 业务场景条数上限（business_scenarios）。
    "af_max_cases": 8,
    # 对大字段（max_len > big_cap）追加一条「大内容健壮性」探针：
    # 用有界的大体积合法内容探一次，看后端是否吞得下（不是边界用例）。
    "overlong_probe": False,
}

# 各档位相对 DEFAULTS 的覆盖项。conservative 空覆盖 = 完全等于 DEFAULTS。
PROFILES = {
    "conservative": {},
    "discovery": {
        # 提高超长上限：让 max_len 在数万级的字段也能构造真正的边界用例
        "big_cap": 20000,
        # 安全载荷每字段每类取 4 条（含"字面 + 变体绕过"两种样本）
        "payload_per_field_per_category": 4,
        # 字段族配额整体放大：更多字段进入边界/URL/UI 代理探测
        "null_state_fields": 12,
        "url_fields": 10,
        "ui_proxy_fields": 4,
        # 业务场景条数放大（写接口会落数据，仍受写操作门禁约束）
        "af_max_cases": 20,
        # 大字段追加一条有界的大内容健壮性探针
        "overlong_probe": True,
    },
}

_ACTIVE = None  # None → conservative（默认档）


def normalize(name):
    """把任意名字规整到已注册的档位名（未知 → conservative，不报错）。"""
    return name if name in PROFILES else "conservative"


def activate(name):
    """激活档位并返回生效名。传空/未知 → conservative。"""
    global _ACTIVE
    _ACTIVE = normalize(name) if name else None
    return current()


def current():
    return _ACTIVE or "conservative"


def env_profile():
    """从环境变量读档位名（CLI 未显式指定时的回退来源）。"""
    return os.environ.get("WJX_PROFILE") or ""


def limits():
    """当前生效的完整限额（DEFAULTS 叠加档位覆盖项）。"""
    out = dict(DEFAULTS)
    out.update(PROFILES.get(current(), {}))
    return out


def get(key, default=None):
    """取某一项限额；key 不存在时回退给定的 default。"""
    lim = limits()
    if key in lim:
        return lim[key]
    return DEFAULTS.get(key, default)


def is_discovery():
    return current() == "discovery"
