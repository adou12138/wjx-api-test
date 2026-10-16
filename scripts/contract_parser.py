# -*- coding: utf-8 -*-
"""契约解析器：把接口文档转成统一的 Contract 结构，供自动生成用例使用。

支持三种契约来源（与 doc_review.md 的四种来源对齐，本模块负责机器可读的三种）：
  1) ShowDoc Markdown（本团队主流：含「接口地址」「请求参数格式」「响应参数列表」「请求/响应示例」）
  2) OpenAPI 3.x / Swagger 2.0（JSON 或 YAML；按 operationId 或第一个 path+method 取；
     支持 $ref 解析与嵌套对象/数组递归展开，字段名用点路径表示如 data.items[].name）
  3) 极简 JSON 契约（{"action":..., "request_fields":[...], "response_fields":[...]}）

产出的 Contract 结构（dict）：
  {
    "action": "1000107", "name": "创建AI主页", "endpoint": "https://...",
    "request_fields":  [Field], "response_fields": [Field],
    "request_example": {...},   "response_example": {...},
    "source": "文件路径", "format": "showdoc|openapi|json"
  }
Field:
  {"name","type","desc","required","remark","enum":[(值,释义)],"max_len","min_len",
   "default","forbid":[禁用子串],"semantics":[语义标签]}

关键：备注列（备注/说明）里的约束是生成边界与枚举用例的金矿——
「最长100个字符」「默认值为0；0网页，1海报，2PPT」「不能包含"问卷星"」都会被抽成结构化约束。
"""
import html
import json
import os
import re

REQ_HINTS = ("请求参数", "请求字段", "请求体", "输入参数")
RESP_HINTS = ("响应参数", "响应字段", "返回参数", "输出参数", "响应体")

MAX_RE = re.compile(r"(?:最长|最多|不超过|长度不能超过|长度上限|最大|长度限制|长度)\s*(\d+)")
MIN_RE = re.compile(r"(?:最短|最少|不小于|最小|至少)\s*(\d+)")
DEF_RE = re.compile(r"默认(?:值)?(?:为|是|取)?\s*[:：]?\s*([^\s；;，,。、]+)")
FORBID_RE = re.compile(r"不能包含\s*[“\"'‘']([^”\"'’']+)[”\"'’']")
ENUM_ITEM_RE = re.compile(r"(-?\d+)\s*([一-龥A-Za-z][一-龥A-Za-z0-9_]*)")
# 【W27】字符串枚举形态：「值只能是 A/B/C」「取值：basic,pro」——整数枚举正则抓不到，
# 字符串枚举此前全丢（invalid_enum 不进分母、B-ENUM 不生成）。
STR_ENUM_RE = re.compile(
    r"(?:只能(?:是|为)|取值(?:范围)?(?:为|是|[:：])?|可选值[:：]?)\s*[:：]?\s*"
    r"([A-Za-z][A-Za-z0-9_]*(?:\s*[/,，、]\s*[A-Za-z][A-Za-z0-9_]*){1,})")
# 【W27】required 真值集合扩充：√ * 必须 yes 等非常规标记此前全漏（B-MISS 不生成）。
REQUIRED_TRUTHY = ("是", "Y", "y", "true", "True", "TRUE", "必填", "1",
                   "√", "✓", "*", "＊", "必须", "yes", "Yes", "YES", " required", "Required")
ACTION_RE = re.compile(r"\[\s*(\d{4,})\s*\]")
ACTION_BODY_RE = re.compile(r"(?:动作标识|action)\s*(?:编号|ID|id)?\s*(?:为|是|=|:|：)?\s*[`'\"]?(\d{4,})", re.I)
URL_RE = re.compile(r"https?://[^\s`\"'）)、,，]+")


def _unescape(s):
    return html.unescape(s or "")


def _row_cells(line):
    s = line.strip()
    if not s.startswith("|"):
        return None
    return [c.strip() for c in s.strip("|").split("|")]


def _is_sep(cells):
    """判定 markdown 表格分隔行（如 |:---|:------|---|-|--------|）。
    注意：必需列可能只写一个短横 `-`，故短横数量下限为 1 而非 2。"""
    body = [c for c in cells if c]
    return bool(body) and all(re.fullmatch(r":?-{1,}:?", c.replace(" ", "")) for c in body)


