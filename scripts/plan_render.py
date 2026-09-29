# -*- coding: utf-8 -*-
"""把智能计划渲染成人可读的 Markdown，供执行前审阅。

为什么需要：`plan_builder` 产出的 `intelligent_test_plan.json` 是给程序看的，
人读不了；而"待确认问题"的素材（阻塞项、低置信度字段、无法定位的依赖、
选测零产出原因）其实全都现成，只是没有汇总成一份可以扫一眼的文档。

对使用场景的价值：每一轮测试都要付执行成本（写接口会落数据）。跑完 170 条
才发现"这个场景设计得不对"代价很高；执行前花几十秒看一页计划，返工便宜得多。

四部分固定结构：
  1. 用例总览     —— 本次要跑多少条、业务/探测/人工怎么分
  2. 待确认问题   —— 需要人拍板或留痕的项，本轮核心价值
  3. 前置依赖与关系 —— 本接口依赖谁、被谁依赖、哪些关系定不位
  4. 风险与不适用凭据 —— 风险评分拆分、以及"为什么这个维度不适用"

只做渲染，不做判断：所有结论都来自 plan / doc，本模块不新增事实。

用法：
  python plan_render.py --plan intelligent_test_plan.json [--cases generated_cases.json] --out plan.md
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def _s(value, default="-"):
    if value is None or value == "":
        return default
    return str(value)


def _table(headers, rows):
    if not rows:
        return []
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        out.append("| " + " | ".join(_s(c) for c in row) + " |")
    return out


def collect_pending(plan, doc=None):
    """汇总需要人拍板或留痕的项。返回 [{type, item, reason, action}]。"""
    plan = plan or {}
    doc = doc or {}
    pending = []

    for item in plan.get("blocked") or []:
        if isinstance(item, dict):
            pending.append({
                "type": "阻塞",
                "item": item.get("dimension") or "-",
                "reason": item.get("reason") or "",
                "action": item.get("action") or "",
            })

    for item in plan.get("inferred_items") or []:
        if isinstance(item, dict):
            pending.append({
                "type": "低置信度字段",
                "item": item.get("field") or "-",
                "reason": "契约证据置信度 %.2f" % float(item.get("confidence") or 0.0),
                "action": item.get("action") or "仅探索性验证，不作硬断言",
            })

    for item in plan.get("dependency_unknowns") or []:
        if isinstance(item, dict):
            pending.append({
                "type": "依赖无法定位",
                "item": _clip(item.get("evidence"), 40),
                "reason": item.get("reason") or "",
                "action": "人工确认前置接口的 action 后再补",
            })

    for item in doc.get("uncovered_optional_detail") or []:
        if isinstance(item, dict):
            pending.append({
                "type": "选测零产出",
                "item": item.get("dimension") or "-",
                "reason": item.get("reason_text") or item.get("reason") or "",
                "action": "按原因补契约、补观测通道或补实现",
            })

    for item in plan.get("llm_unknowns") or []:
        if isinstance(item, dict):
            pending.append({
                "type": "契约未说明",
                "item": _clip(item.get("claim") or item.get("text") or item.get("evidence"), 40),
                "reason": "契约未说明，不得断言",
                "action": "人工确认或保持留痕",
            })

    for item in doc.get("payload_skipped") or []:
        if isinstance(item, dict):
            pending.append({
                "type": "安全载荷被排除",
                "item": item.get("field") or item.get("dimension") or "-",
                "reason": item.get("reason") or "超出每字段载荷上限",
                "action": "审查该排除是否合理",
            })
    return pending


def _clip(text, limit):
    text = _s(text, "")
    return text if len(text) <= limit else text[:limit] + "…"


def render_plan_md(plan, doc=None, contract_path="", generated_at=None, title=None):
    """渲染人可读测试计划。plan/doc 缺字段时按缺省渲染，不抛错。"""
    plan = plan or {}
    doc = doc or {}
    contract = plan.get("contract") or {}
    cases = doc.get("cases") or []
    tier = doc.get("tier_stats") or plan.get("tier_stats") or {}
    risk = plan.get("risk") or {}

    action = _s(doc.get("action") or plan.get("operation") or contract.get("operation"))
    name = _s(doc.get("name") or contract.get("name"), "(未命名接口)")
    lines = ["# 测试计划：%s（action=%s）" % (title or name, action), ""]

    meta = [
        "| 项 | 值 |", "|---|---|",
        "| 接口地址 | %s |" % _s(doc.get("endpoint") or contract.get("endpoint")),
        "| 操作类型 | %s |" % _s(doc.get("op_type")),
        "| 国际版 | %s |" % ("是" if doc.get("is_intl") else "否"),
        "| 契约来源 | %s |" % _s(contract_path or contract.get("source") or doc.get("source")),
        "| 风险 | %s（%s 分） |" % (_s(risk.get("level")), _s(risk.get("score"))),
        "| 用例总数 | %d |" % len(cases),
    ]
    if generated_at:
        meta.append("| 生成时间 | %s |" % generated_at)
    lines += meta + [""]

    lines += ["## 1. 用例总览", ""]
    lines += _table(["分层", "条数", "含义"], [
        ["business", tier.get("business", 0), "业务驱动，验证真实业务规则"],
        ["probe", tier.get("probe", 0), "机械探测，字段变异与安全载荷"],
        ["manual", tier.get("manual", 0), "人工项，需人工确认后执行"],
    ])
    lines += [""]
    required = plan.get("required") or doc.get("coverage_plan") or []
    optional = plan.get("optional") or doc.get("optional_plan") or []
    lines.append("- 必测维度（%d）：%s" % (len(required), "、".join(required) or "无"))
    lines.append("- 选测维度（%d）：%s" % (len(optional), "、".join(optional) or "无"))
    by_dim = {}
    for case in cases:
        key = case.get("dimension") or "-"
        by_dim[key] = by_dim.get(key, 0) + 1
    if by_dim:
        lines.append("- 用例维度分布：%s" % "、".join(
            "%s=%d" % (k, by_dim[k]) for k in sorted(by_dim)))
    patterns = doc.get("matched_patterns") or []
    if patterns:
        lines.append("- 命中缺陷模式：%s" % "、".join(
            "%s(%s)" % (p.get("id"), p.get("cases")) for p in patterns))
    lines.append("")

    lines += ["## 2. 待确认问题", ""]
    pending = collect_pending(plan, doc)
    if pending:
        lines.append("共 **%d** 项。这些项在执行前需要人或拍板或留痕，不处理会导致"
                     "断言方向不明或覆盖缺口被静默带过。" % len(pending))
        lines.append("")
        lines += _table(["类型", "对象", "原因", "建议动作"],
                        [[p["type"], p["item"], p["reason"], p["action"]] for p in pending])
    else:
        lines.append("无。本轮没有阻塞项、低置信度字段或定不位的依赖。")
    lines.append("")

    lines += ["## 3. 前置依赖与关系", ""]
    deps = plan.get("dependencies") or []
    if deps:
        lines.append("以下依赖来自契约正文的**显式声明**，不是推断：")
        lines.append("")
        lines += _table(["类型", "目标接口", "接口名", "来源", "原文证据"], [
            [d.get("kind"), d.get("target_action"), d.get("target_name") or "-",
             "第 %s 行" % d.get("line") if d.get("line") else "契约声明",
             _clip(d.get("evidence"), 46)]
            for d in deps if isinstance(d, dict)
        ])
        lines.append("")
    else:
        lines.append("契约未显式声明跨接口依赖。**这不等于本接口没有前置条件**——"
                     "只是文档没写，因此不做任何推测，也不生成依赖用例。")
        lines.append("")

    companions = []
    if doc.get("read_action"):
        companions.append(["回读接口", doc["read_action"], "写入后真值回读"])
    if doc.get("list_action"):
        companions.append(["列表接口", doc["list_action"], "创建后验列表出现与计数"])
    if doc.get("companion"):
        comp = doc["companion"]
        if isinstance(comp, dict):
            for cand in comp.get("candidates") or []:
                if isinstance(cand, dict):
                    companions.append(["候选配套接口", _s(cand.get("action")),
                                       "置信度 %s，仅审计不自动调" % _s(cand.get("confidence"))])
    if companions:
        lines.append("配套接口（用于交叉验证）：")
        lines.append("")
        lines += _table(["用途", "接口", "说明"], companions)
        lines.append("")

    relations = plan.get("relations") or []
    inferred = [r for r in relations if isinstance(r, dict)
                and r.get("relation_type") not in ("depends_on", "companion_read")]
    lines.append("本接口自身操作类型（关键词推断，非依赖关系，共 %d 条）：" % len(inferred))
    if inferred:
        lines.append("")
        lines += _table(["关系类型", "对象", "目标操作", "置信度", "依据"], [
            [r.get("relation_type"), r.get("resource"), _s(r.get("target_operation")),
             "%.2f" % float(r.get("confidence") or 0.0), _clip(r.get("reason"), 40)]
            for r in inferred
        ])
    else:
        lines.append("")
        lines.append("无。")
    lines.append("")

    lines += ["## 4. 风险与不适用凭据", ""]
    breakdown = risk.get("breakdown") or {}
    if breakdown:
        lines += _table(["风险来源", "分值"], [[k, v] for k, v in breakdown.items()])
        lines.append("")
    else:
        lines.append("- 风险评分：%s（%s 分），无拆分项。" % (_s(risk.get("level")), _s(risk.get("score"))))
        lines.append("")

    na = plan.get("na_proofs") or doc.get("na_proofs") or []
    if na:
        lines.append("以下维度判定为**不适用**，附判定凭据：")
        lines.append("")
        lines += _table(["维度", "不适用原因"], [
            [item.get("dim"), _clip(item.get("reason"), 60)]
            for item in na if isinstance(item, dict)
        ])
    else:
        lines.append("无不适用维度。")
    lines.append("")

    lines += ["---", "",
              "本计划由 `plan_builder` 与 `case_generator` 的产物渲染，不含额外推断。",
              "用例明细见 `generated_cases.json`；机器可读计划见 `intelligent_test_plan.json`。"]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description="把智能计划渲染为人可读 Markdown")
    ap.add_argument("--plan", required=True, help="intelligent_test_plan.json 路径")
    ap.add_argument("--cases", help="generated_cases.json 路径（可选，用于用例总览）")
    ap.add_argument("--contract-path", default="", help="契约路径（仅用于展示）")
    ap.add_argument("--out", help="Markdown 落盘路径；省略则打印到 stdout")
    args = ap.parse_args()

    with open(args.plan, "r", encoding="utf-8") as f:
        plan = json.load(f)
    doc = {}
    if args.cases and os.path.isfile(args.cases):
        with open(args.cases, "r", encoding="utf-8") as f:
            doc = json.load(f)

    contract_path = args.contract_path or (plan.get("contract") or {}).get("source") or ""
    md = render_plan_md(plan, doc=doc, contract_path=contract_path)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(md)
        pending = collect_pending(plan, doc)
        print("待确认问题 %d 项；计划 -> %s" % (len(pending), args.out))
    else:
        sys.stdout.write(md)


if __name__ == "__main__":
    main()
