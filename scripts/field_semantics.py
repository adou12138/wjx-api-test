# -*- coding: utf-8 -*-
"""字段语义推断：从「字段名 + 类型 + 备注/说明」推断语义标签，并给出建议断言 helper。

作用：让生成器知道「这个字段该怎么验」，而不是对所有字段一视同仁只验非空。
例如 name=create_time、type=string → 标签 time → 建议 check_datetime_tz / check_timezone；
name=pc_path 含 url/path → 标签 url → 建议 check_download_url（验证拿到的链接真能打开）。

标签 -> 建议断言（与 api_test_runner.py 的 helper 一一对应）：
  time/tz   -> check_datetime_tz / check_timezone
  url       -> check_download_url
  id        -> verify_by_query（存在性）/ 越权矩阵
  enum      -> check_enum_exhaustive
  status    -> 状态机 E 系列
  page/size -> check_pagination
  amount    -> check_currency
  html      -> 内容安全（注入用例）
  count     -> verify_count_delta
  i18n/tz/currency -> 国际版维度（国内版 N/A）
"""
import re

# (标签, 字段名正则, 备注/说明正则)——任一命中即打标签
RULES = [
    ("time", r"(^|_)(time|timestamp|date|datetime|at)$|_time$|_at$|时间|日期", r"时间|日期|时间戳|时区"),
    ("tz", r"timezone|tz$|时区", r"时区|utc|gmt"),
    # 【W48 词边界收紧】url/link/href 加 (^|_)…($|[^a-z0-9_]) 边界：username 不再命中
    # name、surl 不再命中 url；排除表挡「链接编号」类伪 URL。
    ("url", r"(^|_)(url|uri|href|domain)(s)?($|[^a-z0-9_])|link$|地址", r"链接|地址|可访问|域名"),
    # 以下三个是对 url 的「收紧」：命中后由 _OVERRIDES 把泛化的 url 摘掉。
    # 起因：sid 备注「短链接编号」被 url 的「链接」命中，导致拿编号去验 URL 可达性（假缺陷）。
    ("code", r"(^|_)(sid|shortid|short_id|shortlink|short_link|shorturl|short_url|code|token)$|_code$|_token$",
     r"编号|短链接|短码"),
    ("relative_url", r"(^|_)(path|uri|url_path|relative_path)$", r"相对路径|相对地址|需拼接"),
    ("domain", r"(^|_)(domain|host)$", r"域名|主机地址"),
    ("id", r"(^|_)(id|vid|sid|uid|corpid|userid)$|_ids?$", r"编号|唯一标识"),
    ("status", r"status|state|状态", r"状态"),
    ("enum", r"type$|kind$|category$|level$|枚举", r"枚举|取值|类型"),
    # 注意边界：page_type 是「类型枚举」不是翻页参数，故必须整段/整词匹配，不能只含 page
    # 【W48】page_num/page_number 是翻页参数（页码），加入翻页族
    ("page", r"(^|_)(page|page_index|page_no|pageno|page_num|pagenum|page_number|offset|cursor|page_token|next_cursor|nextcursor)$", r"页码|分页|游标"),
    ("size", r"(^|_)(size|page_size|pagesize|limit|per_page|perpage)$", r"每页|条数"),
    ("amount", r"amount|price|fee|money|balance|金额|价格", r"金额|价格|币种|精度"),
    ("currency", r"currency|币种", r"币种|汇率"),
    ("html", r"html|content|template|正文|内容", r"html|内容|正文"),
    # 【W48】count/num 收紧：page_num 是「页码」不是计数（num$ 的词边界排它）；
    # total 单独词匹配
    ("count", r"(^|_)(total|count|quantity)$|_count$|数量|总数", r"总数|数量|条数"),
    ("i18n", r"language|locale|lang|i18n|语言", r"多语言|国际化|语言"),
    ("bool", r"^(is|has|enable|disable|allow)_|publish$|flag$|开关", r"是否|布尔"),
    # 【W48】name/title 词边界：(_name / name$ / title$ / title 前缀) 命中；
    # username/nickname 等常用名称词显式保留（语义确是名称）；filename/codename
    # 这类无下划线复合词不再误判为「名称展示字段」。
    ("name", r"(^|_)(name|title)(s)?$|_name$|_title$|(^|_)title|user_?name|nick_?name|标题|名称", r"标题|名称"),
    ("mobile", r"(^|_)(mobile|phone|tel|telephone)$", r"手机|电话"),
    ("email", r"(^|_)email(s)?$|_email$|(^|_)mail$|邮箱|邮件", r"邮箱|邮件"),
]