def _extract_tables(text):
    """抽取 markdown 表格，返回 [{"heading","header","rows"}]。"""
    tables, heading, block = [], "", []

    def flush():
        if len(block) >= 2:
            rows = [r for r in block[1:] if not _is_sep(r)]
            tables.append({"heading": heading, "header": block[0], "rows": rows})
        del block[:]

    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            flush()
            heading = s.lstrip("#").strip()
            continue
        cells = _row_cells(s)
        if cells:
            block.append(cells)
        else:
            flush()
    flush()
    return tables


def _col_index(header, keywords, fallback):
    for i, h in enumerate(header):
        for k in keywords:
            if k in h:
                return i
    return fallback if fallback < len(header) else None


def _pick(cells, idx):
    if idx is None or idx >= len(cells):
        return ""
    return cells[idx]


def _parse_constraints(remark, desc=""):
    """从备注/说明里抽结构化约束：最大长度、最小长度、默认值、枚举、禁用子串。"""
    text = "%s %s" % (remark or "", desc or "")
    out = {"max_len": None, "min_len": None, "default": None, "enum": [], "forbid": []}
    m = MAX_RE.search(text)
    if m:
        out["max_len"] = int(m.group(1))
    m = MIN_RE.search(text)
    if m:
        out["min_len"] = int(m.group(1))
    m = DEF_RE.search(text)
    if m:
        out["default"] = m.group(1).strip("。，,;；")
    out["forbid"] = FORBID_RE.findall(text)
    # 枚举：形如「0网页，1海报，2PPT」——至少 2 组且值不重复才认定为枚举
    items = ENUM_ITEM_RE.findall(text)
    if len(items) >= 2:
        vals = [int(v) for v, _ in items]
        if len(set(vals)) == len(vals):
            out["enum"] = [(int(v), lab) for v, lab in items]
    # 【W27】字符串枚举：「只能是 A/B/C」「取值：basic,pro」——整数枚举正则抓不到。
    # 字符串值没有天然释义，释义列留空由人工确认（标「疑似枚举」语义，先入分母）。
    if not out["enum"]:
        m = STR_ENUM_RE.search(text)
        if m:
            vals = [v.strip() for v in re.split(r"[/,，、]", m.group(1)) if v.strip()]
            if len(vals) >= 2 and len(set(vals)) == len(vals):
                out["enum"] = [(v, "") for v in vals]
    return out


def _mk_field(cells, header):
    i_name = _col_index(header, ("参数", "字段", "名称", "name", "key"), 0)
    i_type = _col_index(header, ("类型", "type"), 1)
    i_desc = _col_index(header, ("说明", "描述", "desc", "含义"), 2)
    i_req = _col_index(header, ("必需", "必填", "是否必填", "required"), 3)
    i_remark = _col_index(header, ("备注", "remark", "约束"), 4)
    name = _pick(cells, i_name).strip()
    if not name or name in ("参数", "字段"):
        return None
    remark = _pick(cells, i_remark)
    desc = _pick(cells, i_desc)
    req_raw = _pick(cells, i_req)
    # 【W27】required 真值集合扩充：√/*/*＊/必须/yes 等非常规标记此前全漏
    # （B-MISS 不生成、对账分母少算）。归一后判断，strip 已在 _pick 完成。
    required = req_raw.strip().rstrip("*＊") in tuple(
        t.strip() for t in REQUIRED_TRUTHY) or req_raw.strip() in ("*", "＊") \
        or req_raw.strip() in REQUIRED_TRUTHY
    cons = _parse_constraints(remark, desc)
    return {
        "name": name,
        "type": (_pick(cells, i_type) or "").strip().lower(),
        "desc": desc,
        "required": required,
        "remark": remark,
        "enum": cons["enum"],
        "max_len": cons["max_len"],
        "min_len": cons["min_len"],
        "default": cons["default"],
        "forbid": cons["forbid"],
        "semantics": [],
    }


def _extract_json_blocks(text):
    blocks = []
    for m in re.finditer(r"```(?:json|JSON)?\s*(.*?)```", text, re.S):
        raw = _unescape(m.group(1)).strip()
        if not raw.startswith("{"):
            continue
        try:
            blocks.append(json.loads(raw))
        except Exception:
            continue
    return blocks


