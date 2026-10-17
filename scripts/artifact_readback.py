# -*- coding: utf-8 -*-
"""产物回读校验：交付前把刚写出去的台账读回来，验它是不是我们以为的那张表。

为什么必须有这个（背景）：
    陷阱 7 规定台账 CSV 必须 utf-8-sig + 逗号分隔 + 含逗号/换行/引号的字段用引号包裹，
    但那一直只是**文档里的一句约定**——写盘代码没有任何机制保证自己做到了。
    BOM 少一个、某列忘了引号包裹、列定义改了而渲染层没跟上，Excel 里就是整张表错位，
    而且**完全静默**：脚本 exit 0，报告"看起来完整"，错位在开发打开文件之后才暴露。

    这个脚本把约定变成可执行检查：读回自己写的产物，验编码 BOM、表头与列定义一致
    （含顺序）、每行列数、编号非空且唯一、台账行数 vs **同前缀** report_full.json
    的findings 数、以及采集类证据列（请求ID/TraceID/证据路径）是否真的有内容。
    脚本成功退出 ≠ 产物可交付——这正是本脚本存在的理由。

产出：
    退出码 0 = 全部通过；1 = 有 error 级问题（产物不可交付）。
    warning 级问题只提示不拦（与覆盖率门禁同一套严格度约定：
    WJX_STRICT_READBACK=1 时 warning 也升级为 exit 1）。

用法：
    python artifact_readback.py --dir<产物目录>
    python artifact_readback.py --issues api_test_issues.csv --report api_test_report_full.json
    python artifact_readback.py --dir . --json out.json       # 机器可读，供门禁消费
    python artifact_readback.py --dir . --strict             # 临时收紧为严格模式
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 回读判定的严重度沿用 skill 既有三分法：
#   error   —— 结构错误 / 关键内容缺失，产物不能安全交付
#   warning —— 需要人工确认（证据链为空、无法确认是否截断）
#   info    —— 改进建议，不阻止交付
ERROR, WARNING, INFO = "error", "warning", "info"

# 证据链列分两类，因为它们空的含义完全不同：
#   TRACE_COLS —— 采集类（请求ID/TraceID/证据路径）。空 = 采集链没接上，
#                 开发无法复现，是硬缺口。
#   LEDGER_COLS —— 记录类（置信度/HTTP状态码/归因/复测状态）。由断言与归因环节填写，
#                 空值说明流程跳步，但也可能是简单缺陷没走到那一步。
# 混在一起判"整表无证据"会永远判不出来（记录类几乎总有值），必须拆开。
TRACE_COLS = ["请求ID", "TraceID", "证据路径"]
LEDGER_COLS = ["置信度", "归因", "复测状态"]
EVIDENCE_COLS = TRACE_COLS + LEDGER_COLS

_BOM = b"\xef\xbb\xbf"


def _issue(kind, code, msg, **extra):
    d = {"severity": kind, "code": code, "message": msg}
    d.update(extra)
    return d


def check_csv(path, expect_cols=None, expect_rows=None, id_col="编号"):
    """回读一个台账 CSV，返回问题列表。

    校验项：
      1. 文件存在且非空
      2. 有 UTF-8 BOM（utf-8-sig）——Excel 不认就乱码
      3. 能以 utf-8-sig 解析（编解码错误会在这里暴露）
      4. 表头与列定义一致（顺序 + 名称）——不一致意味着消费方按列位取值会整体错位
      5. 每行列数与表头一致——某行少列说明渲染层错位或含逗号字段没被引号包裹
      6. 编号非空且唯一——空编号的条目无法被复盘引用；重复编号会让复测指向错条目
      7. 行数与 findings 数一致（传expect_rows 时）
      8. 采集类证据列（请求ID/TraceID/证据路径）内容非空——列在但整列为空，
         同样是"开发拿不到复现线索"的硬缺口，只查列存在性会漏掉它
    """
    issues = []
    if not os.path.exists(path):
        return [_issue(ERROR, "MISSING", "台账文件不存在：%s" % path)]
    name = os.path.basename(path)

    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as e:
        return [_issue(ERROR, "UNREADABLE", "台账文件无法读取：%s" % e)]
    if not raw:
        return [_issue(ERROR, "EMPTY", "台账文件为空（0 字节）：%s" % path)]
    if not raw.startswith(_BOM):
        issues.append(_issue(ERROR, "NO_BOM",
                             "%s 缺少 UTF-8 BOM（应写 utf-8-sig），Excel 打开会中文乱码" % name))

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        return issues + [_issue(ERROR, "DECODE",
                               "%s 不是合法 UTF-8：%s" % (name, e))]

    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error as e:
        return issues + [_issue(ERROR, "CSV_PARSE", "%s 解析失败：%s" % (name, e))]

    if not rows:
        return issues + [_issue(ERROR, "NO_ROWS", "%s 没有表头行" % name)]

    header = rows[0]
    body = rows[1:]

    # 空行不算数据行，但也不该出现——出现即说明写入端有格式问题
    if any(not any(c.strip() for c in r) for r in body):
        issues.append(_issue(WARNING, "BLANK_ROW", "%s 存在全空行，疑似写入端格式问题" % name))

    if expect_cols is not None and list(header) != list(expect_cols):
        missing = [c for c in expect_cols if c not in header]
        extra = [c for c in header if c not in expect_cols]
        detail = []
        if missing:
            detail.append("缺失列 %s" % missing)
        if extra:
            detail.append("多出列 %s" % extra)
        if not detail:  # 名称齐全但顺序不同
            detail.append("顺序与列定义不一致（消费方按列位取值会整体错位）")
        issues.append(_issue(ERROR, "HEADER_MISMATCH",
                             "%s 表头与列定义不一致：%s" % (name, "；".join(detail)),
                             header=header))

    # 行宽校验：渲染层错位或含逗号字段未被引号包裹，都表现为某行少列
    bad_width = [(i + 2, len(r)) for i, r in enumerate(body) if len(r) != len(header)]
    for line_no, width in bad_width[:10]:
        issues.append(_issue(ERROR, "ROW_WIDTH",
                             "%s 第 %d 行有 %d 列，表头是 %d 列——列错位或含逗号/换行的字段"
                             "未被正确引号包裹" % (name, line_no, width, len(header))))

    # 编号完整性：空编号无法复盘引用，重复编号会让复测指向错条目
    if id_col in header:
        idx = header.index(id_col)
        seen, dup, blank = set(), [], []
        for i, r in enumerate(body):
            if idx >= len(r):
                continue
            val = (r[idx] or "").strip()
            if not val:
                blank.append(i + 2)
            elif val in seen:
                dup.append(val)
            else:
                seen.add(val)
        if blank:
            issues.append(_issue(ERROR, "BLANK_ID",
                                 "%s 有 %d 行编号为空（第 %s 行），无法被复盘引用"
                                 % (name, len(blank), blank[:10])))
        if dup:
            issues.append(_issue(ERROR, "DUP_ID",
                                 "%s 有重复编号 %s，复测会指向错条目" % (name, sorted(set(dup))[:10])))

    if expect_rows is not None and expect_rows != len(body):
        issues.append(_issue(ERROR, "ROW_COUNT",
                             "%s 有 %d 条数据行，findings 数为 %d——台账与报告不一致，"
                             "有 finding 静默丢失" % (name, len(body), expect_rows)))

    # 证据链列存在性：列缺失是 error；列在但内容空同样是缺口（消费方拿到的是一张
    # 没证据的表）——只查"列在不在"会漏掉后者，而这恰恰是最常见的形态：
    # 台账里常有几列压根没采集渠道（如请求ID/TraceID），整列为空。
    #
    # 分级：区分「所有条目都没证据」与「部分条目没证据」——
    # 前者是采集链整体没接上，后者可能只是个别条目本来就没采到。
    # 前者 error（台账无法支撑复现），后者 warning（人工核对）。
    if expect_cols is not None:
        absent_ev = [c for c in EVIDENCE_COLS if c not in header]
        if absent_ev:
            issues.append(_issue(ERROR, "EV_COL_MISSING",
                                 "%s 缺证据链列 %s——台账无法支撑开发复现" % (name, absent_ev)))
        elif body:
            # 核心判定看 TRACE_COLS（采集类）：它空了才是"开发无法复现"的硬缺口。
            # LEDGER_COLS 空只说明流程跳步，单独记 warning。
            rows_no_trace = 0
            empty_trace_cols, empty_ledger_cols = set(), set()
            for r in body:
                tv = [(r[header.index(c)] or "").strip() for c in TRACE_COLS
                      if header.index(c) < len(r)]
                lv = [(r[header.index(c)] or "").strip() for c in LEDGER_COLS
                      if header.index(c) < len(r)]
                if tv and not any(tv):
                    rows_no_trace += 1
                for c, v in zip(TRACE_COLS, tv):
                    if not v:
                        empty_trace_cols.add(c)
                for c, v in zip(LEDGER_COLS, lv):
                    if not v:
                        empty_ledger_cols.add(c)
            if rows_no_trace == len(body):
                issues.append(_issue(ERROR, "EV_ALL_EMPTY",
                                     "%s 全部 %d 条记录的采集类证据列（%s）均为空——"
                                     "开发无法复现，请确认请求ID/TraceID/证据路径的采集链是否接上"
                                     % (name, rows_no_trace, "、".join(TRACE_COLS))))
            elif rows_no_trace:
                issues.append(_issue(WARNING, "EV_PART_EMPTY",
                                     "%s 有 %d/%d 条记录没有采集类证据（涉及列：%s），核对是否为漏采集"
                                     % (name, rows_no_trace, len(body), "、".join(sorted(empty_trace_cols)))))
            if empty_ledger_cols:
                issues.append(_issue(WARNING, "EV_LEDGER_EMPTY",
                                     "%s 的记录类证据列（%s）在部分行未填写，"
                                     "核对归因/置信度/复测状态是否走了完整流程"
                                     % (name, "、".join(sorted(empty_ledger_cols)))))

    return issues


def check_report_json(path, expect_findings=None):
    """回读 report_full.json：校验可解析、findings 数组存在且为列表。

    expect_findings 保留为签名兼容位但不参与判定——传入的值本就来自本文件，
    自比恒等没有意义。跨产物行数核对在 check_csv 里做。"""
    issues = []
    if not os.path.exists(path):
        return [_issue(WARNING, "MISSING", "报告文件不存在：%s（回读 CSV 时将无法交叉核对行数）" % path)]
    name = os.path.basename(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception as e:
        return [_issue(ERROR, "JSON_PARSE", "%s 解析失败：%s" % (name, e))]

    findings = doc.get("findings")
    if not isinstance(findings, list):
        return issues + [_issue(ERROR, "NO_FINDINGS_KEY",
                                "%s 缺 findings 数组或类型不对，无法与台账交叉核对" % name)]
    # 注意：这里**不**拿 findings 长度与"自己算出的同一个数"比较（恒等，死代码），
    # 真正有意义的跨产物核对在 check_csv：台账数据行数 vs 本文件findings 数。
    # 也不发"JSON 里有证据链、请确认 CSV 里有"这类 INFO——列是否真落进 CSV
    # 由 check_csv 的列存在性 + 空值检查直接判定，那处信息量更高。
    return issues


def _pick_report_for(dirpath, csv_path=None, explicit=None):
    """给台账 CSV 配对应的 report_full.json。

    配对规则：台账 `X_issues.csv` ↔ 报告 `X_report_full.json`（同前缀）。
    **不按 mtime 挑**——一次运行常留下多个前缀的产物（api_test_ / chain_ / noev_），
    按时间跨前缀配对会把不同运行的台账与报告凑成一对，比出根本不存在的
    "finding 静默丢失"。宁可返回空（降级为 warning），不做错配。
    """
    if explicit:
        return explicit
    if not dirpath or not os.path.isdir(dirpath):
        return ""
    if csv_path:
        base = os.path.basename(csv_path)
        # 报告应和台账在同一目录：以台账所在目录为准（CLI 只给 --issues 子路径、没给
        # --dir 时，dirpath 仍是 cwd，用 dirname(csv_path) 才能找对）。flow.py 集成时
        # dirpath 与 dirname(csv_path) 相同，行为不变。
        search_dir = os.path.dirname(csv_path) or dirpath
        prefix = base[:-len("_issues.csv")] if base.endswith("_issues.csv") else \
            os.path.splitext(base)[0]
        cand = os.path.join(search_dir, "%s_report_full.json" % prefix)
        if os.path.exists(cand):
            return cand
        # 同前缀找不到（如台账叫 issues.csv）：退回按前缀模糊找
        cands = [os.path.join(search_dir, f) for f in os.listdir(search_dir)
                 if f.endswith("_report_full.json") and prefix in f]
        if len(cands) == 1:
            return cands[0]
        return ""  # 0 个或多个候选 → 不猜，交由上层报 warning
    # 没给 CSV：取目录里最新的一个（此时只有一份台账，歧义风险低）
    cands = [os.path.join(dirpath, f) for f in os.listdir(dirpath)
             if f.endswith("_report_full.json")]
    if not cands:
        return ""
    return max(cands, key=os.path.getmtime)


def run_readback(dirpath=".", issues_csv=None, report_json=None,
                expect_cols=None, strict=False):
    """执行回读，返回 (issues, summary)。供 CLI 与门禁共用。"""
    if expect_cols is None:
        try:
            import api_test_runner as R
            expect_cols = list(R.FINDING_CSV_COLS)
        except Exception as e:  # noqa: BLE001
            print("⚠️ 列定义读取失败（%s: %s），仅校验文件可读性与行数，"
                  "不做表头比对" % (type(e).__name__, e))

    # 先定台账路径（没显式给就从目录里找），再用它配对应报告——顺序不能反，
    # 否则会在多前缀产物目录里配错对（见 _pick_report_for）。
    csv_path = issues_csv
    if not csv_path and os.path.isdir(dirpath):
        for fn in ("api_test_issues.csv",):
            p = os.path.join(dirpath, fn)
            if os.path.exists(p):
                csv_path = p
                break

    rep = _pick_report_for(dirpath, csv_path=csv_path, explicit=report_json)
    n_findings = None
    if rep and os.path.exists(rep):
        try:
            with open(rep, "r", encoding="utf-8") as f:
                n_findings = len(json.load(f).get("findings") or [])
        except Exception:
            n_findings = None  # JSON 层面的问题交给 check_report_json 报

    all_issues = []
    if rep:
        all_issues += check_report_json(rep)
    elif csv_path:
        all_issues.append(_issue(WARNING, "NO_PAIRED_REPORT",
                                 "未找到与台账同前缀（%s）的 *_report_full.json，"
                                 "无法交叉核对行数——finding 静默丢失这次查不出来"
                                 % os.path.basename(csv_path)))

    if csv_path:
        all_issues += check_csv(csv_path, expect_cols=expect_cols,
                                expect_rows=n_findings)

    # 【审查修复·P1-2】目录里既没找到台账也没找到报告时，旧版 all_issues 为空 →
    # _print_report 打印"✓ 产物可交付"且 rc=0——空目录/路径给错/产物没落盘被
    # 误判为交付就绪，回读门禁形同虚设。改为显式 error：没有可回读的产物
    # 就不是"通过"，必须让人看见。
    if not csv_path and not rep:
        all_issues.append(_issue(
            ERROR, "NO_ARTIFACTS",
            "目录 %s 里既没有 api_test_issues.csv 也没有 *_report_full.json——"
            "没有可回读的产物（确认产物目录/路径是否给对、产物是否已落盘）"
            % (dirpath or ".")))

    summary = {
        "checked_csv": bool(csv_path),
        "issues_csv": csv_path or "",
        "report_json": rep or "",
        "expect_cols": expect_cols or [],
        "findings_count": n_findings,
        "issues": all_issues,
        "n_error": sum(1 for i in all_issues if i["severity"] == ERROR),
        "n_warning": sum(1 for i in all_issues if i["severity"] == WARNING),
        "n_info": sum(1 for i in all_issues if i["severity"] == INFO),
    }
    return all_issues, summary


def _print_report(issues):
    """打印回读结果，返回是否应判失败（有 error 即失败）。

    strict 的判定不在这里做——warning 是否算失败取决于调用方的严格度设定，
    混进打印函数会让它既输出又决策，测试时说不清是谁决定的。
    """
    sev_order = {ERROR: 0, WARNING: 1, INFO: 2}
    icon = {ERROR: "✗", WARNING: "!", INFO: "i"}
    if not issues:
        print("✓ 产物回读通过：编码 / 表头 / 列宽 / 编号 / 行数均一致，产物可交付。")
        return 0
    for it in sorted(issues, key=lambda x: sev_order.get(x["severity"], 9)):
        print("%s [%s] %s: %s" % (icon.get(it["severity"], "?"), it["severity"].upper(),
                                  it["code"], it["message"]))
    n_err = sum(1 for i in issues if i["severity"] == ERROR)
    n_warn = sum(1 for i in issues if i["severity"] == WARNING)
    print("\n回读结果: error=%d warning=%d info=%d" % (n_err, n_warn, len(issues) - n_err - n_warn))
    if n_err:
        print("→ 有error 级问题，产物不可直接交付（脚本退出≠产物可交付）。")
    # 只有 error 才判失败；warning 是否升级由调用方的严格度（--strict / WJX_STRICT_READBACK）决定，
    # 否则默认温和的约定会被这里无条件 return 1 架空（warning 也被当失败）。
    return 1 if n_err else 0


def _main():
    ap = argparse.ArgumentParser(description="产物回读校验（交付前自检）")
    ap.add_argument("--dir", default=".", help="产物所在目录（自动识别台账与报告）")
    ap.add_argument("--issues", help="台账 CSV 路径（不给则用 --dir 里的 api_test_issues.csv）")
    ap.add_argument("--report", help="report_full.json 路径")
    ap.add_argument("--json", help="把回读结果写成 JSON（供门禁/上层消费）")
    ap.add_argument("--strict", action="store_true", help="warning 也视为失败")
    args = ap.parse_args()

    strict = args.strict or os.environ.get("WJX_STRICT_READBACK") == "1"
    issues, summary = run_readback(dirpath=args.dir, issues_csv=args.issues,
                                   report_json=args.report)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print("回读结果 JSON -> %s" % args.json)

    rc = _print_report(issues)
    # strict：warning 也算失败。默认温和（与覆盖率门禁同一套约定），
    # 只有显式 --strict / WJX_STRICT_READBACK=1 才升级。
    if strict and summary["n_warning"]:
        print("→ strict 模式：warning 视为失败。")
        rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(_main())