# -*- coding: utf-8 -*-
"""配套接口解析：交叉验证（framework §9.4.2）的 read_action / list_action 从哪来。

背景：C1「写入真值回读」与「创建后验列表出现 / 计数 +1」都要求**不只看主接口响应**，
而要调一个独立接口做交叉验证。但生成器过去从不填 read_action / list_action，
只在提示里写「人工补」——能力在 case_runner 里已经实现，默认却不启用，深度白掉一档。

三条来源，优先级从高到低：
  ① 命令行显式指定 --read-action / --list-action（人已经确认过，最可信）；
  ② 契约文档显式声明（`read_action: 1000108`、`配套读接口`、`列表接口` 等）；
  ③ 同级目录扫描：同一契约目录下的兄弟文档，按「业务对象名匹配 + 动词分类」挑。

为什么不无脑猜：猜错会调一个不存在的 action，把 ENV 失败伪装成「往返已验证」，
比不验更糟。所以扫描只在**业务对象名对得上**时才采用，并把候选清单与置信度
写进产物供审计；执行期还有一层兜底——读接口返回结构与主接口提交字段交集为空时
点名降级（见 case_runner._check_roundtrip 的字段交集探测）。
"""
import os
import re

# 标题里的动词/修饰词：抽出「业务对象名」时先从两端剥掉，剩下的才是对象本体。
# 例：'2.1.6 创建问卷' -> '问卷'；'2.1.1 获取问卷内容' -> '问卷内容'
_LEAD_VERBS = ("创建", "新增", "添加", "更新", "修改", "编辑", "删除", "清空", "导出", "发布",
               "提交", "复制", "移动", "导入", "上传", "下载", "订阅", "回调", "批量",
               "获取", "查询", "读取", "查看", "搜索", "统计", "计数")
_TAIL_WORDS = ("详情", "内容", "信息", "列表", "清单", "分页", "统计", "计数", "总数",
               "状态", "设置", "内容")

# 排除词：命中即不是配套**读**接口（它们本身是写操作或副作用操作）
_EXCLUDE_VERBS = ("创建", "新增", "添加", "更新", "修改", "编辑", "删除", "清空", "导出",
                  "发布", "提交", "复制", "移动", "导入", "上传", "下载", "订阅", "回调",
                  "绑定", "解绑", "授权", "撤销", "发送", "推送")
_LIST_WORDS = ("列表", "清单", "分页", "统计", "计数", "总数")
_READ_WORDS = ("获取", "查询", "读取", "查看", "详情", "内容", "信息", "搜索")
# 详情类优先于泛查询类：'获取问卷内容' 比 '获取问卷列表' 更适合当 read_action
_DETAIL_WORDS = ("详情", "内容", "信息")

_ACT_RE = re.compile(r"\[(\d{4,})\]\s*$")
# 只吃开头的「编号_版本号」（数字/点/下划线），不能用 \w——\w 会连带吃掉中文标题，
# 于是 '020_2.1.1获取问卷内容' 会被削成空串，真正的配套接口反而进不了候选。
_HEAD_RE = re.compile(r"^\s*\d[\d._]*\s*")


def _split_title(fname):
    """文件名 -> (去掉动词的标题, action, 原始标题)。

    '027_2.1.6 创建问卷[1000101].md'  -> ('问卷', '1000101', '创建问卷')
    '020_2.1.1获取问卷内容[1000001].md' -> ('问卷内容', '1000001', '获取问卷内容')
    原始标题单独保留：分类要按原文判（'获取' 是读接口的信号），
    只有抽业务对象名时才剥掉动词。
    """
    stem = os.path.splitext(os.path.basename(fname))[0]
    m = _ACT_RE.search(stem)
    action = m.group(1) if m else ""
    raw = _HEAD_RE.sub("", _ACT_RE.sub("", stem)).strip()
    title = raw
    for v in _LEAD_VERBS:
        if title.startswith(v):
            title = title[len(v):].strip()
            break
    return title, action, raw


def _object_of(title):
    """标题 -> 业务对象名（再剥一层尾部词，用于对象名相等判定）。"""
    t = title
    for w in _TAIL_WORDS:
        if t.endswith(w):
            t = t[: -len(w)].strip()
            break
    return t


def _classify(title):
    """标题 -> 'list' / 'read' / None（None = 不是配套读接口）。"""
    if any(v in title for v in _EXCLUDE_VERBS):
        return None
    if any(w in title for w in _LIST_WORDS):
        return "list"
    if any(w in title for w in _READ_WORDS):
        return "read"
    return None