def parse_showdoc(text, source="", action=None):
    """解析 ShowDoc 风格的 Markdown 契约。"""
    text = _unescape(text)
    title_m = re.search(r"^#\s+(.+)$", text, re.M)
    title = title_m.group(1).strip() if title_m else ""
    name = re.sub(r"\[\s*\d+\s*\]", "", title).strip()
    name = re.sub(r"^[\d.]+\s*", "", name).strip()
    if not action:
        am = ACTION_RE.search(title) or ACTION_RE.search(os.path.basename(source or ""))
        if not am:
            am = ACTION_BODY_RE.search(text)
        action = am.group(1) if am else ""

    ep_m = re.search(r"接口地址[\s\S]{0,300}?(https?://[^\s`\"'）)、,，]+)", text)
    endpoint = ep_m.group(1) if ep_m else (URL_RE.search(text).group(0) if URL_RE.search(text) else "")

    req_fields, resp_fields = [], []
    for t in _extract_tables(text):
        head = t["heading"]
        is_req = any(h in head for h in REQ_HINTS)
        is_resp = any(h in head for h in RESP_HINTS)
        if not (is_req or is_resp):
            # 无明确标题时按出现顺序兜底：先请求后响应
            is_req = not resp_fields and not req_fields
        for row in t["rows"]:
            f = _mk_field(row, t["header"])
            if not f:
                continue
            (req_fields if is_req and not is_resp else resp_fields).append(f)
    # 兜底：若全被判成请求，且存在两张表，则第二张视为响应
    if resp_fields == [] and len(req_fields) > 0 and text.count("|参数|") >= 2:
        half = len(req_fields) // 2
        resp_fields = req_fields[half:]
        req_fields = req_fields[:half]

    blocks = _extract_json_blocks(text)
    req_ex = blocks[0] if len(blocks) > 0 else {}
    resp_ex = blocks[1] if len(blocks) > 1 else {}
    return {
        "action": action, "name": name, "endpoint": endpoint,
        "request_fields": req_fields, "response_fields": resp_fields,
        "request_example": req_ex if isinstance(req_ex, dict) else {},
        "response_example": resp_ex if isinstance(resp_ex, dict) else {},
        "source": source, "format": "showdoc",
    }


MAX_SCHEMA_DEPTH = 4  # 嵌套展开深度上限，防止病态契约拖垮生成


def _resolve_ref(spec, ref):
    """解析 $ref 指针（如 '#/components/schemas/Pet'、'#/definitions/Pet'）。"""
    if not (isinstance(ref, str) and ref.startswith("#/")):
        return {}
    node = spec
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return {}
    return node if isinstance(node, dict) else {}


def _deref(spec, node):
    """若 node 是 $ref 则解析为实际 schema（单层，调用方负责递归）。

    【W18 兑现兄弟键】Swagger 2.0 常见「{$ref, maxLength, enum, description}」写法：
    旧版只返回目标 schema，$ref 旁的兄弟约束键整体丢失 → 长度/枚举用例不生成、
    对账分母缺项。改为合并：兄弟键优先（局部声明覆盖引用目标）。
    """
    if isinstance(node, dict) and "$ref" in node:
        resolved = _resolve_ref(spec, node["$ref"])
        siblings = {k: v for k, v in node.items() if k != "$ref"}
        if siblings:
            merged = dict(resolved)
            merged.update(siblings)
            return merged
        return resolved
    return node if isinstance(node, dict) else {}


def _merge_alloff(schema, spec, depth=0):
    """【W19】展开 allOf：合并各分支的 properties/required（继承/混入拼装请求体在
    OpenAPI 3.x 极常见，旧版只读顶层 properties → 字段一个都解析不出且无告警）。
    oneOf/anyOf 语义无法机械判定主形态，不展开——留给 TODO 留痕（由 _schema_to_fields
    的 unknown_combo 标记）。"""
    if not isinstance(schema, dict) or depth > MAX_SCHEMA_DEPTH:
        return schema if isinstance(schema, dict) else {}
    branches = schema.get("allOf")
    if not isinstance(branches, list) or not branches:
        return schema
    merged = {k: v for k, v in schema.items() if k != "allOf"}
    props = dict(merged.get("properties") or {})
    req = list(merged.get("required") or [])
    for br in branches:
        br = _deref(spec, br)
        if not isinstance(br, dict):
            continue
        br = _merge_alloff(br, spec, depth + 1)
        for k, v in (br.get("properties") or {}).items():
            props.setdefault(k, v)
        for r in br.get("required") or []:
            if r not in req:
                req.append(r)
    if props:
        merged["properties"] = props
    if req:
        merged["required"] = req
    return merged


