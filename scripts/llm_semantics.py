# -*- coding: utf-8 -*-
"""语义层接入：让大模型读契约产出「结构化事实」，再由本模块机械校验后并入契约。

为什么放在这一层而不是让大模型逐条生成用例：
  事实层的错误**可以机械校验**——字段名不在契约里、evidence 不是原文子串、
  数值与解析结果不符，全都是字符串与集合运算，不需要模型也不需要人工。
  用例层的错误要等执行才知道。把大模型放在能被校验的位置，它最大的弱点就被管住了。

链路（与 AF 业务场景族同一套 prep/merge 协作模式，无 API key 依赖）：
  1. --prep   读契约，生成抽取提示词（自包含，附带可引用的原文摘录）
  2. 由大模型按提示词产出事实 JSON（本模块不调用任何模型）
  3. --merge  读事实 JSON，机械校验，通过的并入契约，失败的丢弃并留痕

降级：没有大模型产出时，纯规则链路照常工作，行为与接入前完全一致。

校验的边界（必须清楚，否则会高估它）：
  能拦：字段不存在、evidence 缺失或非原文连续子串、数值与契约不符、
        声称的数值/枚举在 evidence 里找不到、alias_of 指向不存在的字段、
        role 不在白名单。
  拦不住：「evidence 与它声称的约束无关」——只要那段话确实在原文里就能通过子串校验。
        所以结论分三态，把「只验了证据」单独标出，不让它冒充「按契约值核验」。
"""
import argparse
import csv
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from contract_parser import load_contract  # noqa: E402

# 校验结论
BLOCKED = "BLOCKED"     # 硬性拦截：字段名不存在、evidence 缺失或非原文、关系引用不存在的字段
MISMATCH = "MISMATCH"   # 与解析结果不符，或声称的值无据：数值漂移、枚举膨胀、必填性写反、无据取值

# 通过分三态。两态不够用：只要「有 evidence」就记 PASS_VERIFIED 的话，大量字段其实
# 没有提交任何可机械比对的约束，却和「数值比对一致」长得一模一样，
# 报告里的「核验通过 N 条」会被读成 N 项都验过值。必须把「只验了证据」单独拆出来。
PASS_VERIFIED = "PASS_VERIFIED"            # 至少一项约束与契约值比对一致
PASS_UNVERIFIED = "PASS_UNVERIFIED"        # 提交了可比对约束但契约无值，无法比对（放行但记账）
PASS_EVIDENCE_ONLY = "PASS_EVIDENCE_ONLY"  # 未提交任何可机械比对的约束，只验了 evidence 是原文子串
PASS_STATES = (PASS_VERIFIED, PASS_UNVERIFIED, PASS_EVIDENCE_ONLY)

# evidence 信息量下限：短于该长度的证据（如「签名」「应用ID」）仍可能是原文子串，
# 但支撑不了任何约束，只能标为弱证据供审计，不当作核验依据。
MIN_EVIDENCE_CHARS = 6

# unknowns 上限：每条 unknowns 都会兑付成一条 manual 留痕用例，无上限会撑长清单、
# 稀释有效信息。超出部分丢弃并留痕，避免「模型话多 = 覆盖好」的错觉。
MAX_UNKNOWNS = 20

# 原文摘录默认上限。超长契约会被截断——截断区内的字段说明模型看不到，
# 必须显式把覆盖范围写进提示词，否则模型会对看不见的字段凭字段名猜。
DEFAULT_EXCERPT_LIMIT = 6000

# 字段角色白名单——不在名单里的角色不并入，避免下游拿到无法消费的值
KNOWN_ROLES = (
    "protocol_control", "tenant_identity", "auth_credential", "operator_identity",
    "display_name", "body_content", "business_enum", "publish_flag", "business_key",
    "short_code", "state", "audit_state", "url_relative", "domain_prefix",
    "embed_url", "alias", "unknown",
)


def _val_in_evidence(evidence, value):
    """约束声称的值能否在 evidence 原文里找到。

    这是「有据」的最低标准，而且完全可机械判定：写了 max_len=100，原文里就该有 100；
    写了 forbid=["问卷星"]，原文里就该有「问卷星」。找不到，说明这个值是模型从字段名
    或行业惯例推出来的——采信它就会产出契约里根本不存在的边界用例或禁用词断言。
    """
    ev = (evidence or "").replace(" ", "").replace(",", "").replace("，", "")
    s = str(value).replace(" ", "").replace(",", "")
    return bool(s) and s in ev


