# -*- coding: utf-8 -*-
"""页面基线加载器 —— 让 AB（前端-接口口径一致性）从"永远跳过"变成"可自动执行"。

背景：AB 维度是接口侧唯一能触达 UI 还原缺陷的通路（历史缺陷占比约 8.4%，第二高发），
但此前 AB 用例的 `needs=ui_baseline` 会让它**永远被跳过**。本模块把「页面基线」变成
**机器可读的断言输入**：

    有基线 → 接口值 vs 页面值逐字段比对，不一致即记缺陷（AB 维度真正执行）
    无基线 → 记【覆盖率缺口】，维度不会被算作已覆盖，也不会凭空编预期值

比对语义（归一层）：
  * 类型归一 —— 页面抓取值是字符串（"1"）、接口返回原生类型（1），是同一口径值，不得误报；
  * 时间归一 —— 接口 UTC、页面 +8 是正常换算（Baseline.tz_offset，分钟），不得误报；
    页面**没有**做时区转换（真差 8 小时）恰是 AB3 历史缺陷模式，必须被抓到；
  * 双向检查 —— 基线登记了、接口没返回，前端将拿 undefined 渲染空白，同样记缺陷；
  * 点路径 —— 基线字段支持 "data.created_at" 式登记（markdown 登记表的习惯写法）；
  * 维度前缀 —— 基线 dimension 填登记表编号（AB3-01）也能匹配到维度 AB。

只有"表述差异"会被归一层抹平；值不同就是不同——归一层的目的是消误报，不是吞真差异。

基线的三种来源（见 references/ui_baseline.md）：人工页面操作记录 / 浏览器自动化抓取 / 设计稿标注值。

存放位置（按优先级）：
  `--ui-baseline <path>`  →  环境变量 `WJX_UI_BASELINE`  →
  `~/.workbuddy/wjx-api-test/ui_baseline.json`  →  skill 内 `assets/ui_baseline.json`

**真实基线放包外**（含真实账号/业务数据），skill 包内只放 example 占位模板。
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from dataclasses import dataclass
from typing import Any, List, Tuple

DEFAULT_PATHS = (
    os.path.expanduser("~/.workbuddy/wjx-api-test/ui_baseline.json"),
)

_TIME_FMTS = ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M%z",
              "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
              "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M")


@dataclass
class Baseline:
    """一条页面基线：某个接口的某个字段，页面实际显示什么。

    tz_offset —— 页面时间相对接口时间的偏移（分钟）。接口返回 UTC、页面显示 +8 时填 480：
    比对时接口时间 + 偏移后再与页面时间比较（精确到分钟）。"""

    field: str
    page_value: Any = None
    action: str = ""
    dimension: str = "AB"
    aspect: str = "口径"
    source: str = ""
    captured_at: str = ""
    note: str = ""
    tz_offset: int = 0

    def to_dict(self):
        return {
            "field": self.field, "page_value": self.page_value, "action": self.action,
            "dimension": self.dimension, "aspect": self.aspect, "source": self.source,
            "captured_at": self.captured_at, "note": self.note, "tz_offset": self.tz_offset,
        }


def _paths(explicit=None):
    out = []
    if explicit:
        out.append(explicit)
    env = os.environ.get("WJX_UI_BASELINE")
    if env:
        out.append(env)
    out.extend(DEFAULT_PATHS)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out.append(os.path.join(here, "assets", "ui_baseline.json"))
    return out


def load(explicit=None):
    """加载基线。返回 (baselines, source_path)；找不到返回 ([], "")，不抛异常。

    `template: true` 的占位模板会被**主动跳过**（并打印一行说明）：包内 example 模板里的
    "示范页面值"是格式样例、不是实测值，一旦被当作真实基线与接口返回值比对，会把正确实现
    误判成 AB 缺陷——这是"用假预期制造假缺陷"，比没有基线更糟。复制模板自用时删除该标记。

    文件级 `tz_offset_minutes`（或每行 `tz_offset`）会写入每条基线。"""
    for path in _paths(explicit):
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                doc = json.load(f)
        except Exception as _exc:  # noqa: BLE001
            # 基线文件存在但解析失败（损坏 / 非 UTF-8 / 非 JSON）。此前静默 continue，
            # 上层只会报「未登记页面基线」——把「文件坏了」误报成「你没登记」，
            # 排查方向被带偏。出声点名文件与原因。
            print("[ui_baseline] 基线文件解析失败已跳过：%s（%s）" % (path, _exc),
                  file=sys.stderr)
            continue
        if isinstance(doc, dict) and doc.get("template"):
            print("[ui_baseline] 跳过占位模板（template=true）：%s"
                  "（示范值非实测值，复制后请删除 template 标记并替换为实测值）" % path)
            continue
        rows = doc.get("baselines") if isinstance(doc, dict) else doc
        if not isinstance(rows, list):
            continue
        file_tz = int(doc.get("tz_offset_minutes") or 0) if isinstance(doc, dict) else 0
        out = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("field"):
                continue
            out.append(Baseline(
                field=str(row.get("field")),
                page_value=row.get("page_value"),
                action=str(row.get("action") or ""),
                dimension=str(row.get("dimension") or "AB"),
                aspect=str(row.get("aspect") or "口径"),
                source=str(row.get("source") or ""),
                captured_at=str(row.get("captured_at") or ""),
                note=str(row.get("note") or ""),
                tz_offset=int(row.get("tz_offset") or file_tz or 0),
            ))
        if out:
            return out, path
    return [], ""


def _dim_match(bdim, want):
    """维度匹配：登记表编号（AB3-01）也能匹配维度（AB）。"""
    if not bdim or not want:
        return True
    return bdim == str(want) or str(bdim).startswith(str(want))


def _action_ok(b, action):
    return not (action and b.action and b.action != str(action))


def find(baselines, action="", field="", dimension=""):
    """按接口 + 字段定位基线；action/dimension 为空时不参与过滤（维度按前缀匹配）。"""
    for b in baselines:
        if field and b.field != str(field):
            continue
        if not _action_ok(b, action):
            continue
        if not _dim_match(b.dimension, dimension):
            continue
        return b
    return None


def fields_with_baseline(baselines, action=""):
    """列出该接口（或无接口限定）下所有已有基线的字段名。"""
    out = []
    for b in baselines:
        if not _action_ok(b, action):
            continue
        out.append(b.field)
    return out


def _dig(data, path):
    """点路径取值（"a.b.0.c"）。返回 (value, found)。"""
    cur = data
    for part in str(path).split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.lstrip("-").isdigit():
            idx = int(part)
            if -len(cur) <= idx < len(cur):
                cur = cur[idx]
            else:
                return None, False
        else:
            return None, False
    return cur, True


def _get_value(data, name):
    """平键优先，点路径兜底。"""
    if isinstance(data, dict) and name in data:
        return data[name], True
    return _dig(data, name)


def _parse_time(value):
    s = str(value).strip()
    if len(s) < 10:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in _TIME_FMTS:
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _utc_naive(dt):
    if dt.tzinfo is not None:
        return dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return dt


def _norm(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def values_equal(api_value, page_value, tz_offset=0):
    """归一化比较：类型/空白差异抹平；两侧都能解析为时间时按 tz_offset 平移比较（到分钟）。"""
    if api_value is None or page_value is None:
        return api_value == page_value
    if api_value == page_value:
        return True
    if _norm(api_value) == _norm(page_value):
        return True
    a_t, p_t = _parse_time(api_value), _parse_time(page_value)
    if a_t and p_t:
        shifted = _utc_naive(a_t) + datetime.timedelta(minutes=int(tz_offset or 0))
        page = _utc_naive(p_t)
        return shifted.replace(second=0, microsecond=0) == page.replace(second=0, microsecond=0)
    return False


def _locate(baselines, action, name, dimension="AB"):
    """按平键定位基线；失败时按尾段匹配（基线登记为 "data.created_at"、响应键为 created_at）。"""
    b = find(baselines, action=action, field=name, dimension=dimension)
    if b is not None:
        return b
    tail = str(name).split(".")[-1]
    for cand in baselines:
        if cand.field and cand.field.split(".")[-1] == tail \
                and _action_ok(cand, action) and _dim_match(cand.dimension, dimension):
            return cand
    return None


def check_parity(baselines, action, parsed, check_args=None):
    """用基线对接口返回值做 AB 比对。返回 (checked, gaps, findings)。

    checked  —— 真正做了比对的字段数（>0 才算 AB 维度被执行）
    gaps     —— 接口返回了、但没有对应基线的字段名（记缺口，不编预期值）
    findings —— [(field, api_value, page_value, msg)]：值不一致 + 接口缺基线字段（反向）"""
    check_args = check_args or {}
    dim = check_args.get("dimension") or "AB"
    data = (parsed or {}).get("data") if isinstance(parsed, dict) else None
    if not isinstance(data, dict):
        return 0, [], []
    want = check_args.get("fields") or list(data.keys())
    checked, gaps, findings = 0, [], []
    compared = set()
    for name in want:
        b = _locate(baselines, action, name, dim)
        api_v, found = _get_value(data, name)
        if b is None:
            if found:
                gaps.append(name)
            continue
        if not found:
            continue
        checked += 1
        compared.add(name)
        if not values_equal(api_v, b.page_value, b.tz_offset):
            findings.append((name, api_v, b.page_value,
                             "接口返回「%r」，页面显示「%r」（前端-接口口径不一致）"
                             % (api_v, b.page_value)))
    # 反向：基线登记了、接口没返回 —— 前端将拿到 undefined，渲染空白/占位缺失。
    for b in baselines:
        if not _action_ok(b, action) or not _dim_match(b.dimension, dim):
            continue
        if b.field in compared:
            continue
        api_v, found = _get_value(data, b.field)
        if not found:
            findings.append((b.field, "<缺失>", b.page_value,
                             "接口未返回基线字段「%s」（前端将拿到 undefined，渲染空白/占位缺失）"
                             % b.field))
    return checked, gaps, findings