def _schema_to_fields(schema, required_names=None, spec=None, prefix="", depth=0, seen=None):
    """把 JSON Schema 展开为字段列表。

    - 递归展开嵌套 object 与 array.items，字段名用点路径（data.items[].name）
    - 解析 $ref（spec 为整个契约文档；缺失时 $ref 按空 schema 处理）；$ref 兄弟键合并
    - allOf 展开合并（oneOf/anyOf 无法机械判定时产 unknown_combo 留痕）
    - depth/seen 双重防环：病态递归契约不会拖垮生成
    """
    spec = spec if isinstance(spec, dict) else {}
    seen = seen or set()
    required_names = required_names or []
    schema = _deref(spec, schema)
    schema = _merge_alloff(schema, spec)
    if depth == 0 and isinstance(schema, dict) and (
            "oneOf" in schema or "anyOf" in schema):
        # oneOf/anyOf：多形态无法机械判定「哪个分支是请求主形态」，静默跳过 = 整类漏测。
        # 顶部留一条 TODO 字段（不带真实字段语义，needs 由消费方识别），点名需人工确认。
        combo = "oneOf" if "oneOf" in schema else "anyOf"
        fields = [{
            "name": ("%s<%s_combo>" % (prefix, combo)) if prefix else "<%s_combo>" % combo,
            "type": "unknown", "desc": "契约使用 %s 多形态，需人工确认主形态后拆分字段" % combo,
            "required": False, "remark": "oneOf/anyOf 未自动展开（无法机械判定主形态）",
            "enum": [], "max_len": None, "min_len": None,
            "default": None, "forbid": [], "semantics": [],
        }]
        return fields
    props = schema.get("properties", {}) if isinstance(schema, dict) else {}
    fields = []
    # 【W18 配套】body schema 本身是 scalar（string/integer + maxLength 等）且带参数名时，
    # 没有 properties 可展开——按单个字段产出（参数名取 body 参数名或 action），
    # 否则标量 body 的约束（最常见：字符串 + maxLength）整体不可见。
    if not props and isinstance(schema, dict) and schema.get("type") in (
            "string", "integer", "number", "boolean") and (
            schema.get("maxLength") or schema.get("minimum") is not None
            or schema.get("maximum") is not None or schema.get("enum")):
        cons = _parse_constraints(str(schema.get("description", "")), "")
        if "enum" in schema:
            cons["enum"] = [(e, str(e)) for e in schema["enum"]]
        if schema.get("maxLength") is not None:
            cons["max_len"] = schema["maxLength"]
        if schema.get("default") is not None:
            cons["default"] = schema["default"]
        fields.append({
            "name": (schema.get("x-body-param-name") or prefix.rstrip(".") or "body"),
            "type": str(schema.get("type", "")),
            "desc": str(schema.get("description", "")), "required": bool(required_names),
            "remark": str(schema.get("description", "")),
            "enum": cons["enum"], "max_len": cons["max_len"], "min_len": cons["min_len"],
            "default": cons["default"], "forbid": cons["forbid"], "semantics": [],
            "nullable": bool(schema.get("nullable")) or "null" in str(schema.get("type", "")).split(","),
        })
        return fields
    for k, v in props.items():
        ref = v.get("$ref") if isinstance(v, dict) else None
        v = _deref(spec, v)
        cons = _parse_constraints(str(v.get("description", "")), "")
        if "enum" in v:
            cons["enum"] = [(e, str(e)) for e in v["enum"]]
        if v.get("maxLength") is not None:
            cons["max_len"] = v["maxLength"]
        if v.get("default") is not None:
            cons["default"] = v["default"]
        full = "%s%s" % (prefix, k)
        fields.append({
            "name": full, "type": str(v.get("type", "")), "desc": str(v.get("description", "")),
            "required": k in required_names, "remark": str(v.get("description", "")),
            "enum": cons["enum"], "max_len": cons["max_len"], "min_len": cons["min_len"],
            "default": cons["default"], "forbid": cons["forbid"], "semantics": [],
            # 【W21】nullable 双口径：OpenAPI 3.x 的 nullable / JSON Schema 的 type 含 "null"。
            # 下游 check_key / case_generator 都读 f.get("nullable")，解析侧不产出
            # 等于「显式 null 处理」整类漏测且分母少算、门禁恒过。
            "nullable": bool(v.get("nullable")) or "null" in str(v.get("type", "")).split(","),
        })
        # 嵌套展开：object 直接递归；array 展开 items 并加 [] 路径标记
        if depth < MAX_SCHEMA_DEPTH:
            if v.get("type") == "object" and isinstance(v.get("properties"), dict):
                guard = ref or id(v)
                if guard not in seen:
                    seen.add(guard)
                    fields += _schema_to_fields(v, v.get("required") or [], spec,
                                                prefix=full + ".", depth=depth + 1, seen=seen)
                    seen.discard(guard)
            elif v.get("type") == "array" and isinstance(v.get("items"), dict):
                items = v["items"]
                guard = items.get("$ref") or id(items)
                if guard not in seen:
                    seen.add(guard)
                    # 【W20】数组元素对象的必填约束必须传递：旧版硬编码 []，
                    # B-MISS 对数组内字段整类不生成、不进对账分母。
                    fields += _schema_to_fields(items, items.get("required") or [], spec,
                                                prefix=full + "[].", depth=depth + 1, seen=seen)
                    seen.discard(guard)
    return fields