# --------------------------------------------------------------------- prep
def _excerpt(contract_text, limit=DEFAULT_EXCERPT_LIMIT):
    """契约原文摘录——evidence 必须是这里的连续子串。

    返回 (摘录文本, 全文长度)。全文长度非 0 表示发生了截断。

    截断不是小事：字段清单是全量下发的，但原文只给前一段，模型于是「知道字段名、
    看不到字段说明」。它可能拿前段证据去套后段字段——而子串校验拦不住这种错位，
    因为那段话确实是原文子串。所以覆盖范围必须显式写进提示词并留痕。
    """
    t = contract_text.strip()
    if len(t) <= limit:
        return t, 0
    return (t[:limit] + "\n…（原文共 %d 字符，此处截断，只保留前 %d 字符）" % (len(t), limit),
            len(t))


def build_prompt(contract, contract_text, limit=DEFAULT_EXCERPT_LIMIT, shard=None):
    """生成抽取提示词。

    shard=None 单片模式（原文不超限时）；分片模式下 shard=(片文本, 片号, 总片数)，
    片内不截断，字段清单按「字段名出现在本片原文中」过滤——字段清单全量下发
    而摘录只给一片时，模型会拿别片的字段名去本片找依据，子串校验拦不住这种错位。
    """
    if shard is not None:
        excerpt, idx, total = shard
        trunc_warn = (
            "\n> ℹ️ **分片抽取（第 %d/%d 片）**：本片只覆盖原文的一部分。\n"
            "> 只对依据出现在**本片摘录**中的字段产出事实；字段清单里其它字段\n"
            "> 由其它分片负责，本片**一律不要产出**——拿别片字段来本片找依据\n"
            "> 属于错位推断，会被校验拦截或变成假事实。\n" % (idx, total))
        return _prompt_body(contract, excerpt, trunc_warn,
                            _field_lines(contract, _shard_fields(contract, excerpt)))
    excerpt, total = _excerpt(contract_text, limit)
    trunc_warn = ""
    if total:
        trunc_warn = (
            "\n> ⚠️ **摘录已截断**：契约全文 %d 字符，此处只覆盖前 %d 字符。\n"
            "> 若某字段的依据不在上面的摘录里，请把该字段/该点放进 `unknowns` 并注明\n"
            "> 「原文未覆盖」，**不要**凭字段名或惯例推断——截断区外没有可校验的 evidence，\n"
            "> 推断出来的事实会被当作无据而丢弃或降级。\n"
            "> 也可改用分片抽取（prep 对超长契约自动分片），让每个字段都落在某片摘录内。\n"
            % (total, limit))
    return _prompt_body(contract, excerpt, trunc_warn, _field_lines(contract))


def _shard_fields(contract, shard_text):
    """分片对应的字段子集：字段名出现在该片原文中的才归这片。

    字段名在原文任何一片里都找不到的（比如字段清单是独立表格）归第一片——
    它没有可定位的原文段落，提示词会要求模型对它走 unknowns 而不是猜。
    """
    all_names = [f["name"] for key in ("request_fields", "response_fields")
                 for f in (contract.get(key) or [])]
    hit = {n for n in all_names if n and n in shard_text}
    if all_names and not hit:
        return {all_names[0]}
    return hit or set()


