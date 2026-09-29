# -*- coding: utf-8 -*-
import json
import re
from pathlib import Path

# 敏感值匹配。注意两处易错点：
#   1. 字符类里的 `\s` 不能多转义成 `\\s`——那样匹配的是"字面反斜杠 + 字母 s"，
#      会把敏感值在第一个 s/S 处截断，导致 `REALSIGNVALUE123` → `***SIGNVALUE123`：
#      脱敏表面上做了，实际把**大半个密钥明文写进了证据包**。
#   2. `(?i)` 对字符类同样生效，所以字符类里不要放裸字母（放了就等于把它连大小写一起排除）。
_SECRET = re.compile(
    r"(?i)([\"']?(?:sign|token|api[_-]?key|cookie|password|secret)"
    r"[\"']?\s*[:=]\s*[\"']?)([^,}\s\"']+)([\"']?)")


def redact(value):
    if isinstance(value, dict):
        return json.dumps({k: ("***" if re.search(r"(?i)(sign|token|api[_-]?key|cookie|password|secret)", str(k)) else redact(v)) for k, v in value.items()}, ensure_ascii=False)
    if isinstance(value, list):
        return json.dumps([redact(v) for v in value], ensure_ascii=False)
    text = str(value)
    return _SECRET.sub(lambda m: "%s***%s" % (m.group(1), m.group(3)), text)


def write_bundle(out_dir, run_id, case_id, payload):
    path = Path(out_dir) / run_id
    path.mkdir(parents=True, exist_ok=True)
    target = path / ("%s.json" % case_id)
    target.write_text(redact(payload), encoding="utf-8")
    return str(target)