def _select_operation(spec, action):
    """按 operationId == action 选 operation，否则取第一个 path+method。"""
    paths = spec.get("paths", {}) if isinstance(spec, dict) else {}
    chosen = None
    for p, methods in paths.items():
        if not isinstance(methods, dict):
            continue
        for m, op in methods.items():
            if m.lower() not in ("get", "post", "put", "delete", "patch"):
                continue
            op = op if isinstance(op, dict) else {}
            if action and str(op.get("operationId", "")) == str(action):
                return p, m, op
            if chosen is None:
                chosen = (p, m, op)
    return chosen


def parse_openapi(spec, action=None):
    """解析 OpenAPI 3.x / Swagger 2.0（dict）。

    - Swagger 2.0：body 参数取 parameters[in=body].schema，地址拼 schemes+host+basePath，
      $ref 指向 definitions
    - OpenAPI 3.x：requestBody.content，地址取 servers[0].url，$ref 指向 components
    - 两者共用 $ref 解析与嵌套递归展开
    """
    sel = _select_operation(spec, action)
    if not sel:
        return None
    path, method, op = sel
    is_v2 = "swagger" in spec

    # 请求字段：path/query/header 参数 + body
    req_fields = []
    params = list((spec.get("paths", {}).get(path, {}) or {}).get("parameters", []) or [])
    params += list(op.get("parameters", []) or [])
    body_schema, body_required = None, []
    for prm in params:
        prm = _deref(spec, prm)
        if not isinstance(prm, dict):
            continue
        if is_v2 and prm.get("in") == "body":
            # 【W18】body 参数名要带下去：scalar body 走 _schema_to_fields 的单字段
            # 分支时用它当字段名。schema 的 $ref 兄弟键由 _deref 合并保留。
            prm_name = str(prm.get("name") or "")
            body_schema = _deref(spec, prm.get("schema", {}))
            if prm_name and isinstance(body_schema, dict) and not body_schema.get("properties"):
                body_schema.setdefault("x-body-param-name", prm_name)
            body_required = (body_schema or {}).get("required", [])
            continue
        # Swagger 2.0 的 formData/query/path/header 参数统一走下方通用字段处理
        sc = _deref(spec, prm.get("schema", {})) or prm
        cons = _parse_constraints(str(prm.get("description", "")), "")
        if "enum" in sc:
            cons["enum"] = [(e, str(e)) for e in sc["enum"]]
        req_fields.append({
            "name": prm.get("name", ""), "type": str(sc.get("type", "")),
            "desc": str(prm.get("description", "")), "required": bool(prm.get("required")),
            "remark": str(prm.get("description", "")), "enum": cons["enum"],
            "max_len": cons["max_len"], "min_len": cons["min_len"], "default": cons["default"],
            "forbid": cons["forbid"], "semantics": [],
            "nullable": bool(sc.get("nullable")) or "null" in str(sc.get("type", "")).split(","),
        })
    if not is_v2:
        body = ((op.get("requestBody", {}) or {}).get("content", {}) or {}).get("application/json", {})
        body_schema = _deref(spec, body.get("schema", {}))
        # 【W19 配套】allOf 的 required 合并发生在 _merge_alloff 里，此处先展开一次，
        # 否则 required_names 取到的是合并前的顶层（allOf 分支里的 required 丢失）。
        body_schema = _merge_alloff(body_schema, spec)
        body_required = (body_schema or {}).get("required", [])
    if body_schema:
        req_fields += _schema_to_fields(body_schema, body_required, spec)

    # 响应字段：200（或第一个 2xx 码；键可能是字符串或整数）
    responses = op.get("responses", {}) or {}
    resp_obj = responses.get("200") or responses.get(200)
    if resp_obj is None:
        for code in sorted(str(c) for c in responses):
            if code.startswith("2"):
                resp_obj = responses.get(code)
                if resp_obj is None and code.isdigit():
                    resp_obj = responses.get(int(code))
                break
    resp_obj = _deref(spec, resp_obj) if isinstance(resp_obj, dict) else {}
    if is_v2:
        resp_schema = resp_obj.get("schema", {})
    else:
        rcontent = (resp_obj.get("content", {}) or {}).get("application/json", {})
        resp_schema = rcontent.get("schema", {})
    resp_fields = _schema_to_fields(_deref(spec, resp_schema),
                                    (_deref(spec, resp_schema) or {}).get("required", []), spec)

    # 地址
    if is_v2:
        scheme = ((spec.get("schemes", []) or ["https"])[0])
        server = "%s://%s%s" % (scheme, spec.get("host", ""), spec.get("basePath", ""))
    else:
        server = ((spec.get("servers", []) or [{}])[0] or {}).get("url", "")
    return {
        "action": action or str(op.get("operationId", "")), "name": str(op.get("summary", "")),
        "endpoint": server + path, "request_fields": req_fields, "response_fields": resp_fields,
        "request_example": {}, "response_example": {},
        "source": "", "format": "openapi", "method": method.upper(),
    }


