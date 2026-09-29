import os
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _corpus():
    """全仓 .md 语料 + schemas：SKILL.md + references/*.md + schemas/*.json。

    只扫 SKILL.md 会大面积误报——路由式正文的正确形态是
    「SKILL.md 指 references，references 指 scripts」，二级回链同样合法。
    """
    parts = [(ROOT / "SKILL.md").read_text(encoding="utf-8")]
    for p in sorted((ROOT / "references").glob("*.md")):
        parts.append(p.read_text(encoding="utf-8"))
    for p in sorted((ROOT / "schemas").glob("*.json")):
        parts.append(p.read_text(encoding="utf-8"))
    return "\n".join(parts)


class TestAssetIntegrity(unittest.TestCase):
    def test_new_references_are_linked_from_skill(self):
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        for name in ("intelligence_policy.md", "attribution_rules.md", "adapter_contract.md",
                     "pattern_candidates.md", "personal_config.md", "traps.md"):
            self.assertIn(name, text)

    def test_examples_have_no_real_credentials(self):
        text = (ROOT / "assets" / "intelligent_config.example.json").read_text(encoding="utf-8")
        self.assertIn("<", text)
        self.assertNotRegex(text, r"hesong\.sojump\.cn|WJX_API_KEY=\w+")

    def test_csv_convention_is_comma_not_tsv(self):
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("输出 TSV", text)

    def test_no_orphan_files(self):
        """每个 references/scripts/adapters/assets/schemas 文件都必须被 .md 语料指向。

        正文下沉到 references 时最容易发生的回归就是"文件搬走了、没人指到它"——
        等于把能力删掉了。任何新增/改名都必须同步回链。
        """
        text = _corpus()
        orphans = []
        for sub, ext in (("references", ".md"), ("scripts", ".py"), ("adapters", ".py"),
                         ("assets", None), ("schemas", ".json")):
            base = ROOT / sub
            for path in sorted(base.rglob("*")):
                if not path.is_file():
                    continue
                if ext and path.suffix != ext:
                    continue
                if path.name.startswith("_") or path.name == "__init__.py":
                    continue
                if path.name in text:
                    continue
                orphans.append(str(path.relative_to(ROOT)).replace(os.sep, "/"))
        self.assertEqual(orphans, [], "以下文件未被任何文档回链（下沉后成孤儿）：%s" % orphans)

    def test_skill_md_has_no_version_annotations(self):
        """正文不留版本标注与改动痕迹——版本只在 frontmatter 维护。"""
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        body = text.split("---", 2)[-1]
        for bad in ("（v3", "（v4", "（新增", "（新）", "已修正", "旧版曾", "新增于"):
            self.assertNotIn(bad, body, "正文残留改动/版本标注：%s" % bad)


if __name__ == "__main__":
    unittest.main()