def _prompt_body(contract, excerpt, warn, field_lines):
    return """你是接口测试的事实抽取器。请阅读下面的接口契约，抽取**结构化事实**。

## 接口
- 名称：%s
- action：%s
- 地址：%s

## 字段清单（只能使用这些字段名）
%s

## 契约原文摘录（evidence 必须是这里的连续子串，逐字照抄）
```
%s
```
%s
## 你要产出什么
字段的**业务语义事实**——这个字段在业务上是什么、受什么约束、与其他字段什么关系。

## 边界（重要）
- **不要**臆造：字段名只能取自上面的清单；取值只能取自原文摘录。
- **不要**写技术参数校验建议（类型错误、长度越界、枚举越界、必填缺失、空值四态），
  这些由 B 维度自动生成，你写了会被丢弃。
- **不要**猜。契约没说的一律放进 `unknowns`，并标 `must_not_assert: true`。
  猜出来的事实会变成假缺陷，比不写更糟。
- `evidence` 必须**真的支撑**你写的约束：写了 `max_len` 就要有长度数字，
  写了 `forbid` 就要有那个词，写了 `enum` 就要有取值。说不出来的约束就别写。

## 输出格式（严格 JSON，无代码块、无解释）
{
  "fields": {
    "<字段名>": {
      "role": "字段业务角色，取值限定：%s",
      "semantics": ["标签，如 identity/permission/enum/state/html/url/alias"],
      "constraints": {"max_len": 0, "forbid": [], "enum": [[0, "释义"]], "default": "",
                      "alias_of": "", "relative": false, "is_url": false,
                      "fallback_chain": [], "required_role_for_subaccount": []},
      "evidence": "契约原文摘录中的连续子串，逐字照抄",
      "confidence": "explicit（原文写明）或 inferred（由多个原文事实推出）",
      "test_implications": ["据此可设计的用例，写业务意图不写空洞描述"]
    }
  },
  "relations": [
    {"type": "alias|url_compose|fallback_chain", "between": ["字段A", "字段B"],
     "evidence": "原文子串", "confidence": "explicit|inferred"}
  ],
  "unknowns": [
    {"item": "不确定的点", "reason": "契约未声明", "must_not_assert": true}
  ]
}

## 硬性要求
- 每个 field 必须带 evidence，且必须是原文摘录的连续子串——不是子串会被直接拦截。
- relations 也必须带 evidence，且两个字段名都要在字段清单中。
- `alias_of` 只能指向字段清单里真实存在的字段名。
- `unknowns` 请按重要性排序，最多 %d 条——每条都会生成一条人工确认项，
  请只列真正影响断言决策的缺口，不要把「我不知道的一切」都倒进来。
- 只输出 JSON。
""" % (contract.get("name") or "", contract.get("action") or "",
       contract.get("endpoint") or "", field_lines, excerpt, warn,
       " / ".join(KNOWN_ROLES), MAX_UNKNOWNS)


# ------------------------------------------------------------------- verify
def _gt_fields(contract):
    gt = {}
    for key in ("request_fields", "response_fields"):
        for f in contract.get(key) or []:
            gt[f["name"]] = {
                "required": f.get("required"),
                "max_len": f.get("max_len"),
                "enum": f.get("enum") or [],
            }
    return gt


def _alias_direction(rel, facts_fields):
    """alias 关系的方向判定——只认机械可查的依据，判不出返回 None。

    依据一：role 白名单里的 alias 恰好标在其中一侧（另一侧不是）；
    依据二：一侧字段的 constraints.alias_of 已指向另一侧，与关系互证。
    `between` 的数组顺序是模型输出习惯，**不构成依据**——同一个模型
    这次把别名放前、下次放后，按顺序消费就会产出方向相反的等价性断言。
    """
    pair = rel.get("between") or []
    if len(pair) != 2:
        return None
    a, b = pair
    fa, fb = facts_fields.get(a) or {}, facts_fields.get(b) or {}
    ra = (fa.get("role") or "") == "alias"
    rb = (fb.get("role") or "") == "alias"
    if ra and not rb:
        return [a, b]
    if rb and not ra:
        return [b, a]
    ca = (fa.get("constraints") or {}).get("alias_of")
    cb = (fb.get("constraints") or {}).get("alias_of")
    if ca and ca == b:
        return [a, b]
    if cb and cb == a:
        return [b, a]
    return None