# 【W48 排除表】字段名包含这些词时，即使正则命中也摘掉对应标签（伪 URL/伪计数）。
# 【审查修复·去重】count 项此前把 page_num 写了两遍（未复核的疏漏，功能无影响但
# 表明这张表没被逐条看过）；已逐项核对全表，仅此一处重复。
SEMANTIC_EXCLUDES = {
    "url": ("链接编号", "短链接", "链接id", "linkid", "link_id"),
    "count": ("page_num", "pagenum"),
}

# 具体标签压掉泛化标签。左侧更具体，右侧被摘除。
# 为什么必须摘：不摘的话「短链接编号」同时是 code 和 url，下游会拿编号去验可达性，
# 必然失败并报一条假缺陷——误报比漏测更消耗信任。
_OVERRIDES = [
    ("code", "url"),           # 「短链接编号」是编号，不是链接
    ("relative_url", "url"),   # 相对路径是 URL，但不可直连
    ("domain", "url"),         # 域名是拼接前缀，不是完整 URL
    ("tz", "time"),
]

ASSERTIONS = {
    "time": ["check_datetime_tz(返回时间字段)", "check_timezone(时区偏移)"],
    "tz": ["check_datetime_tz(UTC/+08:00/-05:00 三时区各验一遍)"],
    "url": ["check_download_url(链接可访问且在有效期内)"],
    "code": ["存在性与格式校验（是编号，不做 URL 可达性断言）"],
    "relative_url": ["需与域名前缀拼接后再验可达性（不可直连）"],
    "domain": ["作为路径拼接前缀，不单独验可达性"],
    "id": ["verify_by_query(存在性回读)", "越权矩阵(主/子账号×跨企业)"],
    "status": ["状态机 E 系列(合法/非法转换、终态重复操作)"],
    "enum": ["check_enum_exhaustive(文档枚举遍历)", "B3 枚举越界应被拒绝"],
    "page": ["check_pagination(页码/条数边界、深翻页、筛选真生效)"],
    "size": ["check_pagination(size 边界与上限保护)"],
    "amount": ["check_currency(币种精度与上限)"],
    "currency": ["check_currency(多币种)"],
    "html": ["内容安全 G2(注入 script 应被拦截)", "大字段 J1/J4(超长内容不超时)"],
    "count": ["verify_count_delta(计数 +1/-1)"],
    "i18n": ["check_error_i18n(错误信息语言)", "Z 多语言(国内版 N/A)"],
    "bool": ["类型错误用例(传字符串应被拒绝)"],
    "name": ["长度边界(最长 N 与 N+1)", "特殊字符(emoji/换行/前后空格)"],
    "mobile": ["格式错误用例", "空值四态(可选时)"],
    "email": ["格式错误用例", "重复唯一性 AC2"],
}


def infer(field):
    """给单个 Field 打语义标签，返回标签列表（去重、保序）。"""
    name = (field.get("name") or "").lower()
    text = "%s %s" % (field.get("desc", ""), field.get("remark", ""))
    tags = []
    for tag, nre, tre in RULES:
        if re.search(nre, name) or re.search(tre, text):
            # 【W48 排除表】字段名/说明含排除词时摘掉该标签（伪 URL/伪计数）
            excludes = SEMANTIC_EXCLUDES.get(tag, ())
            if any(x in name or x in text.lower() for x in excludes):
                continue
            tags.append(tag)
    if field.get("enum"):
        if "enum" not in tags:
            tags.append("enum")
    if (field.get("type") or "") == "bool" and "bool" not in tags:
        tags.append("bool")
    # 去掉被更具体标签覆盖的泛化标签
    for specific, generic in _OVERRIDES:
        if specific in tags and generic in tags:
            tags.remove(generic)
    return tags


def annotate(contract):
    """给 Contract 的请求/响应字段批量补 semantics。"""
    for key in ("request_fields", "response_fields"):
        for f in contract.get(key, []) or []:
            f["semantics"] = infer(f)
    return contract


def is_absolute_url(field):
    """该字段是否是「可直接验可达性的完整 URL」。

    相对路径、域名前缀、编号三者都不是——拿它们直连必然失败，会报一条假缺陷。
    下游（pattern_engine S4 等）统一走这里，不要各自用 "url" in semantics 判断。
    """
    tags = field.get("semantics") or infer(field)
    return "url" in tags and not any(
        t in tags for t in ("relative_url", "domain", "code"))


def assertions_for(tags):
    """语义标签 -> 建议断言列表。"""
    out = []
    for t in tags:
        for a in ASSERTIONS.get(t, []):
            if a not in out:
                out.append(a)
    return out


def suggest_for_contract(contract):
    """返回 {字段名: 建议断言列表}，供生成器与报告「修复建议」使用。"""
    out = {}
    for key in ("request_fields", "response_fields"):
        for f in contract.get(key, []) or []:
            sug = assertions_for(f.get("semantics") or infer(f))
            if sug:
                out.setdefault(f["name"], sug)
    return out


