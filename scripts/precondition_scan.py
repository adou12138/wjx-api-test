# -*- coding: utf-8 -*-
"""从**真实执行结果**中发现跨接口依赖（运行实证通道）。

与 `dependency_scan.py` 的分工（两条通道互补，不是替代）：
  - `dependency_scan.py` 读**契约正文**，只收白纸黑字写出的声明。
    优点：不依赖执行；缺点：契约普遍不写，193 篇里只命中 1 篇。
  - 本模块读**执行产物**，从真实失败响应里发现前置依赖。
    优点：能发现契约没写、但实际存在的前置条件；缺点：**必须先真的跑失败一次**，
    用例没构造出前置场景就永远发现不了。

所以本模块只提高召回上限，不提高下限；两条通道的结果合并时以本通道（真实证据）优先。

为什么需要它：当被测接口必须先调另一个接口才能成功时，直接测会得到一条
"看似接口缺陷、实为前置条件未满足"的假缺陷，还会污染缺陷台账。典型如
`1000108 更新AI主页` 在发布态下返回「请先调用修改问卷状态接口1000102…」。

取数纪律（比静态通道更严，因为响应是自由文本、误报风险更高）：
  1. **规则先筛**：失败文案必须命中白名单话术（"请先调用"/"需先"/"前置条件"等），
     不命中不进模型，也不生成任何记录——宁可漏，不可猜；
  2. **模型只抽事实**：从响应里抽「依赖谁、要什么条件」，**证据必须是该条响应的
     原文连续子串**，机械校验不过即整条丢弃；
  3. **定不位就不记**：模型说不出 7 位 action 号时只进 `unknowns` 留痕，
     不生成依赖。猜一个错 action 比不记录更糟；
  4. **候选不等于生效**：默认产出 `candidate`，不参与归因、不参与生成。
     人工确认（`--confirm`）转为 `confirmed` 之后才开始生效。

链路（与 `llm_semantics.py` 同一套 prep/merge 模式，本模块不调用任何模型）：
  1. --prep    读执行报告，筛出疑似前置失败，生成抽取提示词
  2. 由大模型按提示词产出事实 JSON
  3. --merge   读事实 JSON，机械校验，通过的记为候选
  4. --confirm 人工确认候选（或 --confirm-all），转正式
  5. --markdown 把已确认的依赖渲染成可写入知识库文档的「运行实证」节

捷径（v4.28.0）：--auto 规则直提——当失败文案里「明确前置话术 + 邻近 1xxxxxx action 号」
同时出现时，免 LLM 直接产候选（action 号原样在文案里、非猜；仍是候选，须 --confirm）。
话术命中但邻近无 action 号的仍走 --prep/模型或进 unknowns，不猜。

用法：
  python precondition_scan.py --prep --report <prefix>_report_full.json
  python precondition_scan.py --merge facts.json --report <prefix>_report_full.json
  python precondition_scan.py --auto --report <prefix>_report_full.json --store runtime_dependencies.json
  python precondition_scan.py --confirm-all --store runtime_dependencies.json
  python precondition_scan.py --markdown --store runtime_dependencies.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 记录状态：候选不生效，确认后才生效。
CANDIDATE = "candidate"
CONFIRMED = "confirmed"
REJECTED = "rejected"

# 白名单话术：必须表达「有前置条件没满足」这层意思。
# 刻意收窄——"失败""错误""参数"这类词一律不进，否则任何校验失败都会被拉进来。
_PRECONDITION_HINTS = (
    "请先调用", "请先使用", "请先通过", "请先暂停", "请先创建", "请先设置",
    "需先调用", "需要先调用", "必须先调用", "要先调用",
    "请先", "需先", "必须先", "先行调用",
    "前置条件", "前置依赖", "前置未满足", "未满足前置", "前置校验",
    "暂不支持此操作", "当前状态不支持", "当前状态下不允许", "该状态下不允许",
)

# 唯一可外部消费的 action 形态：7 位数字（问卷星 action 编号）。
_ACTION_RE = re.compile(r"(?<!\d)(1\d{6})(?!\d)")

# 规则直提（v4.28.0）：仅"明确前置话术 + 邻近 1xxxxxx action 号"才直提，避免把
# 时间戳/ID 误当 action。捕获组带前后数字边界（同 _ACTION_RE）——否则 8 位数字会截出
# 错误的 7 位号且能骗过 verify 的 fullmatch。只作 candidate，仍须人工 --confirm。
_AUTO_DEP_RE = re.compile(
    r"(?:请先调用|需先调用|需要先调用|必须先调用|要先调用|先行调用|请先|需先|必须先|前置条件)[^\n]{0,16}?((?<!\d)1\d{6}(?!\d))")

# 该条响应里可供引用的原文上限，避免把整份报告塞进提示词。
MAX_RESPONSE_CHARS = 1200


def _text_of(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value)


def response_text(record: dict) -> str:
    """一条执行结果里**可供引用的响应原文**。

    evidence 校验以它为准：模型给的证据必须是这里的连续子串。
    把几个候选来源都拼进来（errormsg / note / reason / 整个响应体），
    是因为同一层语义在不同接口里落在不同字段上——只取 errormsg 会把
    文案写在 note 里的接口全部漏掉。
    """
    resp = record.get("response")
    parts = []
    if isinstance(resp, dict):
        for key in ("errormsg", "error_msg", "msg", "message", "note", "error", "detail"):
            parts.append(_text_of(resp.get(key)))
        parts.append(_text_of(resp))
    else:
        parts.append(_text_of(resp))
    for key in ("reason", "name"):
        parts.append(_text_of(record.get(key)))
    seen, out = set(), []
    for p in parts:
        p = (p or "").strip()
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return " ".join(out)


def is_failure(record: dict) -> bool:
    """该条结果是否属于"跑了但没通过"。

    只收 PASS 以外的终态。N/A 与 SKIPPED 是"没测"，不是"失败"，
    把它们算进来会把"缺基线"误判成"前置条件未满足"。
    """
    status = str(record.get("status") or "").upper()
    return status in ("FAIL", "ERROR") or status == "PRECONDITION_UNMET"


def harvest(report: dict, subject_name: str = "") -> list:
    """规则预筛：从执行报告里挑出**疑似前置条件未满足**的失败。

    只做筛选，不下结论——命中白名单只代表"值得让模型看一眼"，
    真正的依赖关系由第 2 步的模型抽取 + 第 3 步的机械校验给出。

    `subject_name` 是被测接口自己的名字（执行报告里只有用例名，没有接口名）。
    不传则留空，绝不拿用例名顶替——那会让"谁依赖谁"在文档里读错。
    """
    out = []
    for rec in report.get("results") or []:
        if not isinstance(rec, dict) or not is_failure(rec):
            continue
        text = response_text(rec)
        hit = [h for h in _PRECONDITION_HINTS if h in text]
        if not hit:
            continue
        out.append({
            "case_no": rec.get("case_no") or "",
            "case_name": rec.get("name") or "",
            "subject_name": subject_name,
            "dimension": rec.get("dimension") or "",
            "action": str(rec.get("action") or ""),
            "status": rec.get("status") or "",
            "hint": hit[0],
            "hint_all": hit,
            "response_excerpt": text[:MAX_RESPONSE_CHARS],
        })
    return out


def auto_facts(candidates: list) -> dict:
    """规则直提（v4.28.0）：从候选失败直接产事实，免 LLM——仅限"明确前置话术 + 邻近 action 号"。

    sound 边界：action 号必须**原样出现在响应文案里且紧邻前置话术**（不是猜的），
    evidence 取该号上下文原文连续子串；产出仍是 candidate，须人工 --confirm 才生效。
    每条候选只提第一个命中；自引用跳过；话术命中但邻近无 action 号的不走本条
    （留给 --prep 让模型看，或进 unknowns），不猜。
    """
    deps = []
    for c in candidates:
        text = c.get("response_excerpt") or ""
        m = _AUTO_DEP_RE.search(text)
        if not m:
            continue
        target = m.group(1)
        if c.get("action") and target == str(c.get("action")):
            continue
        start = max(0, m.start() - 12)
        end = min(len(text), m.end() + 12)
        deps.append({
            "case_no": c.get("case_no") or "",
            "target_action": target,
            "target_hint": "auto",
            "evidence": text[start:end],
        })
    return {"dependencies": deps}


# ------------------------------------------------------------------- prep
def build_prompt(candidates: list, artifact: str = "") -> str:
    """生成抽取提示词（自包含）。由人工/外部模型消费，本模块不调用模型。"""
    if not candidates:
        return ""
    lines = [
        "# 任务：从真实失败响应中抽取「前置依赖」事实",
        "",
        "下面是接口测试执行中**真实失败**的用例。请判断每条失败是否属于"
        "「前置条件未满足」——即这个接口本身没错，只是要先调用别的接口/先把对象"
        "置于某个状态才能成功。",
        "",
        "## 硬性要求（不满足的事实会被机械校验丢弃）",
        "",
        "1. `evidence` 必须是该条 `response_excerpt` 里的**连续原文子串**，一字不改。",
        "2. `target_action` 只填 7 位数字的 action 号。**原文里没有 action 号就留空**，"
        "由 `target_hint` 描述即可——不要凭接口名猜一个号，猜错比不记更糟。",
        "3. 不确定是否属前置依赖时，**不要输出这条**（宁可漏，不可猜）。",
        "4. 只输出 JSON，不要解释文字。",
        "",
        "## 输出格式",
        "",
        "```json",
        '{"dependencies": [',
        '  {"case_no": "用例编号", "target_action": "1000102",',
        '   "target_name": "修改问卷状态", "condition": "AI主页处于发布状态",',
        '   "params": {"state": "2"}, "evidence": "从响应里原样摘的连续子串"}',
        "]}",
        "```",
        "",
        f"## 待判定失败（共 {len(candidates)} 条）",
        "",
    ]
    for i, c in enumerate(candidates, 1):
        lines += [
            f"### {i}. {c['case_no']} {c['case_name']}",
            "",
            f"- 主体接口 action：`{c['action'] or '(未知)'}`",
            f"- 命中话术：`{c['hint']}`",
            "- 响应原文：",
            "",
            "```",
            c["response_excerpt"],
            "```",
            "",
        ]
    if artifact:
        lines += [f"> 来源报告：`{artifact}`", ""]
    return "\n".join(lines)


# ------------------------------------------------------------------ merge
def _norm_params(raw) -> dict:
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, v in raw.items():
        k = str(k).strip()
        if not k:
            continue
        out[k] = _text_of(v).strip()
    return out


def verify(facts: dict, candidates: list, known_actions=None):
    """机械校验。返回 (rows, clean)。

    能拦：case_no 不在本次候选里、evidence 缺失或不是该条响应原文子串、
          target_action 形态非法、target_action 不在给定的已知集合里、自引用。
    拦不住：evidence 与它声称的依赖无关（那段话确实是原文，但说的是别的事）。
        所以这些记录一律停在 `candidate`，必须人工确认后才生效——
        这是本模块最重要的一道闸，因为它直接决定要不要吞掉一条失败。
    """
    cand_map = {c["case_no"]: c for c in candidates if c.get("case_no")}
    known = {str(a) for a in (known_actions or []) if str(a).strip()}
    rows, clean = [], []
    seen = set()

    for item in (facts or {}).get("dependencies") or []:
        if not isinstance(item, dict):
            rows.append(["(非对象)", "shape", "BLOCKED", "事实项不是对象"])
            continue
        case_no = str(item.get("case_no") or "").strip()
        cand = cand_map.get(case_no)
        if cand is None:
            rows.append([case_no or "(空)", "case_no", "BLOCKED",
                         "case_no 不在本次候选失败清单里（可能是模型臆造的用例）"])
            continue

        ev = str(item.get("evidence") or "").strip()
        if not ev:
            rows.append([case_no, "evidence", "BLOCKED", "未给出 evidence"])
            continue
        if ev not in cand["response_excerpt"]:
            rows.append([case_no, "evidence", "BLOCKED",
                         "evidence 不是该条响应原文子串：%s" % ev[:50]])
            continue

        subject = cand.get("action") or ""
        target = str(item.get("target_action") or "").strip()
        status = CANDIDATE
        note = ""
        if target:
            if not _ACTION_RE.fullmatch(target):
                rows.append([case_no, "target_action", "BLOCKED",
                             "target_action 不是 7 位 action 号：%s" % target[:20]])
                continue
            if subject and target == subject:
                rows.append([case_no, "self_ref", "BLOCKED",
                             "依赖指向自身（%s），排除" % target])
                continue
            if known and target not in known:
                status = "UNKNOWN_ACTION"
                note = "target_action 不在给定已知集合内，待人工核对"
        else:
            status = "UNRESOLVED"
            note = "原文未给出 action 号，无法定位真实接口——只留痕，不生成依赖"

        key = (subject, target, ev)
        if key in seen:
            rows.append([case_no, "duplicate", "SKIPPED", "同一依赖重复出现，已合并"])
            continue
        seen.add(key)

        clean.append({
            "subject_action": subject,
            "subject_name": cand.get("subject_name") or "",
            "target_action": target,
            "target_name": str(item.get("target_name") or "").strip(),
            "target_hint": str(item.get("target_hint") or "").strip(),
            "condition": str(item.get("condition") or "").strip(),
            "params": _norm_params(item.get("params")),
            "evidence": ev,
            "case_no": case_no,
            "case_name": cand.get("case_name") or "",
            "dimension": cand.get("dimension") or "",
            "hint": cand.get("hint") or "",
            "source": "runtime",
            "status": status,
            "confidence": float(item.get("confidence", 0.8) or 0.8),
            "note": note,
        })
        rows.append([case_no, "ok", status, note or "机械校验通过（待人工确认）"])
    return rows, clean


def dedup_merge(existing: list, incoming: list) -> list:
    """并入已有记录。同一 (主体, 目标, 证据) 视为同一条，保留已有状态。

    保留已有状态是刻意的：一条已被人工确认的依赖，不应因为重跑一次
    又退回"候选"——那等于让人重复确认同一件事。
    """
    out = list(existing or [])
    index = {}
    for i, r in enumerate(out):
        index[(r.get("subject_action"), r.get("target_action"), r.get("evidence"))] = i
    for rec in incoming or []:
        key = (rec.get("subject_action"), rec.get("target_action"), rec.get("evidence"))
        if key in index:
            old = out[index[key]]
            # 已有记录若已确认，保持确认态；其余字段用新信息补齐。
            if old.get("status") == CONFIRMED:
                rec["status"] = CONFIRMED
            out[index[key]] = rec
        else:
            index[key] = len(out)
            out.append(rec)
    return out


def _load(path, default):
    if not path or not os.path.isfile(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(path, obj):
    if not path:
        return
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def confirm(records: list, keys=None, all_flag=False) -> int:
    """人工确认：候选转正式。返回本次转正条数。

    只允许候选（或未知 action / 未定位）转正，不直接改已拒绝的——
    把拒绝项再拉回来需要显式重新 merge，避免误操作让被否掉的依赖复活。
    """
    n = 0
    want = {str(k).strip() for k in (keys or []) if str(k).strip()}
    for rec in records:
        if rec.get("status") == CONFIRMED:
            continue
        if all_flag or (rec.get("case_no") in want) or (rec.get("target_action") in want):
            rec["status"] = CONFIRMED
            n += 1
    return n


def confirmed_only(records: list) -> list:
    """只有这一批才允许影响归因、影响生成、写入知识库。"""
    return [r for r in records if r.get("status") == CONFIRMED]


def render_markdown(records: list, date: str = "") -> str:
    """渲染成可写入知识库文档「运行实证」一节的内容。"""
    if not records:
        return ""
    lines = [
        "### 运行实证（测试中发现的依赖）",
        "",
        "> 来源：**真实执行结果**。与上方「契约显式声明」是两条独立证据通道——",
        "> 本节每条都来自一次真实的失败响应，`evidence` 是该响应原文的连续子串。",
        f"> 发现时间：{date or ''}",
        "",
        "| 主体接口 | 依赖接口 | 依赖接口名 | 前置条件 | 要求参数 | 响应原文证据 | 状态 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in records:
        params = r.get("params") or {}
        pstr = "；".join("`%s=%s`" % (k, v) for k, v in params.items()) or "—"
        lines.append("| %s %s | **%s** | %s | %s | %s | `%s` | %s |" % (
            r.get("subject_action") or "?", r.get("subject_name") or "",
            r.get("target_action") or "（未定位）", r.get("target_name") or "—",
            r.get("condition") or "—", pstr,
            (r.get("evidence") or "").replace("|", "\\|")[:160],
            "已确认" if r.get("status") == CONFIRMED else "待确认",
        ))
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description="从真实执行结果中发现跨接口依赖（运行实证）")
    ap.add_argument("--report", help="执行报告 *_report_full.json")
    ap.add_argument("--prep", action="store_true", help="生成抽取提示词")
    ap.add_argument("--prompt-out", help="提示词落盘路径")
    ap.add_argument("--merge", help="并入大模型产出的事实 JSON")
    ap.add_argument("--store", default="runtime_dependencies.json", help="依赖记录落盘路径")
    ap.add_argument("--known-actions", help="已知 action 全集，逗号分隔；给出后才做存在性校验")
    ap.add_argument("--manual", help="人工登记的依赖 JSON（与 merge 同格式）")
    ap.add_argument("--auto", action="store_true",
                    help="规则直提：话术+邻近 action 号明确时免 LLM 直接产候选（仍需 --confirm 生效）")
    ap.add_argument("--confirm", help="确认指定候选（case_no 或 target_action），逗号分隔")
    ap.add_argument("--confirm-all", action="store_true", help="确认全部候选")
    ap.add_argument("--markdown", action="store_true", help="渲染「运行实证」节")
    ap.add_argument("--date", help="发现日期，写入渲染结果")
    args = ap.parse_args()

    report = _load(args.report, {})
    candidates = harvest(report)
    known = [a.strip() for a in (args.known_actions or "").split(",") if a.strip()] or None

    if args.prep:
        prompt = build_prompt(candidates, artifact=args.report or "")
        if not prompt:
            print("未发现疑似前置失败的用例（白名单 0 命中），提示词为空。")
        elif args.prompt_out:
            os.makedirs(os.path.dirname(os.path.abspath(args.prompt_out)) or ".", exist_ok=True)
            with open(args.prompt_out, "w", encoding="utf-8") as f:
                f.write(prompt)
            print("疑似前置失败 %d 条 -> 提示词 %s" % (len(candidates), args.prompt_out))
        else:
            print(prompt)
        return

    existing = _load(args.store, {"dependencies": []})
    records = existing.get("dependencies") or []

    if args.merge:
        facts = _load(args.merge, {})
        rows, clean = verify(facts, candidates, known)
        for row in rows:
            print("  [%s] %s %s" % (row[2], row[0], row[3]))
        records = dedup_merge(records, clean)
        print("机械校验通过 %d 条，累计记录 %d 条" % (len(clean), len(records)))

    if args.manual:
        manual = _load(args.manual, {})
        mrows, mclean = verify(manual, candidates, known)
        records = dedup_merge(records, mclean)
        print("人工登记并入 %d 条" % len(mclean))

    if args.auto:
        rows, clean = verify(auto_facts(candidates), candidates, known)
        for row in rows:
            print("  [%s] %s %s" % (row[2], row[0], row[3]))
        records = dedup_merge(records, clean)
        print("规则直提通过 %d 条，累计记录 %d 条（仍是候选，需 --confirm 才生效）"
              % (len(clean), len(records)))

    if args.confirm or args.confirm_all:
        keys = (args.confirm or "").split(",")
        n = confirm(records, keys=keys, all_flag=args.confirm_all)
        print("已确认 %d 条候选依赖（确认后才会影响归因与生成）" % n)

    if args.store and (args.merge or args.manual or args.auto or args.confirm or args.confirm_all):
        _save(args.store, {
            "source": "runtime",
            "generated_at": args.date or "",
            "report": args.report or "",
            "total": len(records),
            "confirmed": len(confirmed_only(records)),
            "dependencies": records,
        })
        print("记录 -> %s（候选 %d / 已确认 %d）" % (
            args.store, len(records) - len(confirmed_only(records)), len(confirmed_only(records))))

    if args.markdown:
        print(render_markdown(confirmed_only(records), date=args.date or ""), end="")
        return

    if not (args.merge or args.manual or args.auto or args.confirm or args.confirm_all or args.markdown):
        print("疑似前置失败 %d 条：" % len(candidates))
        for c in candidates:
            print("  [%s] %s | 命中 %s | %s" % (c["case_no"], c["name"][:30],
                                               c["hint"], c["response_excerpt"][:70]))


if __name__ == "__main__":
    main()