def verify(facts, contract, contract_text):
    """机械校验。返回 (rows, clean_facts)。

    clean_facts 只保留校验通过的事实；被拦截的字段整条丢弃并留痕，
    不做「部分保留」——半对半错的事实比没有更危险。
    """
    gt = _gt_fields(contract)
    names = set(gt)
    rows = []
    clean = {"fields": {}, "relations": [], "unknowns": []}

    for fname, f in (facts.get("fields") or {}).items():
        g = gt.get(fname)
        if g is None:
            rows.append([fname, "field_exists", BLOCKED, "字段名不在契约中（臆造字段）"])
            continue

        ev = (f.get("evidence") or "").strip()
        if not ev:
            rows.append([fname, "evidence", BLOCKED, "未给出 evidence"])
            continue
        if ev not in contract_text:
            rows.append([fname, "evidence", BLOCKED,
                         "evidence 不是契约原文子串：%s" % ev[:50]])
            continue

        cons = f.get("constraints") or {}
        bad = []
        # 契约没解析出该约束时无法比对——放行是刻意的（解析器只认「最长N个字符」这类
        # 机械写法，模型可能读懂更复杂的表述），但必须记账，不能与「比对一致」混为一谈。
        waived = []
        checked = 0   # 真正与契约值比对过的约束项数——「核验通过」只能按这个算

        llm_max = cons.get("max_len")
        if llm_max is not None:
            if g["max_len"] is None:
                # 契约没解析出 ≠ 可以放行。这个数值必须能在 evidence 里找到，
                # 否则就是一个凭空出现的数字，后面会变成凭空出现的边界用例。
                if _val_in_evidence(ev, llm_max):
                    waived.append("max_len(evidence 有据)")
                else:
                    bad.append("max_len 抽取 %s，契约未解析出该值且 evidence 中不含该数字（无据）"
                               % llm_max)
            elif llm_max != g["max_len"]:
                bad.append("max_len 抽取 %s，契约 %s" % (llm_max, g["max_len"]))
            else:
                checked += 1

        llm_enum = cons.get("enum")
        if llm_enum is not None:
            if not isinstance(llm_enum, list):
                # 形如 "external_ref:3.8"：不是枚举清单，无值可比对，也无数字可查
                waived.append("enum(非清单，无法比对)")
            elif not g["enum"]:
                vals = [v[0] for v in llm_enum if isinstance(v, (list, tuple))]
                orphans = [v for v in vals if not _val_in_evidence(ev, v)]
                if vals and orphans:
                    bad.append("enum 取值 %s 契约未解析出且 evidence 中不含（无据）" % orphans)
                else:
                    waived.append("enum(evidence 有据)")
            else:
                gt_vals = {v[0] for v in g["enum"] if isinstance(v, (list, tuple))}
                extra = [v for v in llm_enum
                         if not (isinstance(v, (list, tuple)) and v[0] in gt_vals)]
                if extra:
                    bad.append("枚举多出契约外取值：%s"
                               % [v[0] if isinstance(v, (list, tuple)) else v for v in extra])
                else:
                    checked += 1

        if isinstance(f.get("required"), bool) and f["required"] != g["required"]:
            bad.append("required 抽取 %s，契约 %s" % (f["required"], g["required"]))

        # alias_of 驱动「只走别名 / 只走主字段」两条硬断言用例，指向的字段必须真实存在。
        # 只校验别名自己存在是不够的：指向错的字段会产出一条错误的等价性断言。
        alias_of = cons.get("alias_of")
        if alias_of and alias_of not in names:
            bad.append("alias_of 指向不存在的字段：%s" % alias_of)

        role = f.get("role") or ""
        if role and role not in KNOWN_ROLES:
            bad.append("role 不在白名单：%s" % role)

        if bad:
            rows.append([fname, "consistency", MISMATCH, "；".join(bad)])
            continue

        weak = ("（弱证据：evidence 仅 %d 字，无约束信息量）" % len(ev)
                if len(ev) < MIN_EVIDENCE_CHARS else "")
        if waived:
            rows.append([fname, "evidence", PASS_UNVERIFIED,
                         "%s｜未与契约比对的约束：%s%s"
                         % (ev[:40], "、".join(waived),
                            "；已比对 %d 项" % checked if checked else "")])
        elif checked:
            rows.append([fname, "evidence", PASS_VERIFIED, "%s%s" % (ev[:50], weak)])
        else:
            # 没有提交任何可机械比对的约束（只写了 role / semantics 之类），状态只能是
            # 「仅验证据」。与「比对一致」分开，否则「核验通过 N 条」会被读成 N 项都验过值。
            rows.append([fname, "evidence", PASS_EVIDENCE_ONLY,
                         "%s｜无可机械比对的约束（只校验了字段存在与 evidence 是原文子串）%s"
                         % (ev[:50], weak)])
        clean["fields"][fname] = f

    for rel in facts.get("relations") or []:
        between = rel.get("between") or []
        if len(between) < 2 or not all(b in names for b in between):
            rows.append(["relation", "field_exists", BLOCKED,
                         "关系字段不足两个或引用了不存在的字段：%s" % between])
            continue
        ev = (rel.get("evidence") or "").strip()
        # 关系恰恰是最需要证据的一环——它是跨字段推断，字段各自都在清单里，
        # 唯一能表明「两者确实有关系」的就是原文。字段分支缺 evidence 即 BLOCKED，
        # 关系分支不该比它更宽松。
        if not ev:
            rows.append(["relation:%s" % "+".join(between), "evidence", BLOCKED,
                         "关系未给出 evidence"])
            continue
        if ev not in contract_text:
            rows.append(["relation:%s" % "+".join(between), "evidence", BLOCKED,
                         "关系 evidence 非原文子串：%s" % ev[:50]])
            continue
        if (rel.get("type") or "") == "alias":
            direction = _alias_direction(rel, facts.get("fields") or {})
            if not direction:
                # 方向判不出：别名用例断言的是「只走别名也应生效」，方向反了
                # 断言就完全错位。宁可降为留痕让人工确认，也不按数组顺序猜。
                clean["unknowns"].append({
                    "item": "alias 关系方向未定：%s 与 %s（谁是别名谁是主字段需人工确认）"
                            % (between[0], between[1]),
                    "reason": "relations 未标明方向，role 与 alias_of 均无法机械判定",
                    "must_not_assert": True,
                })
                rows.append(["relation:%s" % "+".join(between), "direction", MISMATCH,
                             "alias 关系方向无法机械判定，已转 unknowns 留痕（不按数组顺序猜）"])
                continue
            rel = dict(rel, alias_direction=direction)
        clean["relations"].append(rel)
        if (rel.get("confidence") or "") == "inferred":
            # 推断关系没有原文直接依据，不能记「比对一致」——它只是被采信的推断，
            # 单据性质不同，审计时必须能分开
            rows.append(["relation:%s" % "+".join(between), "evidence", PASS_UNVERIFIED,
                         "%s｜推断关系（confidence=inferred），无原文直接依据，仅作留痕"
                         % ev[:50]])
        else:
            rows.append(["relation:%s" % "+".join(between), "evidence", PASS_VERIFIED, ev[:50]])

    # unknowns 去重 + 上限：每条都会兑付成一条 manual 留痕用例，无上限会撑长清单
    seen_items = set()
    for u in facts.get("unknowns") or []:
        item = (u.get("item") or "").strip()
        if not item or item in seen_items:
            continue
        if len(clean["unknowns"]) >= MAX_UNKNOWNS:
            rows.append(["unknowns", "count", MISMATCH,
                         "unknowns 超过上限 %d 条，其余已丢弃（请只列影响断言决策的缺口）"
                         % MAX_UNKNOWNS])
            break
        seen_items.add(item)
        clean["unknowns"].append(u)

    return rows, clean


