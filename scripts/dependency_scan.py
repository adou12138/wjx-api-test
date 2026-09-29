# -*- coding: utf-8 -*-
"""从契约正文梳理**显式声明**的跨接口依赖（保守取数，不猜）。

为什么需要：当被测接口必须"先调另一个接口"才能成功时，只测本接口会得到一条
看似接口缺陷、实为前置条件未满足的假缺陷。典型例子是 `1000108 更新AI主页`
正文写着：

    AI主页处于发布状态时不能直接更新，请先调用修改问卷状态接口1000102，
    传入state=2暂停主页后再更新。

这类关系过去完全丢失：生成器不知道 1000102 的存在，于是"发布态下更新失败"
就只能靠人读文档发现。

取数纪律（与语义层一致，宁可少收也不误收）：
  1. **只认契约正文白纸黑字写出的依赖**，不做字段名相似性推断，不跨文档猜；
  2. 每条依赖必须带 `evidence`，且是契约原文的**连续子串**；
  3. 正文提到"要先调某接口"但拿不到 action 号时，进 `unknowns` 留痕，
     **不生成依赖、也不生成用例**——定不到位就不调，猜错比不测更糟；
  4. 请求示例里的 `"action": "1000108"` 是文档自身标识，必须排除；
     否则每条契约都会把自己识别成自己的依赖。

产出三类：
  - `depends_on`      前置依赖（触发词 + action 号）
  - `companion_read`  显式声明的配套读接口（`read_action` / `list_action` 字段，
                      或「配套读接口」「列表接口」等字样 + action 号）
  - `unknowns`        疑似依赖但**无法定位**的项，只留痕

用法：
  python dependency_scan.py --contract <契约路径>
  python dependency_scan.py --contract <契约路径> --out dependencies.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from contract_model import Evidence, RelationModel  # noqa: E402

_ACTION_RE = re.compile(r"(?<!\d)(1\d{6})(?!\d)")
_NAME_RE = re.compile(r"调用\s*([^，。；、,;:：()（）\s]{2,24}?)\s*(?:接口)?\s*(1\d{6})")
# 参数值只取 ASCII 短 token（数字 / 标识 / 枚举）。中文取值没有可靠边界，
# 贪婪匹配会把「state=2暂停主页后再更新」的整句尾巴当取值——**宁可不记也不记错**，
# 原句始终保留在 evidence 里可供人核对。
_PARAM_RE = re.compile(r"(?:传入|传参|设置|置为|置)\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([0-9A-Za-z_.\-/:*]+)")

# 触发词：必须表达"要先用另一个接口"这层意思。裸"先"不进，避免"先生成"这类误命中。
_DEP_TRIGGERS = ("请先调用", "请先使用", "请先通过", "需先调用", "需要先调用", "必须先调用",
                 "先调用", "先行调用", "请先", "需先", "必须先")

# 配套读接口的显式字样；命中后仍要求同行出现 action 号才收。
_COMPANION_KEYWORDS = ("配套接口", "配套读接口", "配套查询接口", "回读接口", "列表接口")
_COMPANION_FIELDS = (("read_action", "读接口"), ("list_action", "列表接口"))

_SELF_TEXT_MAX = 200


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _split_sentences(line: str):
    """按句末标点切句；保留原文片段，便于直接当 evidence 用。"""
    out, buf = [], ""
    for ch in line:
        buf += ch
        if ch in "。；;":
            out.append(buf)
            buf = ""
    if buf.strip():
        out.append(buf)
    return out


def _self_action(contract_path: str, contract=None) -> str:
    """本契约自己的 action：优先取解析结果，其次取文件名里的 [1000107]。"""
    act = str((contract or {}).get("action") or "").strip()
    if act:
        return act
    m = re.search(r"\[(\d{7})\]", os.path.basename(contract_path or ""))
    return m.group(1) if m else ""


def _mk_dep(target_action, target_name, kind, reason, excerpt, contract_path, line):
    return {
        "target_action": target_action,
        "target_name": target_name,
        "kind": kind,
        "confidence": 1.0,
        "reason": reason,
        "evidence": excerpt,
        "source": contract_path,
        "line": line,
    }


def scan_text(text: str, contract_path: str = "", self_action: str = "",
              contract=None) -> dict:
    """从契约正文文本中梳理显式依赖。返回 dependencies / unknowns 两类记录。"""
    deps, unknowns, seen = [], [], set()
    lines = text.split("\n")

    for idx, line in enumerate(lines, start=1):
        for raw_sent in _split_sentences(line):
            sent = raw_sent.strip()
            if not sent:
                continue
            trigger = next((t for t in _DEP_TRIGGERS if t in sent), None)
            if not trigger:
                continue
            codes = [c for c in _ACTION_RE.findall(sent) if c and c != self_action]
            if codes:
                name_hit = _NAME_RE.search(sent)
                params = dict(_PARAM_RE.findall(sent))
                for code in codes:
                    key = (code, "depends_on")
                    if key in seen:
                        continue
                    seen.add(key)
                    name = name_hit.group(1) if (name_hit and name_hit.group(2) == code) else ""
                    record = _mk_dep(
                        code, name, "depends_on",
                        "契约正文显式声明需先调用该接口",
                        sent, contract_path, idx,
                    )
                    if params:
                        record["params"] = params
                    # 触发词之前的部分是原文里的前置语境，原样保留、不加工成结论
                    context = sent.split(trigger, 1)[0].strip()
                    if context:
                        record["context"] = context
                    deps.append(record)
            elif "接口" in sent:
                # 说了要调接口却给不出 action 号：定不到位，只留痕
                unknowns.append({
                    "kind": "dependency_unresolved",
                    "reason": "契约正文声明需先调用接口，但未给出 action 号，无法定位真实接口",
                    "evidence": sent,
                    "source": contract_path,
                    "line": idx,
                })

    deps.extend(_scan_companion_fields(text, contract_path, self_action, contract, seen))
    deps.extend(_scan_companion_text(text, contract_path, self_action, seen))
    return {"dependencies": deps, "unknowns": unknowns}


def _scan_companion_fields(text, contract_path, self_action, contract, seen):
    """契约显式声明的 read_action / list_action。"""
    out = []
    lines = text.split("\n")
    for key, label in _COMPANION_FIELDS:
        val = str((contract or {}).get(key) or "").strip()
        if not val or val == self_action or not _ACTION_RE.fullmatch(val):
            continue
        key_seen = (val, "companion_read")
        if key_seen in seen:
            continue
        seen.add(key_seen)
        excerpt = next((ln.strip() for ln in lines if key in ln), val)
        out.append(_mk_dep(val, label, "companion_read",
                           "契约显式声明配套接口（%s）" % key,
                           excerpt, contract_path, 0))
        continue
    return out


def _scan_companion_text(text, contract_path, self_action, seen):
    """正文里「配套读接口 1000108」这类显式字样。"""
    out = []
    for idx, line in enumerate(text.split("\n"), start=1):
        if not any(k in line for k in _COMPANION_KEYWORDS):
            continue
        for code in _ACTION_RE.findall(line):
            if code == self_action:
                continue
            key_seen = (code, "companion_read")
            if key_seen in seen:
                continue
            seen.add(key_seen)
            out.append(_mk_dep(code, "", "companion_read",
                               "契约正文显式声明配套接口",
                               line.strip(), contract_path, idx))
    return out


def evidence_is_substring(text: str, excerpt: str) -> bool:
    """证据纪律：excerpt 必须是契约原文的连续子串。"""
    return bool(excerpt) and excerpt in text


def to_relations(result: dict, contract_path: str = "") -> list:
    """把依赖记录转成 ContractModel 可消费的 RelationModel 列表。"""
    out = []
    for dep in result.get("dependencies") or []:
        reason = dep.get("reason") or ""
        if dep.get("context"):
            reason = "%s（原文语境：%s）" % (reason, dep["context"])
        if dep.get("params"):
            reason = "%s；要求参数 %s" % (reason, json.dumps(dep["params"], ensure_ascii=False))
        out.append(RelationModel(
            relation_type=dep.get("kind") or "depends_on",
            resource=dep.get("target_name") or dep.get("target_action") or "",
            target_operation=dep.get("target_action"),
            confidence=float(dep.get("confidence", 1.0)),
            reason=reason,
            evidence=[Evidence(source=dep.get("source") or contract_path,
                               line=dep.get("line"), confidence=1.0,
                               excerpt=dep.get("evidence") or "")],
        ))
    return out


def annotate(model, contract_path: str = ""):
    """把显式依赖追加进模型的关系列表，返回本次新增的依赖记录。

    只追加、不覆盖：`relationship_inference` 推断出的操作类型关系保持原样。
    """
    path = contract_path or (model.metadata or {}).get("source") or ""
    contract = {"action": model.operation}
    text = read_text(path) if path and os.path.isfile(path) else ""
    result = scan_text(text, contract_path=path, self_action=str(model.operation or ""),
                       contract=contract) if text else {"dependencies": [], "unknowns": []}
    model.relations.extend(to_relations(result, path))
    return result


def main():
    ap = argparse.ArgumentParser(description="从契约正文梳理显式声明的跨接口依赖")
    ap.add_argument("--contract", required=True, help="契约文档路径")
    ap.add_argument("--action", help="本契约 action；省略时从解析结果或文件名推断")
    ap.add_argument("--out", help="依赖结果 JSON 落盘路径")
    args = ap.parse_args()

    contract = None
    try:
        from contract_parser import load_contract  # noqa: E402
        contract = load_contract(args.contract, action=args.action)
    except Exception as exc:  # 解析失败不阻断：正文扫描不依赖解析结果
        print("WARN: 契约解析失败，仅按正文扫描：%s" % exc, file=sys.stderr)

    text = read_text(args.contract)
    self_action = _self_action(args.contract, contract)
    result = scan_text(text, contract_path=args.contract, self_action=self_action,
                       contract=contract)

    bad = [d for d in result["dependencies"] if not evidence_is_substring(text, d["evidence"])]
    if bad:
        print("ERROR: %d 条依赖的证据不是契约原文子串，已剔除" % len(bad), file=sys.stderr)
        result["dependencies"] = [d for d in result["dependencies"] if d not in bad]

    result["self_action"] = self_action
    payload = {
        "self_action": self_action,
        "contract": args.contract,
        "dependencies": result["dependencies"],
        "unknowns": result["unknowns"],
    }
    print("self_action: %s" % (self_action or "(未知)"))
    print("显式依赖: %d 条" % len(payload["dependencies"]))
    for dep in payload["dependencies"]:
        print("  [%s] %s %s  ← %s" % (dep["kind"], dep["target_action"],
                                      dep["target_name"] or "", dep["evidence"][:40]))
    print("无法定位（待确认）: %d 条" % len(payload["unknowns"]))
    for unk in payload["unknowns"]:
        print("  [unknowns] %s" % unk["evidence"][:60])
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print("产物 -> %s" % args.out)


if __name__ == "__main__":
    main()
