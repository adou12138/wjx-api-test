# -*- coding: utf-8 -*-
"""状态字段覆盖（v4.26.0）：E02/E04 覆盖所有状态字段，而非只第一个。

锁住：① 多状态字段各自生成 E02/E04；② 单状态字段 ID 保持 E02/E04（不加后缀，不破坏既有）；
③ E04 用字段**自己**的枚举（修掉旧版请求字段名配响应枚举的错配）；④ 无枚举状态字段仍点名 TODO；
⑤ 受 STATE_MAX_FIELDS 上限约束，防爆炸。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

import pattern_engine  # noqa: E402


def _ctx():
    return {"base": {}, "read_action": None, "read_params": {}}


def _sf(name, enum=None):
    f = {"name": name, "type": "int", "semantics": ["status"]}
    if enum is not None:
        f["enum"] = enum
    return f


def _gen(fields):
    return pattern_engine.s3_gen({"request_fields": fields, "response_fields": []}, _ctx())


class TestStateCoverage(unittest.TestCase):
    def test_multi_status_fields_covered(self):
        fields = [_sf("state", [(0, "草稿"), (1, "发布")]),
                  _sf("audit", [(0, "待审"), (1, "通过")])]
        ids = [c["id"] for c in _gen(fields)]
        for want in ("E02", "E02-audit", "E04", "E04-audit"):
            self.assertIn(want, ids, "多状态字段应各自覆盖：缺 %s" % want)

    def test_single_status_field_ids_unchanged(self):
        ids = [c["id"] for c in _gen([_sf("state", [(0, "a"), (1, "b")])])]
        self.assertIn("E02", ids)
        self.assertIn("E04", ids)
        self.assertNotIn("E02-state", ids, "单字段不应加后缀（保持既有 ID 稳定）")
        self.assertNotIn("E04-state", ids)

    def test_e04_uses_own_enum(self):
        fields = [_sf("state", [(0, "a"), (1, "b")]),
                  _sf("audit", [(5, "x"), (9, "y")])]
        cases = _gen(fields)
        e04_audit = [c for c in cases if c["id"] == "E04-audit"][0]
        self.assertEqual(e04_audit["check_args"]["field"], "audit")
        self.assertEqual(e04_audit["check_args"]["values"], [5, 9],
                         "E04 必须用字段自己的枚举，不错配")

    def test_status_field_no_enum_todo(self):
        ids = [c["id"] for c in _gen([_sf("state")])]  # 无枚举
        self.assertIn("TODO-E-状态枚举", ids)

    def test_bounded(self):
        fields = [_sf("s%d" % i, [(0, "a"), (1, "b")]) for i in range(10)]
        cases = _gen(fields)
        e04 = [c for c in cases if str(c["id"]).startswith("E04")]
        e02 = [c for c in cases if str(c["id"]).startswith("E02")]
        self.assertLessEqual(len(e04), pattern_engine.STATE_MAX_FIELDS)
        self.assertLessEqual(len(e02), pattern_engine.STATE_MAX_FIELDS)

    def test_response_only_status_no_e04(self):
        """响应独有的状态字段设不进去，不应生成 E04（拿它当 target 是空操作）——v4.26.0 审查修复。"""
        contract = {"request_fields": [{"name": "title", "type": "string", "semantics": ["name"]}],
                    "response_fields": [_sf("audit", [(0, "a"), (1, "b")])]}
        cases = pattern_engine.s3_gen(contract, _ctx())
        ids = [str(c["id"]) for c in cases]
        self.assertEqual([i for i in ids if i.startswith("E04")], [],
                         "响应独有状态字段不可设置，不应生成 E04")
        todo = [c for c in cases if c["id"] == "TODO-E-状态枚举"]
        self.assertEqual(len(todo), 1)
        self.assertIn("仅在响应侧", todo[0]["name"], "TODO 原因应说清'仅响应侧设不进去'")


if __name__ == "__main__":
    unittest.main()