# ----------------------------------------------------------------- compare
def _rel_keys(facts):
    return sorted("%s:%s" % (r.get("type"), "+".join(sorted(r.get("between") or [])))
                  for r in (facts.get("relations") or []))


def _cmp_row(name, key, va, vb, explicit):
    if va == vb:
        return [name, key, "一致", "", explicit]
    return [name, key, "分歧", "第一次=%r 第二次=%r" % (va, vb), explicit]


def compare(facts_a, facts_b):
    """双次抽取的稳定性比对：同一契约抽两遍，关键事实是否一致。

    单次抽取无法自证稳定——两次抽取有差异是常态，差异多大、差在哪，
    必须有机械答案而不是「感觉还行」。只比影响断言决策的关键事实
    （role / max_len / enum / alias_of / required / 关系集合 / unknowns 集合）；
    evidence 措辞不同不扣分。`explicit` 列标记两侧都自称 confidence=explicit
    的事实——explicit 层的分歧比 inferred 层严重，应优先人工复核。
    """
    rows = []
    fa, fb = facts_a.get("fields") or {}, facts_b.get("fields") or {}
    for n in sorted(set(fa) | set(fb)):
        a, b = fa.get(n), fb.get(n)
        if a is None or b is None:
            rows.append([n, "field", "仅单次抽到",
                         "仅第一次" if b is None else "仅第二次", ""])
            continue
        explicit = ("是" if a.get("confidence") == "explicit"
                    and b.get("confidence") == "explicit" else "")
        rows.append(_cmp_row(n, "role", a.get("role") or "", b.get("role") or "", explicit))
        ca, cb = a.get("constraints") or {}, b.get("constraints") or {}
        rows.append(_cmp_row(n, "max_len", ca.get("max_len"), cb.get("max_len"), explicit))
        ea = sorted(str(v[0]) for v in (ca.get("enum") or []) if isinstance(v, (list, tuple)))
        eb = sorted(str(v[0]) for v in (cb.get("enum") or []) if isinstance(v, (list, tuple)))
        rows.append(_cmp_row(n, "enum", ea, eb, explicit))
        rows.append(_cmp_row(n, "alias_of", ca.get("alias_of") or "",
                             cb.get("alias_of") or "", explicit))
        rows.append(_cmp_row(n, "required", a.get("required"), b.get("required"), explicit))
    ka, kb = _rel_keys(facts_a), _rel_keys(facts_b)
    rows.append(["relations", "type+between 集合", "一致" if ka == kb else "分歧",
                 "" if ka == kb else "第一次=%s 第二次=%s" % (ka, kb), ""])
    ua = sorted((u.get("item") or "") for u in (facts_a.get("unknowns") or []))
    ub = sorted((u.get("item") or "") for u in (facts_b.get("unknowns") or []))
    rows.append(["unknowns", "item 集合", "一致" if ua == ub else "分歧",
                 "" if ua == ub else "第一次 %d 条 / 第二次 %d 条" % (len(ua), len(ub)), ""])
    return rows


