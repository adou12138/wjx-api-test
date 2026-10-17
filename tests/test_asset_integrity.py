import fnmatch
import os
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _gitignored(rel_path):
    """按 .gitignore 语义（含 `!` 取反、后规则优先）判断包内相对路径是否被忽略。

    只覆盖本仓实际用到的简单模式：无 `/` 的按 basename 匹配，含 `/` 的按整路径匹配。
    用于把「文档指导放包内的真实值实例名必须被忽略」变成可回归的护栏，
    不依赖机器上是否安装了 git。
    """
    ignored = False
    for raw in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        pat = line[1:] if negate else line
        if pat.endswith("/"):
            p = pat.rstrip("/")
            hit = rel_path == p or rel_path.startswith(p + "/")
        elif "/" in pat:
            hit = fnmatch.fnmatch(rel_path, pat)
        else:
            hit = fnmatch.fnmatch(Path(rel_path).name, pat)
        if hit:
            ignored = not negate
    return ignored


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
        """所有占位模板/参考实例都必须零真实凭据：不命中真实凭据特征、无真实子域名。

        逐一枚举而非只查一个 —— 新增模板时最容易被漏掉的护栏就是这条。
        """
        templates = [
            "personal_config.example.json",
            "intelligent_config.example.json",
            "smoke_config.example.json",
            "smoke_config_wjx.json",
            "ui_baseline.example.json",
        ]
        for name in templates:
            text = (ROOT / "assets" / name).read_text(encoding="utf-8")
            self.assertNotRegex(text, r"hesong\.sojump\.cn|WJX_API_KEY=\w+",
                                "%s 命中已知真实域名/凭据" % name)
            # 真实子域名不得出现：文档级示例域名一律以 your- 前缀占位（your-env1 等）
            hosts = re.findall(r"https://([a-z0-9-]+)\.(?:sojump\.(?:cn|com)|wjx\.cn)", text)
            bad = [h for h in hosts if not h.startswith("your-")]
            self.assertEqual(bad, [], "%s 含疑似真实域名：%s" % (name, bad))
        # 配置类模板必须以 <...> 尖括号占位承载个人参数（ui_baseline 用 template 标记保护，不适用）
        for name in ("personal_config.example.json", "intelligent_config.example.json",
                     "smoke_config.example.json", "smoke_config_wjx.json"):
            text = (ROOT / "assets" / name).read_text(encoding="utf-8")
            self.assertIn("<", text, "%s 应以尖括号占位符承载个人参数" % name)

    def test_in_package_real_configs_are_gitignored(self):
        """文档指导放在包内的真实值实例名，必须被 .gitignore 覆盖。

        文档约定「真实值实例名如 personal_config_cw.json / smoke_config_cw.json」，
        若这些名字没被 .gitignore 命中，照文档做就会把真实凭据 git add 进库 ——
        这是最隐蔽的一类凭据泄漏（约定本身制造了泄漏路径）。占位模板则必须保持可入库。
        """
        must_be_ignored = ["assets/personal_config_cw.json",
                           "assets/smoke_config_cw.json",
                           "assets/smoke_config_wjx_private.json"]
        must_stay_tracked = ["assets/personal_config.example.json",
                             "assets/smoke_config.example.json"]
        for p in must_be_ignored:
            self.assertTrue(_gitignored(p), "%s 未被 .gitignore 覆盖（照文档做会泄漏凭据）" % p)
        for p in must_stay_tracked:
            self.assertFalse(_gitignored(p), "%s 是占位模板，不应被 .gitignore 排除" % p)

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