def _score(title, obj_main, obj_cand):
    """候选打分：对象名匹配优先，详情类次之。返回 (是否匹配, 分数)。"""
    if not obj_main or not obj_cand:
        return False, 0
    if obj_main == obj_cand:
        hit, score = True, 4
    elif obj_main in obj_cand or obj_cand in obj_main:
        hit, score = True, 2
    else:
        return False, 0
    if any(w in title for w in _DETAIL_WORDS):
        score += 2
    return hit, score


def parse_declared(text):
    """从契约正文解析显式声明的配套接口。命中即最高置信（人写下的，不是猜的）。"""
    out = {"read_action": None, "list_action": None, "read_params": None}
    if not text:
        return out
    pats = {
        "read_action": r"(?:read_action|配套读接口|配套查询接口|详情接口|读取接口|查询接口|获取接口)\s*[:：]\s*[\"']?(\d{4,})",
        "list_action": r"(?:list_action|配套列表接口|列表接口|分页接口|统计接口|计数接口)\s*[:：]\s*[\"']?(\d{4,})",
    }
    for key, pat in pats.items():
        m = re.search(pat, text)
        if m:
            out[key] = m.group(1)
    m = re.search(r"read_params\s*[:：]\s*(\{[^}]*\})", text)
    if m:
        try:
            import json
            out["read_params"] = json.loads(m.group(1))
        except Exception:
            out["read_params"] = None
    return out


def scan_siblings(contract_path, main_action=None):
    """扫描契约文件同级目录下的兄弟文档，返回 {read: [...], list: [...]} 候选。

    每个候选形如 {"action": "1000001", "title": "获取问卷内容", "score": 6, "file": "..."}。
    业务对象名对不上的一律不进候选——宁可空着让人指定，也不猜一个出来假装验过。
    """
    if not contract_path:
        return {"read": [], "list": []}
    d = os.path.dirname(os.path.abspath(contract_path))
    if not os.path.isdir(d):
        return {"read": [], "list": []}
    main_title, main_act, _raw_main = _split_title(contract_path)
    obj_main = _object_of(main_title)
    main_action = main_action or main_act
    buckets = {"read": [], "list": []}
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return buckets
    for n in names:
        if not n.lower().endswith((".md", ".markdown")):
            continue
        if os.path.abspath(os.path.join(d, n)) == os.path.abspath(contract_path):
            continue
        title, act, raw = _split_title(n)
        if not act or act == main_action:
            continue
        # 分类按原始标题判（'获取' 是读接口信号，剥掉动词后就丢了）
        kind = _classify(raw)
        if not kind:
            continue
        # 对象名与详情加分：对象名用剥掉动词的标题，详情类关键词看原始标题
        ok, sc = _score(raw, obj_main, _object_of(title))
        if not ok:
            continue
        buckets[kind].append({"action": act, "title": raw, "score": sc, "file": n})
    for k in buckets:
        buckets[k].sort(key=lambda x: -x["score"])
    return buckets


def resolve(contract_path, contract_text=None, cli_read=None, cli_list=None,
            cli_read_params=None, main_action=None, auto=True):
    """解析配套接口，返回可写进产物的结构化结果。

    auto=False（或对象名对不上）时只出候选清单不自动填，让人用 --read-action 指定。
    """
    declared = parse_declared(contract_text or "")
    sib = scan_siblings(contract_path, main_action=main_action)
    res = {
        "read_action": None, "list_action": None, "read_params": {},
        "source": "none", "confidence": "none",
        "candidates": {"read": sib["read"], "list": sib["list"]},
        "declared": declared,
    }
    rp = cli_read_params or declared.get("read_params") or {}
    res["read_params"] = rp

    # ① 命令行显式指定：最高优先级
    if cli_read or cli_list:
        res["read_action"] = cli_read
        res["list_action"] = cli_list
        res["source"] = "cli"
        res["confidence"] = "explicit"
        return res

    # ② 契约显式声明
    if declared.get("read_action") or declared.get("list_action"):
        res["read_action"] = declared.get("read_action")
        res["list_action"] = declared.get("list_action")
        res["source"] = "declared"
        res["confidence"] = "explicit"
        return res

    # ③ 同级扫描：仅高/中置信且开启 auto 时采用
    if not auto:
        res["source"] = "scan(disabled)"
        return res
    rd = sib["read"]
    ls = sib["list"]
    if rd:
        res["read_action"] = rd[0]["action"]
        res["read_score"] = rd[0]["score"]
    if ls:
        res["list_action"] = ls[0]["action"]
        res["list_score"] = ls[0]["score"]
    if res["read_action"] or res["list_action"]:
        res["source"] = "scan"
        # 同类中只有唯一候选 或 得分达到「对象名相等 + 详情类」→ 高置信
        best = max([x["score"] for x in rd[:1]] or [0])
        res["confidence"] = "high" if (best >= 4 or len(rd) <= 1) else "medium"
    return res
