#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""缺陷模式池：把历轮真实缺陷沉淀成可累积、可排序的模式（v4.27.0）。

为什么需要：skill 每轮从零出题，复现过的缺陷类没有沉淀——同样的坑下一轮可能再踩。
本工具把真实缺陷产物（`*_report_full.json` 的 findings）按指纹并入池，统计每类
被多少个不同轮次命中、最高等级，输出风险 Top，让"最常翻车 / 最该先看"的类浮出来。

**边界（反假缺陷，重要）**：池只做**累积与排序**，不自动生成用例。从一个历史缺陷
推断不出"修复后的正确行为"，自动生成探针会产生错误断言或与既有生成器重复。闭环落点是
"知道什么常翻车 → 人决定补什么测试"，不是"自动补可能错的测试"。

池文件：assets/bug_patterns.json。`--merge` 幂等：按指纹去重，同一来源文件重复并入不改变池；
不同来源命中同一模式则累加 hits（hits = 命中该模式的不同来源数）。只增不改 id。
本脚本不与网络交互，只读写本地文件。

用法：
  python bug_pattern_pool.py --list
  python bug_pattern_pool.py --merge <*_report_full.json> [--pool <池路径>]
  python bug_pattern_pool.py --rank <*_report_full.json> --top 8 [--out top.md]
