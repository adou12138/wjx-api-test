# -*- coding: utf-8 -*-
"""缺陷回流自进化：把新发现的缺陷抽象成"模式候选"，回写到规则库候选池待评审。

闭环的最后一环：defect_patterns.md 的 19 条模式来自历史缺陷统计归纳，
但要让它"越用越准"，就得把本轮新发现、且现有模式没覆盖的缺陷回流成新候选。

纪律（防止规则库被污染）：
  * 候选先进 references/pattern_candidates.md（候选池），**不直接改 defect_patterns.md**；
  * 每条候选必须带「证据」（缺陷现象 + 触发用例来源），评审通过后才并入正式清单；
  * 同一维度下现象高度相似的候选会被合并计数，避免刷屏。

用法：
  # 从问题台账自动提炼候选
  python pattern_feedback.py --from-csv api_test_issues.csv
  # 手工登记一条
  python pattern_feedback.py --add --dim B8 --symptom "可选字段传空被拒" --evidence "用例 B-NULL-xx"
"""
import argparse
import csv
import os
import re
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
CANDIDATES = os.path.join(HERE, "..", "references", "pattern_candidates.md")
PATTERNS = os.path.join(HERE, "..", "references", "defect_patterns.md")

HEADER = """# 缺陷模式候选池（自进化 · 待评审）

> 来源：pattern_feedback.py 从问题台账自动提炼，或人工登记。
> **纪律**：候选先进本池，评审通过后才并入 `defect_patterns.md`（正式清单）并同步到 `pattern_engine.py` 规则。
> 每条候选必须带证据（缺陷现象 + 触发用例来源），无证据的候选不得并入。

| 登记时间 | 维度 | 候选模式（现象） | 出现次数 | 证据/来源 | 评审状态 |
| --- | --- | --- | --- | --- | --- |
"""


def _existing_symptoms():
    """读正式清单里已有的模式名，用于判断候选是否已被覆盖（避免重复登记）。"""
    try:
        with open(PATTERNS, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return []
    return re.findall(r"^##\s+(?:Top\s*\d+\s*)?(.+)$", text, re.M)


def _load_rows(path):
    rows = []
    with open(path, "r", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    return rows


def _norm(s):
    s = re.sub(r"\[[^\]]*\]", "", s or "")
    s = re.sub(r"[0-9]+", "N", s)
    return re.sub(r"[\s，,。；;：:]+", "", s)[:50]


def mine_from_csv(csv_path):
    """从台账提炼候选：只取「缺陷」类，按维度+归一化现象聚合。"""
    rows = [r for r in _load_rows(csv_path)
            if (r.get("类型") or "").strip() == "缺陷"]
    agg = {}
    for r in rows:
        dim = (r.get("维度") or "-").strip()
        key = (dim, _norm(r.get("问题描述", "")))
        c = agg.setdefault(key, {"dim": dim, "symptom": r.get("问题描述", ""),
                                 "count": 0, "ids": []})
        c["count"] += 1
        c["ids"].append(r.get("编号", ""))
    return sorted(agg.values(), key=lambda c: (-c["count"], c["dim"]))


def append_candidates(items):
    path = os.path.abspath(CANDIDATES)
    if not os.path.isfile(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(HEADER)
    existing = ""
    with open(path, "r", encoding="utf-8") as f:
        existing = f.read()
    known = _existing_symptoms()
    today = datetime.now().strftime("%Y-%m-%d")
    added = 0
    lines = []
    for it in items:
        sym = (it.get("symptom") or "").strip()
        if not sym:
            continue
        # 已被正式清单覆盖的同名模式跳过（避免重复登记）
        if any(sym[:8] and sym[:8] in k for k in known):
            continue
        if _norm(sym) in existing:
            continue
        evidence = it.get("evidence") or (",".join(x for x in it.get("ids", []) if x) or "—")
        lines.append("| %s | %s | %s | %d | %s | 待评审 |" % (
            today, it.get("dim", "-"), sym.replace("|", "/")[:60],
            it.get("count", 1), evidence.replace("|", "/")[:50]))
        added += 1
    if not lines:
        print("无新增候选（可能已被正式清单覆盖或已登记）。")
        return 0
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("已登记 %d 条候选 -> %s" % (added, path))
    return added


def main():
    ap = argparse.ArgumentParser(description="缺陷回流自进化：提炼模式候选")
    ap.add_argument("--from-csv", help="从问题台账 CSV 自动提炼")
    ap.add_argument("--add", action="store_true", help="手工登记一条")
    ap.add_argument("--dim", help="维度编号")
    ap.add_argument("--symptom", help="缺陷现象（现象口吻）")
    ap.add_argument("--evidence", help="证据/触发用例来源")
    a = ap.parse_args()

    if a.add:
        if not (a.dim and a.symptom):
            ap.error("--add 需同时提供 --dim 与 --symptom")
        append_candidates([{"dim": a.dim, "symptom": a.symptom, "count": 1,
                            "evidence": a.evidence or "人工登记"}])
        return
    if a.from_csv:
        if not os.path.isfile(a.from_csv):
            sys.stderr.write("台账不存在：%s\n" % a.from_csv)
            sys.exit(2)
        items = mine_from_csv(a.from_csv)
        print("台账中缺陷类问题 %d 条，聚合出 %d 个候选模式。" % (
            sum(i["count"] for i in items), len(items)))
        append_candidates(items)
        return
    ap.error("需指定 --from-csv 或 --add")


if __name__ == "__main__":
    main()
