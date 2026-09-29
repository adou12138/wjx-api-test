import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from contract_parser import parse_showdoc


class TestShowdocActionExtraction(unittest.TestCase):
    def test_extracts_action_from_body_description(self):
        text = """# 创建 AI 主页\n\n## 接口地址\nhttps://example.test/openapi/default.aspx\n\n## 请求参数\n| 参数 | 类型 | 必填 | 说明 |\n|---|---|---|---|\n| action | int | 是 | 动作标识 1000107 |\n"""
        contract = parse_showdoc(text)
        self.assertEqual(contract["action"], "1000107")

    def test_extracts_action_assignment_in_body(self):
        text = "# 创建\n\n接口地址 https://example.test/api\n\naction=1000108\n"
        contract = parse_showdoc(text)
        self.assertEqual(contract["action"], "1000108")


if __name__ == "__main__":
    unittest.main()