# ------------------------------------------------------------------ combine
def combine_facts(facts_list):
    """多片抽取结果的机械并集（配合分片 prep 使用）。

    字段按字段名归并：分片时每个字段只分给一个片，正常无冲突；
    模型越界输出了别片的字段时，**保留先到的并留痕打印**——同名冲突说明
    两次抽的不一样，静默覆盖会把分歧藏起来。relations / unknowns 直接拼接，
    unknowns 按 item 去重。
    """
    combined = {"fields": {}, "relations": [], "unknowns": []}
    seen_items = set()
    for facts in facts_list:
        for name, f in (facts.get("fields") or {}).items():
            if name in combined["fields"]:
                print("警告：字段 %s 在多片结果中重复，保留先到的（两次抽取不一致，"
                      "建议用 --mode compare 核对）" % name)
                continue
            combined["fields"][name] = f
        combined["relations"].extend(facts.get("relations") or [])
        for u in facts.get("unknowns") or []:
            item = (u.get("item") or "").strip()
            if item and item not in seen_items:
                seen_items.add(item)
                combined["unknowns"].append(u)
    return combined


# ------------------------------------------------------------------ 分片 prep
def _shard_blocks(contract_text, limit):
    """把原文按空行分段、贪心累积成不超过 limit 的片。

    按段落边界切，不把一个说明段落拦腰斩断——段中被切断的证据仍是原文子串，
    但模型读到的是半句话，抽出来的事实质量会明显变差。
    """
    blocks = [b for b in re.split(r"\n\s*\n", contract_text.strip()) if b.strip()]
    shards, cur, size = [], [], 0
    for b in blocks:
        add = len(b) + (2 if cur else 0)
        if cur and size + add > limit:
            shards.append("\n\n".join(cur))
            cur, size = [b], len(b)
        else:
            cur.append(b)
            size += add
    if cur:
        shards.append("\n\n".join(cur))
    return shards


def _field_lines(contract, subset=None):
    out = []
    for key, role in (("request_fields", "请求"), ("response_fields", "响应")):
        for f in contract.get(key) or []:
            if subset is not None and f["name"] not in subset:
                continue
            cons = []
            if f.get("max_len"):
                cons.append("max_len=%s" % f["max_len"])
            if f.get("enum"):
                cons.append("enum=%s" % json.dumps(f["enum"], ensure_ascii=False))
            if f.get("forbid"):
                cons.append("forbid=%s" % json.dumps(f["forbid"], ensure_ascii=False))
            if f.get("default"):
                cons.append("default=%s" % f["default"])
            out.append("  - %s[%s] type=%s required=%s %s | 说明：%s"
                       % (f["name"], role, f.get("type"), f.get("required"),
                          (" ".join(cons)) if cons else "", f.get("desc") or ""))
    return "\n".join(out)


# -------------------------------------------------------------------- merge
def merge(contract, clean):
    """把校验通过的事实并入契约字段，供下游消费。

    并入位置：字段上的 llm_role / llm_constraints / llm_evidence / llm_confidence /
    llm_implications；契约级的 llm_relations / llm_unknowns。
    不覆盖解析器已有的结构化约束——那部分规则解析得很好且零成本。

    **整体替换语义**：本次没有事实的字段会清掉上一轮的 llm_* 残留。若只做部分更新，
    同一份契约被并入两次时会出现「新关系 + 旧字段事实」的混合态，审计时无从分辨
    某条 llm_* 是哪一次留下的。
    """
    fields = clean.get("fields") or {}
    llm_keys = ("llm_role", "llm_constraints", "llm_evidence", "llm_confidence",
                "llm_implications")
    for key in ("request_fields", "response_fields"):
        for f in contract.get(key) or []:
            src = fields.get(f["name"])
            if not src:
                for k in llm_keys:
                    f.pop(k, None)
                continue
            f["llm_role"] = src.get("role") or ""
            f["llm_constraints"] = src.get("constraints") or {}
            f["llm_evidence"] = src.get("evidence") or ""
            f["llm_confidence"] = src.get("confidence") or ""
            f["llm_implications"] = src.get("test_implications") or []
    contract["llm_relations"] = clean.get("relations") or []
    contract["llm_unknowns"] = clean.get("unknowns") or []
    contract["llm_facts_count"] = len(fields)
    # alias 关系的字段级兜底：模型可能只在 relations 里声明别名而不在字段上填
    # alias_of——不兜底的话，这条已判明方向的事实没有任何消费路径，别名用例丢失。
    # 只在字段没写 alias_of 时补（显式声明优先）；字段本身没有事实的，用关系
    # 证据（它已单独过过 evidence 校验）作兑付依据。
    for rel in clean.get("relations") or []:
        if rel.get("type") != "alias":
            continue
        d = rel.get("alias_direction") or []
        if len(d) != 2:
            continue
        an, cn = d
        for key in ("request_fields", "response_fields"):
            for f in contract.get(key) or []:
                if f["name"] != an:
                    continue
                cons = f.get("llm_constraints")
                if cons is None:
                    cons = {}
                    f["llm_constraints"] = cons
                    if not f.get("llm_evidence"):
                        f["llm_evidence"] = (rel.get("evidence") or "").strip()
                if not cons.get("alias_of"):
                    cons["alias_of"] = cn
    return contract


