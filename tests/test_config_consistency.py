# -*- coding: utf-8 -*-
"""配置一致性门禁（config_consistency_check）与 WJX_STRICT_* 严格度约定的单测。

覆盖三类：
  1) config_consistency_check 的矛盾检测（region 冲突 / 跨环境缺失 / 域-区启发式）；
  2) WJX_STRICT_CONSISTENCY=1：配置有警告时启动阶段 exit(2)，否则放行；
  3) WJX_STRICT_READBACK=1：产物只有 warning 时 exit(1)，默认温和 exit(0)。

严格度约定是「防静默失败」的最后一环：默认温和只提示，显式开启才拦，
不能反过来（默认就拦会让正常回归跑不动，默认不提示则矛盾被静默吞掉）。
"""
import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: F401,E402
from personal_config import config_consistency_check  # noqa: E402
import api_test_runner as R  # noqa: E402

_STRICT_KEYS = ("WJX_REGION", "WJX_CROSS_ENV", "WJX_STRICT_CONSISTENCY",
                "WJX_STRICT_READBACK", "WJX_ENV")


def _clean_env(extra=None):
    """构造干净子进程环境：剥掉一切 WJX_* 继承值，避免父进程替身污染。"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("WJX_")}
    env.update(extra or {})
    return env


class TestConfigConsistency(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _STRICT_KEYS}
        for k in _STRICT_KEYS:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _levels(self, issues):
        return {lvl for lvl, _m in issues}

    def test_region_env_conflict_is_fatal(self):
        os.environ["WJX_REGION"] = "intl"
        issues = config_consistency_check(
            {"_selected_region": "cn", "base_url": "https://cn.sojump.cn/api"})
        self.assertIn("fatal", self._levels(issues))
        self.assertTrue(any("region" in m for _l, m in issues))

    def test_cross_env_requested_but_empty_is_fatal(self):
        issues = config_consistency_check({}, cross_env_requested=True, cross_cfg=None)
        self.assertIn("fatal", self._levels(issues))

    def test_cross_env_loaded_no_fatal(self):
        issues = config_consistency_check({}, cross_env_requested=True,
                                          cross_cfg={"base_url": "https://a/x"})
        self.assertNotIn("fatal", self._levels(issues))

    def test_region_domain_mismatch_warns(self):
        issues = config_consistency_check(
            {"_selected_region": "cn", "base_url": "https://x.sojump.com/api"})
        self.assertIn("warn", self._levels(issues))
        issues = config_consistency_check(
            {"_selected_region": "intl", "base_url": "https://x.sojump.cn/api"})
        self.assertIn("warn", self._levels(issues))

    def test_matching_region_domain_no_warn(self):
        issues = config_consistency_check(
            {"_selected_region": "cn", "base_url": "https://x.sojump.cn/api"})
        self.assertEqual(issues, [])

    def test_no_region_cannot_judge(self):
        issues = config_consistency_check({"base_url": "https://x.sojump.com/api"})
        self.assertEqual(issues, [])

    def test_non_dict_guard(self):
        self.assertEqual(config_consistency_check(None), [])
        self.assertEqual(config_consistency_check("oops"), [])


class TestStrictConsistencyGate(unittest.TestCase):
    """WJX_STRICT_CONSISTENCY：smoke_runner 启动阶段的严格度开关。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        d = self._td.name
        self.personal = os.path.join(d, "personal.json")
        with open(self.personal, "w", encoding="utf-8") as f:
            json.dump({"default_env": "e",
                       "envs": [{"name": "e", "region": "cn",
                                 "base_url": "https://t.sojump.com/api",
                                 "appid": "a", "sign": "s"}]}, f)
        self.cfg = os.path.join(d, "cfg.json")
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump({"name": "t", "base_url": "https://t.sojump.com/api/{endpoint}",
                       "default_endpoint": "q",
                       "common_params": {"encode": "json", "appid": "a", "sign": "s", "ts": "1"}}, f)
        self.cases = os.path.join(d, "cases.json")
        with open(self.cases, "w", encoding="utf-8") as f:
            json.dump({"cases": []}, f)
        self.out = os.path.join(d, "reports")

    def tearDown(self):
        self._td.cleanup()

    def _run(self, extra_env):
        return subprocess.run(
            [sys.executable, os.path.join("scripts", "smoke_runner.py"),
             "--config", self.cfg, "--cases", self.cases, "--personal", self.personal,
             "--dry-run", "--out-dir", self.out],
            cwd=ROOT, env=_clean_env(extra_env),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_warning_does_not_block_by_default(self):
        p = self._run({})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("配置一致性警告", p.stdout)

    def test_strict_env_escalates_warning_to_exit_2(self):
        p = self._run({"WJX_STRICT_CONSISTENCY": "1"})
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)


class TestStrictReadbackGate(unittest.TestCase):
    """WJX_STRICT_READBACK：产物回读的严格度开关（warning-only 场景）。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.d = self._td.name
        cols = list(R.FINDING_CSV_COLS)
        with open(os.path.join(self.d, "api_test_issues.csv"), "w",
                  encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            w.writerow(R.finding_row({
                "id": "F1", "kind": "缺陷", "severity": "高", "status": "待修复",
                "dimension": "B8", "desc": "越权返回成功",
                "request": '{"encode":"json","appid":1,"action":"1000107","sign":"***","ts":1}',
                "expected": "应拒绝", "actual": "HTTP 200", "confidence": 0.9,
                "http_code": 200, "request_id": "r1", "trace_id": "t1",
                "evidence_path": "e/E01.json", "attribution": "DEFECT", "retest_status": "待复测",
            }))

    def tearDown(self):
        self._td.cleanup()

    def _run(self, extra_env):
        return subprocess.run(
            [sys.executable, os.path.join("scripts", "artifact_readback.py"), "--dir", self.d],
            cwd=ROOT, env=_clean_env(extra_env),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_warning_only_not_blocking_by_default(self):
        p = self._run({})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_strict_env_escalates_warning_to_exit_1(self):
        p = self._run({"WJX_STRICT_READBACK": "1"})
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
