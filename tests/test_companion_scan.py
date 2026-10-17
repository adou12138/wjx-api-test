# -*- coding: utf-8 -*-
"""companion_scan 单测：配套接口解析（read_action / list_action 从哪来）。

覆盖：标题解析、读写分类、对象名打分、契约显式声明解析、同级扫描、
四级来源的优先级（cli > declared > scan > 不自动填），以及「对象名对不上
一律不进候选」这条反假通过的纪律。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402
import companion_scan as C  # noqa: E402


class TestTitleParse(unittest.TestCase):
    def test_split_title_strips_numbering_and_verb(self):
        title, act, raw = C._split_title("027_2.1.6 创建问卷[1000101].md")
        self.assertEqual(act, "1000101")
        self.assertEqual(raw, "创建问卷")
        self.assertEqual(title, "问卷")

    def test_split_title_keeps_object_body(self):
        title, act, raw = C._split_title("020_2.1.1获取问卷内容[1000001].md")
        self.assertEqual(act, "1000001")
        self.assertEqual(raw, "获取问卷内容")
        self.assertEqual(title, "问卷内容")

    def test_object_of_strips_tail_word(self):
        self.assertEqual(C._object_of("问卷内容"), "问卷")
        self.assertEqual(C._object_of("问卷列表"), "问卷")
        self.assertEqual(C._object_of("问卷"), "问卷")

    def test_classify_read_write_and_list(self):
        # 分类按**原始**标题判：'获取' 是读信号，剥掉动词就丢了
        self.assertEqual(C._classify("获取问卷内容"), "read")
        self.assertEqual(C._classify("获取问卷列表"), "list")
        self.assertEqual(C._classify("创建问卷"), None)
        self.assertEqual(C._classify("更新问卷"), None)

    def test_score_prefers_object_equality_and_detail(self):
        hit, sc = C._score("获取问卷内容", "问卷", "问卷")
        self.assertTrue(hit)
        # 对象名相等(4) + 详情类(2)
        self.assertEqual(sc, 6)
        hit, sc = C._score("获取问卷列表", "问卷", "问卷")
        self.assertTrue(hit)
        self.assertEqual(sc, 4)
        hit, sc = C._score("获取答卷详情", "问卷", "答卷")
        self.assertFalse(hit)
        self.assertEqual(sc, 0)


class TestParseDeclared(unittest.TestCase):
    def test_english_keys(self):
        out = C.parse_declared("read_action: 1000108\nlist_action: 1000109")
        self.assertEqual(out["read_action"], "1000108")
        self.assertEqual(out["list_action"], "1000109")

    def test_chinese_labels(self):
        out = C.parse_declared("配套读接口：1000110\n列表接口: 1000111")
        self.assertEqual(out["read_action"], "1000110")
        self.assertEqual(out["list_action"], "1000111")

    def test_read_params_json(self):
        out = C.parse_declared('read_params: {"id": 1, "ver": 2}')
        self.assertEqual(out["read_params"], {"id": 1, "ver": 2})

    def test_empty_returns_none(self):
        out = C.parse_declared("")
        self.assertEqual(out, {"read_action": None, "list_action": None, "read_params": None})


class TestScanSiblings(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.d = self._td.name

    def tearDown(self):
        self._td.cleanup()

    def _write(self, name):
        with open(os.path.join(self.d, name), "w", encoding="utf-8") as f:
            f.write("# %s\n" % name)

    def _fixture(self):
        main = os.path.join(self.d, "020_2.1.6 创建问卷[1000101].md")
        self._write("020_2.1.6 创建问卷[1000101].md")
        self._write("021_2.1.1 获取问卷内容[1000001].md")   # read, 对象名相等+详情
        self._write("022_2.1.2 获取问卷列表[1000002].md")   # list
        self._write("023_2.1.7 更新问卷[1000102].md")       # 写操作 → 排除
        self._write("024_2.1.3 获取答卷详情[1000003].md")   # 对象名不符 → 排除
        return main

    def test_scan_picks_matching_read_and_list(self):
        main = self._fixture()
        res = C.scan_siblings(main, main_action="1000101")
        self.assertEqual([x["action"] for x in res["read"]], ["1000001"])
        self.assertEqual([x["action"] for x in res["list"]], ["1000002"])

    def test_scan_never_guesses_on_object_mismatch(self):
        main = self._fixture()
        res = C.scan_siblings(main, main_action="1000101")
        acts = {x["action"] for k in ("read", "list") for x in res[k]}
        self.assertNotIn("1000103", acts)  # 不存在
        self.assertNotIn("1000102", acts)  # 写操作被排除
        self.assertNotIn("1000003", acts)  # 对象名不符（答卷≠问卷）

    def test_no_dir_returns_empty_buckets(self):
        res = C.scan_siblings("", main_action=None)
        self.assertEqual(res, {"read": [], "list": []})


class TestResolve(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.d = self._td.name
        self._write("020_2.1.6 创建问卷[1000101].md")
        self._write("021_2.1.1 获取问卷内容[1000001].md")
        self.main = os.path.join(self.d, "020_2.1.6 创建问卷[1000101].md")

    def tearDown(self):
        self._td.cleanup()

    def _write(self, name):
        with open(os.path.join(self.d, name), "w", encoding="utf-8") as f:
            f.write("# %s\n" % name)

    def test_cli_wins_over_everything(self):
        res = C.resolve(self.main, contract_text="read_action: 1000108",
                        cli_read="1000109", cli_list="1000110", main_action="1000101")
        self.assertEqual(res["read_action"], "1000109")
        self.assertEqual(res["list_action"], "1000110")
        self.assertEqual(res["source"], "cli")
        self.assertEqual(res["confidence"], "explicit")

    def test_declared_wins_over_scan(self):
        res = C.resolve(self.main, contract_text="read_action: 1000108",
                        main_action="1000101")
        self.assertEqual(res["read_action"], "1000108")
        self.assertEqual(res["source"], "declared")

    def test_scan_used_when_auto(self):
        res = C.resolve(self.main, contract_text="", main_action="1000101", auto=True)
        self.assertEqual(res["read_action"], "1000001")
        self.assertEqual(res["source"], "scan")
        self.assertEqual(res["confidence"], "high")

    def test_auto_false_never_autofills(self):
        res = C.resolve(self.main, contract_text="", main_action="1000101", auto=False)
        self.assertIsNone(res["read_action"])
        self.assertIsNone(res["list_action"])
        self.assertEqual(res["source"], "scan(disabled)")
        # 候选仍在，供人工 --read-action 指定
        self.assertEqual([x["action"] for x in res["candidates"]["read"]], ["1000001"])

    def test_kb_candidates_empty_without_kb_root(self):
        # 无知识库目录时第四级不产出，绝不凭空填 action
        from unittest import mock
        import kb_load_contract as KB
        with mock.patch.object(KB, "default_kb_candidates",
                               return_value=["/nonexistent/kb-root"]):
            buckets = C._kb_companion_candidates("创建问卷", main_action="1000101")
        self.assertEqual(buckets, {"read": [], "list": []})


if __name__ == "__main__":
    unittest.main()
