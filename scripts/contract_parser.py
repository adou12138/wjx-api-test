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

MAX_RE = re.compile(r"(?:最长|最多|不超过|长度不能超过|长度上限|最大)\s*(\d+)")
MIN_RE = re.compile(r"(?:最短|最少|不小于|最小)\s*(\d+)")
DEF_RE = re.compile(r"默认(?:值)?(?:为|是|取)?\s*[:：]?\s*([^\s；;，,。、]+)")
FORBID_RE = re.compile(r"不能包含\s*[“\"'‘']([^”\"'’']+)[”\"'’']")
ENUM_ITEM_RE = re.compile(r"(-?\d+)\s*([一-龥A-Za-z][一-龥A-Za-z0-9_]*)")
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
    required = req_raw.strip() in ("是", "Y", "y", "true", "True", "必填", "1")
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
    """若 node 是 $ref 则解析为实际 schema（单层，调用方负责递归）。"""
    if isinstance(node, dict) and "$ref" in node:
        return _resolve_ref(spec, node["$ref"])
    return node if isinstance(node, dict) else {}


def _schema_to_fields(schema, required_names=None, spec=None, prefix="", depth=0, seen=None):
    """把 JSON Schema 展开为字段列表。

    - 递归展开嵌套 object 与 array.items，字段名用点路径（data.items[].name）
    - 解析 $ref（spec 为整个契约文档；缺失时 $ref 按空 schema 处理）
    - depth/seen 双重防环：病态递归契约不会拖垮生成
    """
    spec = spec if isinstance(spec, dict) else {}
    seen = seen or set()
    required_names = required_names or []
    schema = _deref(spec, schema)
    props = schema.get("properties", {}) if isinstance(schema, dict) else {}
    fields = []
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
                    fields += _schema_to_fields(items, [], spec,
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
            body_schema = _deref(spec, prm.get("schema", {}))
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
        })
    if not is_v2:
        body = ((op.get("requestBody", {}) or {}).get("content", {}) or {}).get("application/json", {})
        body_schema = _deref(spec, body.get("schema", {}))
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