"""
from __future__ import annotations
import argparse
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_POOL = HERE.parent / "assets" / "bug_patterns.json"
# 缺陷来源键：**不含 "results"**——report_full.json 的 results 是逐用例执行结果（含 PASS），
# 不是缺陷；把它当来源会把执行结果误并进模式池。findings 才是缺陷本体。
ITEM_KEYS = ("findings", "issues", "defects", "violations")
SEV_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def extract_items(data):
    """从产物里取缺陷列表：优先 findings，兼容 issues/defects 等键与裸列表。"""
    if isinstance(data, list):
        return [i for i in data if isinstance(i, dict)]
    if isinstance(data, dict):
        for key in ITEM_KEYS:
            value = data.get(key)
            if isinstance(value, list):
                return [i for i in value if isinstance(i, dict)]
    return []


def _norm(text, n=60):
    return " ".join(str(text or "").split())[:n]


def _symptom(item):
    return item.get("symptom") or item.get("msg") or item.get("message") or item.get("name") or ""


def fingerprint(item):
    """缺陷→模式指纹：维度 + 类别/属性 + 现象前缀。同一模式重复并入时命中同一指纹。"""
    parts = [str(item.get("dimension") or ""),
             str(item.get("category") or item.get("property") or ""),
             _norm(_symptom(item))]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def severity_of(item):
    """等级规整：兼容 P0–P3 与中文 高/中/低（findings 两种都出现过）。"""
    sev = str(item.get("severity") or item.get("severityHint") or "P2").strip()
    upper = sev.upper()
    if upper in SEV_RANK:
        return upper
    return {"高": "P1", "中": "P2", "低": "P3", "严重": "P0", "提示": "P3"}.get(sev, "P2")


def load_pool(path):
    path = Path(path)
    if not path.exists():
        return {"version": "1.0", "patterns": []}
    return json.loads(path.read_text(encoding="utf-8"))


def save_pool(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        sys.stderr.write("无法读取/解析 %s：%s\n" % (path, e))
        return None


def cmd_list(pool_path):
    data = load_pool(pool_path)
    pats = data.get("patterns") or []
    print("缺陷模式池：%d 条（%s）" % (len(pats), pool_path))
    ranked = sorted(pats, key=lambda p: (SEV_RANK.get(p.get("severity"), 2),
                                         -int(p.get("hits", 0))))
    for p in ranked[:20]:
        print("  [%s] hits=%d %s｜%s" % (p.get("severity", "P2"), p.get("hits", 0),
                                         p.get("dimension", "-"), _norm(p.get("symptom"), 50)))
    return 0


def cmd_merge(pool_path, src):
    data = load_json(src)
    if data is None:
        return 2
    items = extract_items(data)
    pool = load_pool(pool_path)
    pats = pool.setdefault("patterns", [])
    by_fp = {p.get("fingerprint") or fingerprint(p): p for p in pats}
    src_name = Path(src).name
    changed = False
    for item in items:
        fp = fingerprint(item)
        if fp in by_fp:
            p = by_fp[fp]
            sources = p.setdefault("sources", [])
            if src_name not in sources:
                sources.append(src_name)          # 新来源命中 → hits+1（跨轮累积）
                p["hits"] = len(sources)
                changed = True
            if SEV_RANK.get(severity_of(item), 2) < SEV_RANK.get(p.get("severity"), 2):
                p["severity"] = severity_of(item)  # 等级就高
                changed = True
        else:
            changed = True
            pats.append({
                "id": "BP-AUTO-%04d" % (len(pats) + 1),
                "dimension": str(item.get("dimension") or ""),
                "category": str(item.get("category") or item.get("property") or ""),
                "symptom": _norm(_symptom(item), 120),
                "assertion": _norm(item.get("expected") or item.get("assertion") or "", 120),
                "severity": severity_of(item),
                "source": "merged:%s" % src_name,
                "sources": [src_name],
                "hits": 1,
                "fingerprint": fp,
            })
            by_fp[fp] = pats[-1]
    if changed:
        save_pool(pool_path, pool)   # 幂等：无变化不写盘
    print("并入完成：来源 %s（输入 %d 条缺陷），池现 %d 条 -> %s%s"
          % (src_name, len(items), len(pats), pool_path, "（无变化）" if not changed else ""))
    return 0


def cmd_rank(src, top, out):
    data = load_json(src)
    if data is None:
        return 2
    items = extract_items(data)
    # 排序：等级 P0>P1>P2>P3，同级按置信度降序
    def conf(i):
        try:
            return float(i.get("confidence", 0.5))
        except (TypeError, ValueError):
            return 0.5
    ranked = sorted(items, key=lambda i: (SEV_RANK.get(severity_of(i), 2), -conf(i)))
    chosen = ranked[:max(1, top)]
    lines = ["# 风险排序 Top %d（共 %d 条候选）" % (len(chosen), len(items)), ""]
    for idx, item in enumerate(chosen, 1):
        sel = ""
        el = item.get("element") or {}
        if isinstance(el, dict):
            sel = el.get("selector") or ""
        lines.append("%d. [%s] %s｜%s" % (idx, severity_of(item),
                                          item.get("dimension") or "-", _norm(_symptom(item), 160)))
        if sel:
            lines.append("   - 定位：%s" % sel)
        lines.append("   - 置信度：%.2f" % conf(item))
    text = "\n".join(lines) + "\n"
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(text, encoding="utf-8")
        print("风险排序 Top %d 已写入 %s（候选 %d 条）" % (len(chosen), out, len(items)))
    else:
        print(text)
    return 0


def main():
    ap = argparse.ArgumentParser(description="缺陷模式池累积与风险排序")
    ap.add_argument("--pool", type=Path, default=DEFAULT_POOL, help="池文件（默认 assets/bug_patterns.json）")
    ap.add_argument("--list", action="store_true", help="列出池内容汇总")
    ap.add_argument("--merge", type=Path, help="把缺陷产物里的新模式并入池（幂等）")
    ap.add_argument("--rank", type=Path, help="对缺陷产物做风险排序")
    ap.add_argument("--top", type=int, default=8, help="--rank 输出条数")
    ap.add_argument("--out", type=Path, help="--rank 的 Markdown 输出路径（省略则打印）")
    args = ap.parse_args()
    if args.list:
        return cmd_list(args.pool)
    if args.merge:
        return cmd_merge(args.pool, args.merge)
    if args.rank:
        return cmd_rank(args.rank, args.top, args.out)
    ap.error("需要 --list / --merge / --rank 之一")
    return 2


if __name__ == "__main__":
    sys.exit(main())
