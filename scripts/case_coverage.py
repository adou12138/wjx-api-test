#!/usr/bin/env python3
"""用例执行率闭环：计划 vs 实际（wjx-api-test v4.36.0）。

为什么需要（两个真实缺口 + 一项产物化增量，读实现核验后确认）：
  ① **AF 业务场景整类缺席无声**：`flow.py` 主链路未接入 `business_scenarios`，
     AF 要人工单独跑再 --merge。`coverage_plan` 是维度驱动、AF1 不是声明维度
     ⇒ 唯一能测业务语义的一类用例可能 0 条，报告却如常。
  ② **没有"计划 vs 实际"的逐条对账**：无权威计划清单落盘；`--only` 直接改写 cases 不留声明。

  产物化增量：`coverage_gate()` **已经**按 EXECUTED_STATUSES 过滤（只有 PASS/FAIL/PRECONDITION_UNMET 算触及），
  并把"全部被跳过"的维度记成「假覆盖」缺陷——判定本身是对的。本模块把它从 **stdout 提示**
  升级为**可落盘 / 可进报告 / 可 --enforce 的产物**，并给到用例级明细。

本模块**只加对账层，不改** case_runner / coverage_gate / 任何生成器：
  - `executed.total/rate` 是**严格口径**（只有 PASS/FAIL 算真判定）；
  - `executed.looseTotal` 是**宽松口径**（任一 result 即触及，含 SKIPPED/N/A；比 coverage_gate 更宽——门禁只认 EXECUTED_STATUSES，SKIPPED-only 维度被标「假覆盖」不算触及）；
  - 两者差异通过 `skippedOnlyDimensions` 落进产物（既有门禁只 stdout 打印），供人决策。

用法：
  python case_coverage.py --plan cases.json --results <prefix>_report_full.json \
      [--out case_coverage.json] [--expect-categories AF,业务场景] \
      [--exempt-categories "AF:本轮未启用大模型链路"] [--min-rate 0.8] [--enforce]

门禁（--enforce）：
  ① 未执行（SKIPPED/N-A）却没写 reason → 失败（沿用"原因必填"纪律）；
  ② 严格执行率低于 --min-rate → 失败；
  ③ 声明的类别既没覆盖也没显式豁免 → 失败。
  计划外 id（results 里出现 plan 之外的）只告警不阻塞。

状态词表（与 case_runner 写入 results 的字面量一致）：
  PASS / FAIL  → 已执行（真判定）
  SKIPPED / N/A → 未执行（跑了但没判定），reason 必填
只读本地文件，不与网络交互。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

EXECUTED = {"PASS", "FAIL"}
NOT_EXECUTED = {"SKIPPED", "N/A"}
KNOWN = EXECUTED | NOT_EXECUTED


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print("无法读取/解析 {}：{}".format(path, e), file=sys.stderr)
        return None


def plan_cases(plan) -> list:
    cases = plan.get("cases") if isinstance(plan, dict) else None
    return [c for c in (cases or []) if isinstance(c, dict) and c.get("id")]


def results_of(doc) -> list:
    if isinstance(doc, list):
        return [r for r in doc if isinstance(r, dict)]
    if isinstance(doc, dict):
        for key in ("results", "cases", "executions"):
            if isinstance(doc.get(key), list):
                return [r for r in doc[key] if isinstance(r, dict)]
    return []


def _norm(v) -> str:
    return str(v or "").strip()


def parse_exempt(items) -> list:
    """`key:reason` → [{"name","reason"}]；缺 reason 的也收下（由 unexplained 逻辑点名）。"""
    out = []
    for raw in (items or []):
        text = str(raw)
        if ":" in text or "：" in text:
            name, _, reason = text.replace("：", ":").partition(":")
            out.append({"name": name.strip(), "reason": reason.strip()})
        else:
            out.append({"name": text.strip(), "reason": ""})
    return out


def _dim_match(case_dim, case_cat, key) -> bool:
    k = _norm(key)
    if not k:
        return False
    d, c = _norm(case_dim), _norm(case_cat)
    if d == k or (d and d.startswith(k)):
        return True
    return bool(c) and (c == k or c.startswith(k))


def compute(plan, results, expect_categories, exempt_categories) -> dict:
    cases = plan_cases(plan)
    by_id = {c["id"]: c for c in cases}
    # 同一用例可能有多条记录：results 是纯 append 列表（case_runner 每次执行都 append，
    # 无覆盖机制），重跑/回归并进就会出现重复。此时**真判定优先**——只要有一条
    # PASS/FAIL 就说明这条用例真跑过，不能因为首条是 SKIPPED 就判它未执行
    # （同批 detect_flaky 也是按多条记录判不稳，两处口径必须一致）。
    res_by_id, multi = {}, {}
    for r in results:
        rid = _norm(r.get("case_no") or r.get("id"))
        if not rid:
            continue
        multi.setdefault(rid, []).append(r)
        prev = res_by_id.get(rid)
        if prev is None:
            res_by_id[rid] = r
        elif _norm(prev.get("status")).upper() not in EXECUTED and \
                _norm(r.get("status")).upper() in EXECUTED:
            res_by_id[rid] = r          # 后到的真判定覆盖先前的跳过
    multiple = sorted(cid for cid, rs in multi.items() if len(rs) > 1)
    flaky = sorted(cid for cid in multiple
                   if len({_norm(r.get("status")).upper() for r in multi[cid]}) > 1)

    executed, not_executed, unexplained = 0, 0, []
    exec_by_dim, skip_by_dim = {}, {}
    executed_ids, skipped_ids = set(), set()

    for cid, case in by_id.items():
        dim = _norm(case.get("dimension")) or "(未标维度)"
        rec = res_by_id.get(cid)
        if rec is None:
            # 计划里有、results 里没有 = 静默丢失（当前 runner 每条都留痕，应为空；
            # 这条断言的价值在于将来一旦有路径绕过留痕，立刻暴露）
            not_executed += 1
            skip_by_dim[dim] = skip_by_dim.get(dim, 0) + 1
            unexplained.append("{} 计划内但 results 无任何执行记录（静默丢失）".format(cid))
            continue
        status = _norm(rec.get("status")).upper()
        if status in EXECUTED:
            executed += 1
            executed_ids.add(cid)
            exec_by_dim[dim] = exec_by_dim.get(dim, 0) + 1
        elif status in NOT_EXECUTED:
            not_executed += 1
            skipped_ids.add(cid)
            skip_by_dim[dim] = skip_by_dim.get(dim, 0) + 1
            if not _norm(rec.get("reason")):
                unexplained.append("{} 标记 {} 但未写明原因".format(cid, status))
        else:
            not_executed += 1
            skipped_ids.add(cid)
            skip_by_dim[dim] = skip_by_dim.get(dim, 0) + 1
            unexplained.append("{} 状态值非法：{}".format(cid, status or "(空)"))

    total = len(by_id)
    rate = round(executed / total, 4) if total else 0.0

    # 宽松口径：比 coverage_gate 更宽——任一 result 即计（含 SKIPPED/N/A），只算计划内用例。
    # coverage_gate 只认 EXECUTED_STATUSES（PASS/FAIL/PRECONDITION_UNMET），SKIPPED-only 维度被标「假覆盖」不算触及。
    loose_dims = {_norm(by_id[cid].get("dimension")) or "(未标维度)"
                  for cid in res_by_id if cid in by_id}
    loose_total = sum(1 for cid in res_by_id if cid in by_id)

    # 只靠 SKIPPED 撑着的维度：既有门禁会算它"已触及"，严格口径下它一条真判定都没有
    skipped_only = sorted(dim for dim in skip_by_dim
                          if dim not in exec_by_dim and dim in loose_dims)

    # 类别对账：至少一条真实执行（PASS/FAIL）才算覆盖——AF 用例全 SKIPPED 不算覆盖
    declared = [_norm(x) for x in (expect_categories or []) if _norm(x)]
    exempt = parse_exempt(exempt_categories)
    exempt_names = {e["name"] for e in exempt}
    covered, missing = [], []
    for key in declared:
        if key in exempt_names:
            continue
        hit = any(_dim_match(c.get("dimension"), c.get("category"), key) and cid in executed_ids
                  for cid, c in by_id.items())
        (covered if hit else missing).append(key)

    unknown = sorted(cid for cid in res_by_id if cid not in by_id)
    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "planned": {
            "total": total,
            "byDimension": _count(cases, "dimension"),
            "byCategory": _count(cases, "category"),
            "coveragePlan": (plan.get("coverage_plan") if isinstance(plan, dict) else None) or [],
        },
        "executed": {
            "total": executed,
            "rate": rate,
            "looseTotal": loose_total,
            "byDimension": dict(sorted(exec_by_dim.items())),
            "caseIds": sorted(executed_ids),
        },
        "notExecuted": {
            "total": not_executed,
            "byDimension": dict(sorted(skip_by_dim.items())),
            "unexplained": unexplained,
        },
        "multipleRecords": {
            "caseIds": multiple,
            "flakyCaseIds": flaky,
            "note": "同一用例在 results 里出现多条记录（重跑/回归并进）。本模块按"
                    "「真判定优先」取状态；终态不一致的即不稳定，与 detect_flaky 同口径。",
        },
        "silentLost": sorted(cid for cid in by_id if cid not in res_by_id),
        "skippedOnlyDimensions": skipped_only,
        "categories": {
            "declared": declared,
            "covered": covered,
            "missing": missing,
            "exempt": exempt,
        },
        "unknownResults": unknown,
        "note": "严格口径：只有 PASS/FAIL 算已执行（coverage_gate 还多认 PRECONDITION_UNMET）；"
                "looseTotal 是比 coverage_gate 更宽的宽松口径（任一 result 即触及，含 SKIPPED/N/A）。"
                "差异集中在 skippedOnlyDimensions——这些维度在既有门禁里被标为「假覆盖」（不算触及），"
                "但真判定为零，请人工决定是否收紧。本模块不修改 case_runner / coverage_gate，既有门禁结论不受影响。",
    }


def _count(items, key) -> dict:
    out = {}
    for c in items:
        k = _norm(c.get(key)) or "(空)"
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))


def main() -> int:
    ap = argparse.ArgumentParser(description="用例执行率对账：计划 vs 实际（wjx-api-test）")
    ap.add_argument("--plan", required=True, type=Path, help="生成的用例文件 cases.json（case_generator --out 产物）")
    ap.add_argument("--results", type=Path, help="report_full.json（含 results 数组的执行产物）")
    ap.add_argument("--out", type=Path, help="对账结果 JSON 输出")
    ap.add_argument("--expect-categories", default="", help="本轮应覆盖的用例类别，逗号分隔（如 AF,业务场景）")
    ap.add_argument("--exempt-categories", default="", help='显式豁免的类别，格式 "key:理由"，逗号分隔')
    ap.add_argument("--min-rate", type=float, default=None, help="严格执行率下限，低于则 --enforce 失败（默认不设限）")
    ap.add_argument("--enforce", action="store_true", help="门禁模式")
    args = ap.parse_args()

    plan = load_json(args.plan)
    if plan is None:
        return 2
    results = []
    if args.results:
        doc = load_json(args.results)
        if doc is None:
            return 2
        results = results_of(doc)

    expect = [x.strip() for x in str(args.expect_categories or "").split(",") if x.strip()]
    exempt = [x.strip() for x in str(args.exempt_categories or "").split(",") if x.strip()]
    cov = compute(plan, results, expect, exempt)
    cov["output"] = str(args.out) if args.out else None

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(cov, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "planned": cov["planned"]["total"],
        "executed": cov["executed"]["total"],
        "rate": cov["executed"]["rate"],
        "looseTotal": cov["executed"]["looseTotal"],
        "notExecuted": cov["notExecuted"]["total"],
        "silentLost": cov["silentLost"],
        "skippedOnlyDimensions": cov["skippedOnlyDimensions"],
        "categories": cov["categories"],
        "unexplained": cov["notExecuted"]["unexplained"][:5],
        "output": cov["output"],
    }, ensure_ascii=False, indent=2))

    if not args.enforce:
        return 0
    problems = []
    if cov["notExecuted"]["unexplained"]:
        problems.append("存在未写明原因/未留痕的未执行项 {} 条（如 {}）".format(
            len(cov["notExecuted"]["unexplained"]), "、".join(cov["notExecuted"]["unexplained"][:5])))
    if args.min_rate is not None and cov["executed"]["rate"] < args.min_rate:
        problems.append("严格执行率 {:.0%} 低于下限 {:.0%}（宽松口径 {}/{}）；"
                        "只靠 SKIPPED 撑着的维度：{}".format(
                            cov["executed"]["rate"], args.min_rate,
                            cov["executed"]["looseTotal"], cov["planned"]["total"],
                            "、".join(cov["skippedOnlyDimensions"]) or "无"))
    if cov["categories"]["missing"]:
        problems.append("声明应覆盖但未覆盖的类别：{}（如确属本轮不适用，用 --exempt-categories "
                        '"key:理由" 显式豁免）'.format("、".join(cov["categories"]["missing"])))
    if problems:
        print("错误：" + "；".join(problems), file=sys.stderr)
        return 1
    if cov["unknownResults"]:
        print("警告：results 里出现计划外的用例 ID {} 个（可能来自旧用例文件），不阻塞门禁".format(
            len(cov["unknownResults"])), file=sys.stderr)
    if cov["skippedOnlyDimensions"]:
        print("提醒：维度 {} 在既有门禁里被标为「假覆盖」（不算触及）但真判定为零，请人工确认是否需要收紧"
              .format("、".join(cov["skippedOnlyDimensions"])), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())