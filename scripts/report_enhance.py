# -*- coding: utf-8 -*-
"""报告增强：把问题台账变成"可直接提单"的形态。

三件事（对应易用性优化）：
  1) 聚类去重：同一维度 + 同一现象的问题合并，附出现次数，避免台账被重复项淹没；
  2) 复现命令：按请求 Body 生成可复制的 curl（含公共参数），开发能原样复现；
  3) 修复建议：按维度编号给出排查方向（挂维度号，便于对照 framework.md 定位）。

用法：
  python report_enhance.py --issues api_test_issues.csv [--out api_test_issues_enhanced]
  # 或在 flow.py 里自动调用
"""
import argparse
import csv
import json
import os
import re

FIX_SUGGEST = {
    "A": "核对鉴权与权限矩阵：签名/Token 是否有效、主子账号与跨企业越权是否被拦截、401 与 403 是否区分",
    "B": "补齐参数校验：必填缺失/类型错误/枚举越界/长度边界逐一断言「应被拒绝」；同时检查可选字段是否被过度校验（B8）",
    "C": "字段语义真值：写操作后必须回读（详情/列表/计数）验证真落库；URL 类字段验证真能打开",
    "D": "业务主链路：补齐异常分支与依赖链，不只测成功路径",
    "E": "状态机：非法转换、终态重复操作应有明确提示，不得静默返回 true",
    "F": "组合交叉：多字段同时变异，检查条件必填与互斥约束",
    "G": "安全深化：注入/越权/SSRF/重放/信息泄露，检查是否返回堆栈或内部错误",
    "H": "错误处理质量：错误码与文案映射一致、根因不掩盖（不同成因错误必须不同）、无未渲染占位符",
    "I": "编码与国际化：特殊字符/emoji/空字节/多语言文案",
    "J": "性能与稳定性：并发正确性、限流阈值与配置一致、大字段不超时",
    "K": "契约测试：响应字段与文档双向 diff、命名风格统一、枚举释义与实现一致",
    "L": "兼容性：向后/向前兼容，旧客户端解析不崩",
    "Y": "复杂结构返回：各子类型结构完整、无缺失层级",
    "AA": "分页与列表：页码/条数边界、深翻页、排序稳定、筛选无效时严禁静默返回全量",
    "AE": "认证与传输补充：请求头/传输契约、认证生命周期、重放时间窗",
    "AB": "前端-接口口径：以页面基线为预期值，核对权限/校验/展示/枚举/默认值五类口径",
    "AC": "幂等与业务唯一性：重复提交应被拒、错误信息回显冲突值且对象名正确",
    "AD": "事件与回调：事件完备、不重不漏、载荷准确、订阅范围正确",
    "AF": "业务场景：主业务流 / 业务规则校验 / 业务组合场景（补机械展开覆盖不到的业务语义）",
    "Z": "国际化/多语言：语言枚举、时区换算（与页面差固定小时数即为缺陷）",
    "M": "导出下载：链接可访问且在有效期内、数据口径正确、空数据有提示",
    "X": "批量操作：部分成功语义、失败明细、不得因一项失败导致整体失败",
    "U": "批量导入：数据合法性分级、部分成功、错误可定位",
}

SEVERITY_ORDER = {"高": 0, "中": 1, "低": 2, "信息": 3, "": 4}

# 维度列实际取值是「字母前缀 + 子号」（如 B8 / AA3 / H9），而 FIX_SUGGEST 的主键是
# 维度前缀（B / AA / H）。精确匹配失败时按前缀回退，否则"挂维度号给排查方向"
# 会对绝大多数真实 finding 静默退化成兜底文案。
_LEAD_ALPHA_RE = re.compile(r"^([A-Za-z]+)")


def _fix_for(dim):
    """维度编号 -> 修复建议：先精确匹配，再按字母前缀匹配，最后兜底。"""
    d = (dim or "").strip()
    if d in FIX_SUGGEST:
        return FIX_SUGGEST[d]
    m = _LEAD_ALPHA_RE.match(d)
    if m and m.group(1).upper() in FIX_SUGGEST:
        return FIX_SUGGEST[m.group(1).upper()]
    return "对照 framework.md 该维度逐项核对"