def main():
    ap = argparse.ArgumentParser(
        description="语义层接入：大模型事实抽取的 prep / merge / compare / combine")
    ap.add_argument("--contract", required=True)
    ap.add_argument("--action", default=None)
    ap.add_argument("--mode", required=True, choices=("prep", "merge", "compare", "combine"))
    ap.add_argument("--facts", nargs="+",
                    help="merge：事实 JSON；compare：两份事实 JSON；"
                         "combine：多片事实 JSON（≥2 个）")
    ap.add_argument("--facts2", help="compare 模式：第二次抽取的事实 JSON（也可只用 --facts 传两个）")
    ap.add_argument("--out", required=True,
                    help="prep 写提示词 / merge 写合并后契约 / compare、combine 写产物 JSON")
    ap.add_argument("--report", help="merge/compare 模式：校验结果 CSV")
    ap.add_argument("--excerpt-limit", type=int, default=DEFAULT_EXCERPT_LIMIT,
                    help="提示词里契约原文摘录的字符上限（默认 %d）；超长契约自动按段落"
                         "分片，每片生成独立提示词" % DEFAULT_EXCERPT_LIMIT)
    args = ap.parse_args()

    with open(args.contract, encoding="utf-8") as fh:
        text = fh.read()
    contract = load_contract(args.contract, action=args.action)

    if args.mode == "prep":
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        shards = _shard_blocks(text, args.excerpt_limit)
        if len(shards) <= 1:
            prompt = build_prompt(contract, text, args.excerpt_limit)
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(prompt)
            print("提示词 ->", args.out)
            _, total = _excerpt(text, args.excerpt_limit)
            if total:
                print("摘录覆盖：全文 %d 字符，**仅覆盖前 %d 字符（已截断）**——"
                      "摘录区外的字段没有可校验的 evidence，模型被要求放进 unknowns"
                      % (total, args.excerpt_limit))
            else:
                print("摘录覆盖：全文 %d 字符（未截断）" % len(text.strip()))
            print("下一步：把提示词交给大模型，产出事实 JSON 后用 --mode merge 并入")
        else:
            # 超长契约分片：与其把后半段截掉让模型对看不见的字段猜，
            # 不如按段落切成多片，每片只对落在片内的字段抽取
            base, ext = os.path.splitext(args.out)
            paths = []
            for i, s in enumerate(shards, 1):
                p = "%s_shard%02d%s" % (base, i, ext or ".md")
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write(build_prompt(contract, text, shard=(s, i, len(shards))))
                paths.append(p)
            print("原文 %d 字符超过摘录上限 %d，已按段落分成 %d 片："
                  % (len(text.strip()), args.excerpt_limit, len(shards)))
            for p in paths:
                print("  提示词 ->", p)
            print("下一步：每片单独交给大模型各产出一份事实 JSON，"
                  "用 --mode combine 合并成一份后再 --mode merge")
        print("字段数：%d（请求 %d / 响应 %d）"
              % (len(contract.get("request_fields") or [])
                 + len(contract.get("response_fields") or []),
                 len(contract.get("request_fields") or []),
                 len(contract.get("response_fields") or [])))
        return

    if args.mode == "compare":
        paths = list(args.facts or [])
        if args.facts2:
            paths.append(args.facts2)
        if len(paths) != 2:
            ap.error("compare 模式需要恰好两份事实 JSON（--facts A --facts2 B）")
        with open(paths[0], encoding="utf-8") as fh:
            fa = json.load(fh)
        with open(paths[1], encoding="utf-8") as fh:
            fb = json.load(fh)
        rows = compare(fa, fb)
        judged = [r for r in rows if r[2] in ("一致", "分歧")]
        agree = [r for r in judged if r[2] == "一致"]
        expl = [r for r in judged if r[4] == "是"]
        expl_agree = [r for r in expl if r[2] == "一致"]
        print("关键事实一致性：%d/%d（%.0f%%）" % (len(agree), len(judged),
              100.0 * len(agree) / len(judged) if judged else 100.0))
        if expl:
            print("explicit 层一致性：%d/%d（%s）"
                  % (len(expl_agree), len(expl),
                     "explicit 层分歧应优先人工复核" if len(expl_agree) < len(expl) else "好"))
        only = [r for r in rows if r[2] == "仅单次抽到"]
        if only:
            print("仅单次抽到 %d 项（两次抽取的覆盖范围不同，本身即不稳定信号）："
                  % len(only))
            for r in only:
                print("   %s  %s" % (r[0], r[3]))
        for r in judged:
            if r[2] == "分歧":
                print("   分歧 %-12s %-10s %s%s"
                      % (r[0], r[1], r[3], "【explicit】" if r[4] else ""))
        if args.report:
            with open(args.report, "w", encoding="utf-8-sig", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["对象", "比对项", "结论", "明细", "两侧均为explicit"])
                w.writerows(rows)
            print("比对报告 ->", args.report)
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"agreement": "%d/%d" % (len(agree), len(judged)),
                       "explicit_agreement": ("%d/%d" % (len(expl_agree), len(expl)))
                       if expl else "n/a",
                       "rows": rows}, fh, ensure_ascii=False, indent=2)
        print("比对产物 ->", args.out)
        print("解读：一致性高不代表事实正确（两次一致地错仍可能——校验靠 verify，"
              "不靠稳定）；一致性低则说明该契约的事实抽取不可靠，关键事实需人工复核。")
        return

    if args.mode == "combine":
        paths = args.facts or []
        if len(paths) < 2:
            ap.error("combine 模式需要至少两份事实 JSON（--facts A B C ...）")
        facts_list = []
        for p in paths:
            with open(p, encoding="utf-8") as fh:
                facts_list.append(json.load(fh))
        combined = combine_facts(facts_list)
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(combined, fh, ensure_ascii=False, indent=2)
        print("合并 %d 份分片结果 -> %s（字段 %d / 关系 %d / unknowns %d）"
              % (len(facts_list), args.out, len(combined["fields"]),
                 len(combined["relations"]), len(combined["unknowns"])))
        print("下一步：--mode merge 并入（merge 会重新做全套机械校验，"
              "combine 只做机械并集，不校验）")
        return

    # merge
    if not args.facts:
        ap.error("merge 模式需要 --facts")
    with open(args.facts[0] if isinstance(args.facts, list) else args.facts,
              encoding="utf-8") as fh:
        facts = json.load(fh)

    rows, clean = verify(facts, contract, text)
    merge(contract, clean)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(contract, fh, ensure_ascii=False, indent=2)

    blocked = [r for r in rows if r[2] == BLOCKED]
    mismatch = [r for r in rows if r[2] == MISMATCH]
    print("并入事实 %d 个字段 | 关系 %d 条 | unknowns %d 条"
          % (len(clean["fields"]), len(clean["relations"]), len(clean["unknowns"])))
    verified = [r for r in rows if r[2] == PASS_VERIFIED]
    unverified = [r for r in rows if r[2] == PASS_UNVERIFIED]
    evidence_only = [r for r in rows if r[2] == PASS_EVIDENCE_ONLY]
    print("校验：比对一致 %d | 放行采信 %d | 仅验证据 %d | BLOCKED %d | MISMATCH %d"
          % (len(verified), len(unverified), len(evidence_only), len(blocked), len(mismatch)))
    print("  只有「比对一致」是按契约值核验过的；「仅验证据」只校验了字段存在与"
          " evidence 是原文子串，" "**不代表它的约束有契约依据**。")
    for r in unverified + evidence_only:
        print("   %-18s %-18s %s" % (r[2], r[0], r[3]))
    for r in blocked + mismatch:
        print("   %-9s %-18s %s :: %s" % (r[2], r[0], r[1], r[3]))

    if args.report:
        with open(args.report, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["对象", "校验项", "结论", "明细"])
            w.writerows(rows)
        print("校验报告 ->", args.report)
    print("合并后契约 ->", args.out)
    print("提示：「%s」供审阅。生成用例请用 flow.py --llm-facts <facts.json>——"
          "flow 内部会自行完成同一套校验与并入，两者不必叠加使用。" % args.out)


if __name__ == "__main__":
    main()
