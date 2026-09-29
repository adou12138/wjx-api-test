# -*- coding: utf-8 -*-
"""问卷星开放接口冒烟 —— 业务适配层（参考实例，可整份替换）

本文件承载「无法用纯数据表达」的少量业务动作：环境准备、资源登记、收尾清理。
接口参数全部声明在 assets/smoke_cases_wjx.json 中，本文件不重复定义。

引擎约定四个入口，缺一不可：
  bootstrap(cfg, ctx, call)              跑用例前准备环境（取/建基准问卷，铺上下文）
  build_params(action, cfg, ctx, call)   返回 None 表示「用案例里的声明式参数」
  post_process(action, cfg, ctx, parsed) 调用后回调（登记待清理资源等）
  teardown(cfg, ctx, call)               收尾清理（默认不删数据，见下方授权规则）

清理的安全约定：删除是不可逆操作。teardown 仅在配置 teardown_enabled=true 时
才真正删除；否则只打印「待清理清单」供人工确认。开启前请确认目标为测试环境，
且清单中的 vid 均为本次运行创建（ctx.created_vids）。
"""
from __future__ import annotations

import json
import time


def _first_survey(parsed) -> tuple[int | None, str]:
    """从问卷列表响应里取第一条问卷的 vid / sid。"""
    if not isinstance(parsed, dict) or not parsed.get("result"):
        return None, ""
    data = parsed.get("data") or {}
    acts = data.get("activitys") or {}
    if isinstance(acts, dict) and acts:
        first = next(iter(acts.values()))
        if isinstance(first, dict):
            return int(first.get("vid")), str(first.get("sid") or "")
    return None, ""


def bootstrap(cfg, ctx, call) -> None:
    print("\n=== Bootstrap ===")
    main_user = (cfg.get("account") or {}).get("main")

    code, parsed, raw = call("1000002", {"creater": main_user, "page_index": 1, "page_size": 5})
    vid, sid = _first_survey(parsed)
    if vid:
        print(f"  复用已有问卷 vid={vid}")
    else:
        title = f"TEST_AUTO_SMOKE_{ctx.run_id}"
        code, parsed, raw = call("1000101", {
            "creater": main_user, "atype": 1, "title": title,
            "desc": "openapi smoke bootstrap", "publish": True,
            "questions": ctx.overrides.get("minimal_questions"),
        })
        if not isinstance(parsed, dict) or not parsed.get("result"):
            raise RuntimeError(f"Bootstrap 失败: {str(raw)[:300]}")
        data = parsed.get("data") or {}
        vid = int(data.get("vid"))
        sid = str(data.get("sid") or "")
        ctx.created_vids.append(vid)
        print(f"  创建 bootstrap 问卷 vid={vid}")

    ctx.smoke_vid = vid
    ctx.smoke_sid = sid
    # 业务 ID 优先取环境专项覆盖值，缺失时回落到基准问卷，保证依赖接口有可用入参
    ctx.settings_vid = ctx.overrides.get("settings_vid") or vid
    ctx.usersystem_vid = ctx.overrides.get("usersystem_vid") or ctx.settings_vid
    ctx.answer_vid = ctx.overrides.get("response_vid") or vid


def build_params(action, cfg, ctx, call):
    """参数一律由案例数据声明，这里不做业务判断。返回 None 引擎即取 case.params。"""
    return None


def post_process(action, cfg, ctx, parsed) -> None:
    if not isinstance(parsed, dict) or not parsed.get("result"):
        return
    data = parsed.get("data") or {}
    # 登记本次新建的资源，供 teardown 清理
    if action in ("1000101", "1000107") and isinstance(data, dict) and data.get("vid"):
        vid = int(data["vid"])
        if vid not in ctx.created_vids:
            ctx.created_vids.append(vid)


def teardown(cfg, ctx, call) -> None:
    print("\n=== Teardown ===")
    if not ctx.created_vids:
        print("  本次运行未创建资源，无需清理")
        return
    print(f"  本次创建资源：{ctx.created_vids}")

    # AI 主页先下线再删除，避免状态不允许删除
    if getattr(ctx, "ai_vid", None):
        call("1000102", {"vid": ctx.ai_vid, "state": 2})
        time.sleep(0.3)

    if not cfg.get("teardown_enabled"):
        print("  teardown_enabled=false，仅列出待清理清单，未执行删除。")
        print("  确认目标为测试环境后，在配置里置为 true 即会自动清理。")
        return

    main_user = (cfg.get("account") or {}).get("main")
    for vid in ctx.created_vids:
        try:
            _, parsed, raw = call("1000301", {"vid": vid, "username": main_user})
            ok = isinstance(parsed, dict) and parsed.get("result")
            print(f"  删除 vid={vid} -> {'成功' if ok else '跳过/失败: ' + str(raw)[:80]}")
        except Exception as e:  # 清理失败不应中断整体流程
            print(f"  删除 vid={vid} 异常: {e}")
        time.sleep(0.3)