def unique_key_field(contract):
    """找契约里**显式声明**的业务唯一键字段；找不到返回 None。

    只认三种显式信号，刻意不用 name/email 等语义标签猜：
      ① 契约里写了 `unique: true`（最明确的口径）；
      ② 字段名含 unique/uniq（如 unique_code / uniq_no）；
      ③ 备注写明「唯一」「不可重复」「不得重复」「重复校验」
         （"唯一标识" 除外——那是 id 字段的描述，不是唯一性约束）。

    为什么不用猜：AC01 断言的是「重复提交必须被拒绝」。在允许多个同名对象的接口上，
    拿 name 字段当唯一键会造出一条 severity=高 的假缺陷——误报比漏测更消耗信任。
    无法判定时改为产出「需人工指明唯一键」的占位，由交付前的待补清单兜住。
    """
    for f in (contract.get("request_fields") or []):
        name = (f.get("name") or "").lower()
        doc = "%s %s" % (f.get("desc", ""), f.get("remark", ""))
        if f.get("unique") is True:
            return f
        if "unique" in name or "uniq" in name:
            return f
        if ("唯一" in doc and "唯一标识" not in doc) or "不可重复" in doc \
                or "不得重复" in doc or "重复校验" in doc:
            return f
    return None


# ------------------------------------------------------------------ 字段族
# 同族字段在服务端通常走同一套校验分支：限量砍用例时按「族」保代表，
# 比按位置取前 N 更不容易把一整类分支整个砍掉。
_COMMON_STEM_SUFFIXES = ("_id", "_no", "_num", "_type", "_key", "_name", "_url", "_time")


def field_family(f):
    """字段族键——判定依据按可靠度排序：

    ① 语义层给的 llm_role（最明确的业务角色）；
    ② 解析出的语义标签（url / code / identity 等）；
    ③ 去掉常见后缀的字段名词干（item_id / item_no 同族）。

    三个依据都取不到时退回字段名本身——即每个字段自成一族，行为与不限族一致。
    """
    role = (f.get("llm_role") or "").strip()
    if role:
        return "role:" + role
    sem = sorted(s for s in (f.get("semantics") or []) if s and s != "unknown")
    if sem:
        return "sem:" + sem[0]
    n = (f.get("name") or "").lower()
    for suf in _COMMON_STEM_SUFFIXES:
        if n.endswith(suf) and len(n) > len(suf):
            return "stem:" + n[: -len(suf)]
    return "name:" + n


def pick_by_family(fields, n, warnings=None):
    """字段族限额选择：先让每个族至少一个代表入选，配额有余再按原序补齐。

    直接取前 N 的问题：排在前面的族往往字段形态相近（比如一排 id），
    砍掉的是排在后面的整个族——族内共享校验分支，砍掉一族等于一类分支零覆盖。
    族按规模降序拿代表：砍掉大族的损失比砍掉单字段族大。

    【W28 退化可审计】字段无 stem/语义标签时 field_family 返回 "name:<字段名>"
    （每字段自成一族），"每族一个代表"退化成"按原序取前 N"——docstring 声称要
    避免的行为。

    【审查修复·纯函数】本函数此前直接 sys.stderr.write 告警：它是个纯计算
    （输入字段与配额，输出选中的字段），调用方无法控制输出、去重、归档，
    安静模式（只想要结果）下也照样污染日志。改为：**显式传入收集器**
    ``warnings``（list）时把退化告警写进该 list，返回值仍是选中的字段。
    不传收集器则完全静默——是否出声由调用方决定。
    需要落日志的调用方在自己的输出层统一 emit（见 pattern_engine.run /
    case_generator 生成入口），那里本就有 stderr 通道与报告产物。
    """
    fields = list(fields)
    if len(fields) <= n:
        return fields
    fams = {}
    for i, f in enumerate(fields):
        fams.setdefault(field_family(f), []).append(i)
    ranked = sorted(fams.values(), key=lambda idxs: (-len(idxs), idxs[0]))
    picked = set()
    for idxs in ranked:
        if len(picked) >= n:
            break
        picked.add(idxs[0])
    for i in range(len(fields)):
        if len(picked) >= n:
            break
        picked.add(i)
    dropped = [fields[i] for i in sorted(set(range(len(fields))) - picked)]
    # 【W28】退化检测：name: 族（无任何语义线索）超过半数且确有丢弃 → 告警
    name_fam_count = sum(1 for k in fams if k.startswith("name:"))
    if warnings is not None and name_fam_count >= max(1, len(fams) // 2) and dropped:
        warnings.append(
            "pick_by_family 退化：%d/%d 个字段无语义标签（name: 自成一族），"
            "限额 %d 退化为按位置取前 N；被砍字段：%s（请人工确认或补语义标签）"
            % (name_fam_count, len(fams), n,
               ",".join(str(d.get("name")) for d in dropped[:10])))
    return [fields[i] for i in sorted(picked)]