def _load_yaml(raw, path):
    """解析 YAML 契约；缺 PyYAML 时明确报错（不静默降级到 showdoc 解析）。"""
    try:
        import yaml
    except ImportError:
        raise SystemExit("契约是 YAML 格式但缺少 PyYAML：pip install pyyaml（文件：%s）" % path)
    obj = yaml.safe_load(raw)
    return obj if isinstance(obj, dict) else None


def load_contract(path, action=None):
    """按内容自动分派解析器。path 可为 .md / .json / .yaml / .yml / .txt。"""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()
    stripped = raw.lstrip()
    obj = None
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            obj = json.loads(stripped)
        except Exception:
            obj = None
    elif str(path).lower().endswith((".yaml", ".yml")) or re.match(r"^(openapi|swagger)\s*:", stripped):
        obj = _load_yaml(raw, path)
    if isinstance(obj, dict):
        if "paths" in obj or "openapi" in obj or "swagger" in obj:
            c = parse_openapi(obj, action)
            if c:
                c["source"] = path
                return c
        if "request_fields" in obj or "response_fields" in obj:
            c = {
                "action": str(obj.get("action", action or "")), "name": obj.get("name", ""),
                "endpoint": obj.get("endpoint", ""), "request_fields": obj.get("request_fields", []),
                "response_fields": obj.get("response_fields", []),
                "request_example": obj.get("request_example", {}),
                "response_example": obj.get("response_example", {}),
                # 配套接口声明（交叉验证用）：写进契约即可让生成器自动做真往返/分页校验，
                # 不必每次在命令行重复传 --read-action / --list-action。
                "read_action": obj.get("read_action"), "read_params": obj.get("read_params") or {},
                "list_action": obj.get("list_action"),
                "source": path, "format": "json",
            }
            return c
    return parse_showdoc(raw, source=path, action=action)