def _norm(desc):
    """归一化描述用于聚类：去掉编号、具体值与标点差异。"""
    s = re.sub(r"\[[^\]]*\]", "", desc or "")
    s = re.sub(r"[0-9]+", "N", s)
    s = re.sub(r"[\s，,。；;：:]+", "", s)
    return s[:60]


def _curl(body_text, base_url=None):
    url = base_url or os.environ.get("WJX_BASE_URL") or "<WJX_BASE_URL>"
    body = body_text or "{}"
    if isinstance(body, (dict, list)):
        body = json.dumps(body, ensure_ascii=False)
    body = body.replace('"', '\\"')
    return 'curl -X POST "%s" -H "Content-Type: application/json" -d "%s"' % (url, body)


def enhance(csv_path, out_prefix):
    if not os.path.isfile(csv_path):
        print("未找到问题台账：%s" % csv_path)
        return
    rows = []
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rows.append(r)

    clusters = {}
    for r in rows:
        dim = (r.get("维度") or "-").strip()
        key = (dim, _norm(r.get("问题描述", "")))
        c = clusters.setdefault(key, {
            "dim": dim, "desc": r.get("问题描述", ""), "count": 0,
            "severity": r.get("严重程度", ""), "ids": [],
            "expected": r.get("预期结果", ""), "actual": r.get("实际结果", ""),
            "body": r.get("请求Body", ""),
        })
        c["count"] += 1
        c["ids"].append(r.get("编号", ""))
        if SEVERITY_ORDER.get(r.get("严重程度", ""), 4) < SEVERITY_ORDER.get(c["severity"], 4):
            c["severity"] = r.get("严重程度", "")

    ordered = sorted(clusters.values(),
                     key=lambda c: (SEVERITY_ORDER.get(c["severity"], 4), -c["count"], c["dim"]))

    # 增强 CSV
    with open("%s.csv" % out_prefix, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["聚类编号", "维度", "严重程度", "出现次数", "问题单号", "问题描述",
                    "预期结果", "实际结果", "修复建议", "复现命令"])
        for i, c in enumerate(ordered, 1):
            w.writerow(["CL-%02d" % i, c["dim"], c["severity"], c["count"],
                        ",".join(x for x in c["ids"] if x), c["desc"], c["expected"],
                        c["actual"], _fix_for(c["dim"]),
                        _curl(c["body"])])

    # 人类可读 MD
    lines = ["# 接口测试问题报告（增强版）", "",
             "共 %d 条原始问题，聚类去重后 %d 类。" % (len(rows), len(ordered)), "",
             "| 聚类 | 维度 | 严重程度 | 次数 | 问题描述 | 修复建议 |", "| --- | --- | --- | --- | --- | --- |"]
    for i, c in enumerate(ordered, 1):
        lines.append("| CL-%02d | %s | %s | %d | %s | %s |" % (
            i, c["dim"], c["severity"], c["count"], c["desc"].replace("|", "/")[:60],
            _fix_for(c["dim"]).replace("|", "/")[:40]))
    lines += ["", "## 复现命令", ""]
    for i, c in enumerate(ordered, 1):
        lines.append("**CL-%02d**（%s）" % (i, c["dim"]))
        lines.append("```")
        lines.append(_curl(c["body"]))
        lines.append("```")
    with open("%s.md" % out_prefix, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print("增强报告 -> %s.csv / %s.md（%d 条原始 -> %d 类）" % (out_prefix, out_prefix, len(rows), len(ordered)))


def main():
    ap = argparse.ArgumentParser(description="问题台账增强（聚类/复现/建议）")
    ap.add_argument("--issues", required=True, help="问题台账 CSV")
    ap.add_argument("--out", default="api_test_issues_enhanced", help="输出前缀")
    a = ap.parse_args()
    enhance(a.issues, a.out)


if __name__ == "__main__":
    main()
