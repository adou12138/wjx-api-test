# -*- coding: utf-8 -*-
"""kb_load_contract 单测：从 L5 知识库按 action / 关键词载入契约。

覆盖两路检索（文件系统匹配 / 向量索引兜底）与『接口地址』抽取、
默认回退候选列表。检索只依赖标准库，用临时目录构造知识库结构即可。
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402
import kb_load_contract as KB  # noqa: E402


class TestFilesystemSearch(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = self._td.name
        self.base = os.path.join(self.root, "project", "showdoc_api_docs")
        os.makedirs(os.path.join(self.base, "ver2"), exist_ok=True)

    def tearDown(self):
        self._td.cleanup()

    def _put(self, rel, name, text="# doc\n"):
        d = os.path.join(self.base, rel)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            f.write(text)
        return os.path.join(d, name)

    def test_matches_by_action_token_in_filename(self):
        p = self._put("", "020_2.1.1 获取问卷内容[1000001].md")
        self._put("", "027_2.1.6 创建问卷[1000101].md")
        hits = KB.find_by_filesystem(self.root, "1000001")
        self.assertEqual(hits, [os.path.normpath(p)])

    def test_matches_by_keyword_case_insensitive(self):
        self._put("ver2", "020_AI主页[1000107].md")
        hits = KB.find_by_filesystem(self.root, "ai主页")
        self.assertEqual(len(hits), 1)

    def test_non_markdown_ignored(self):
        self._put("", "020_x[1000001].txt")
        self.assertEqual(KB.find_by_filesystem(self.root, "1000001"), [])

    def test_missing_base_returns_empty(self):
        empty_root = os.path.join(self.root, "nothing_here")
        os.makedirs(empty_root, exist_ok=True)
        self.assertEqual(KB.find_by_filesystem(empty_root, "1000001"), [])


class TestVectorIndexFallback(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = self._td.name
        self.meta = os.path.join(self.root, "project", "_vector_index", "meta.json")
        os.makedirs(os.path.dirname(self.meta), exist_ok=True)

    def tearDown(self):
        self._td.cleanup()

    def _meta(self, items):
        with open(self.meta, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)

    def test_matches_title_and_relpath(self):
        self._meta([
            {"title": "获取问卷内容", "rel_path": "showdoc_api_docs/x[1000001].md",
             "text": "正文"},
            {"title": "无关文档", "rel_path": "showdoc_api_docs/y.md", "text": "正文"},
        ])
        hits = KB.find_by_vector_index(self.root, "问卷")
        self.assertEqual(len(hits), 1)
        self.assertIn("1000001", hits[0][0])

    def test_ignores_non_showdoc_sources(self):
        self._meta([{"title": "问卷 wiki", "rel_path": "other/notes.md", "text": "问卷"}])
        self.assertEqual(KB.find_by_vector_index(self.root, "问卷"), [])

    def test_broken_meta_returns_empty(self):
        with open(self.meta, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(KB.find_by_vector_index(self.root, "问卷"), [])

    def test_non_list_meta_returns_empty(self):
        self._meta({"title": "x"})
        self.assertEqual(KB.find_by_vector_index(self.root, "x"), [])


class TestExtractEndpoints(unittest.TestCase):
    def test_heading_then_url(self):
        text = "# 接口地址\n\nhttps://env.sojump.cn/api/v2\n\n参数说明"
        self.assertEqual(KB.extract_endpoints(text), ["https://env.sojump.cn/api/v2"])

    def test_environment_colon_form(self):
        text = "生产环境：`https://prod.example.com/api`\n测试环境: https://test.example.com/api"
        eps = KB.extract_endpoints(text)
        self.assertIn("https://prod.example.com/api", eps)
        self.assertIn("https://test.example.com/api", eps)

    def test_dedup_preserves_order(self):
        text = "# 请求地址\nhttps://a.example.com/x\nhttps://a.example.com/x\nhttps://b.example.com/y"
        self.assertEqual(KB.extract_endpoints(text),
                         ["https://a.example.com/x", "https://b.example.com/y"])

    def test_empty_text(self):
        self.assertEqual(KB.extract_endpoints(""), [])


class TestDefaultCandidates(unittest.TestCase):
    def test_returns_user_knowledge_paths(self):
        cands = KB.default_kb_candidates()
        self.assertTrue(cands)
        for c in cands:
            self.assertTrue(c.endswith(os.path.join("qa-req2testcase-generator", "user_knowledge"))
                            or c.endswith("user_knowledge"))


if __name__ == "__main__":
    unittest.main()
