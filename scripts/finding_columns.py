# -*- coding: utf-8 -*-
"""台账列口径的单一来源：深度流程（api_test_runner）与冒烟（smoke_runner）共用。

改列名 / 增删列只改 `assets/finding_columns.json`；本模块只负责读取与回退，
不 import api_test_runner 等重量级模块——避免在导入期连带加载个人配置，
也让冒烟链路在 api_test_runner 不可用时仍取到**同一份**列口径。
"""

import json
import os

# assets 缺失 / 损坏时的回退列。必须与 assets/finding_columns.json 的表头一致
# （tests/test_artifact_readback.py 锁定 DEFAULT_FINDING_COLS == FINDING_CSV_COLS）。
DEFAULT_FINDING_COLS = ["编号", "类型", "严重程度", "状态", "维度", "问题描述", "请求Body",
                        "预期结果", "实际结果", "置信度", "HTTP状态码", "请求ID", "TraceID",
                        "证据路径", "归因", "复测状态"]

# finding 的英文键 ↔ 台账表头中文列名。finding_row 按 FINDING_CSV_COLS 取值，
# 增删列只需改 assets/finding_columns.json 与这张映射表，不用改渲染逻辑。
FIELD_KEYS = {
    "编号": "id", "类型": "kind", "严重程度": "severity", "状态": "status",
    "维度": "dimension", "问题描述": "desc", "请求Body": "request",
    "预期结果": "expected", "实际结果": "actual", "置信度": "confidence",
    "HTTP状态码": "http_code", "请求ID": "request_id", "TraceID": "trace_id",
    "证据路径": "evidence_path", "归因": "attribution", "复测状态": "retest_status",
}


def load_finding_cols_from(path):
    """从指定路径读台账列名，失败回退内置默认列并返回告警文案（告警为 None 表示正常）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            cols = [c["header"] for c in json.load(f)["finding_columns"]]
        if not cols or not all(isinstance(c, str) and c for c in cols):
            raise ValueError("列定义为空或含非法表头：%r" % (cols,))
        return cols, None
    except Exception as e:
        return list(DEFAULT_FINDING_COLS), \
            "台账列定义读取失败（%s: %s），已回退内置默认列；改列名请编辑 assets/finding_columns.json" \
            % (type(e).__name__, e)


def load_finding_cols():
    """按包内固定路径（../assets/finding_columns.json）读取列口径，返回 (cols, warn)。"""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "assets", "finding_columns.json")
    return load_finding_cols_from(path)
