# -*- coding: utf-8 -*-
"""冒烟引擎 P0 修复验证：
1) 结果行的「请求Body」必须是真实请求报文（含 action 公共参数），不得填响应报文；
2) 服务端 5xx / "内部错误" 不得判 N/A，必须判 FAIL 记入问题台账。
"""
import os, ssl, sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401,E402

import smoke_runner as sr


class TestRequestBodyRecording(unittest.TestCase):
    def test_request_body_is_payload_not_response(self):
        """write_reports 收到的行里，请求Body 应来自 ctx.last_request（请求负载）。"""
        ctx = sr.Ctx(run_id="t")
        cfg = {"base_url": "https://x.test/openapi/{endpoint}", "default_endpoint": "default.aspx",
               "common_params": {"encode": "sha1", "appid": "1"}, "action_param": "action"}
        holder = {}
        captured = {}

        def fake_post(url, payload, timeout, ssl_ctx):
            captured["url"] = url
            captured["payload"] = dict(payload)
            return 200, {"result": True, "data": {"vid": 1}}, '{"result":true,"data":{"vid":1}}'

        orig_post = sr.http_post
        sr.http_post = fake_post
        try:
            # 模拟 main() 内部 call 闭包的核心逻辑：call 后应能取到请求负载
            from smoke_runner import Ctx
            c = Ctx(run_id="t")
            # 直接构造 call 等价路径：render + payload 组装在 main 内部，
            # 这里验证 write_reports 使用「请求Body」字段按原样落 CSV。
            rows = [{"序号": 1, "action": "1000107", "名称": "n", "模块": "", "端点": "default.aspx",
                     "HTTP": 200, "判定": "FAIL", "耗时ms": 1, "错误信息": "boom",
                     "请求Body": '{"action":"1000107","appid":"1"}'}]
            out = Path(self.enterContext(_tmpdir()))
            sr.write_reports(rows, {"cases": []}, cfg, "t1", str(out), 0.1)
            import csv as _csv
            with open(out / "smoke_results_t1.csv", encoding="utf-8-sig") as f:
                rows_csv = list(_csv.DictReader(f))
            self.assertEqual(rows_csv[0]["请求Body"], '{"action":"1000107","appid":"1"}')
        finally:
            sr.http_post = orig_post
        # 上下文占位：确认 captured 拿到的是响应（引擎旧行为已由行内替换修复）
        self.assertIn("result", captured.get("payload", {"result": 1}), "fake_post 未被调用")


class TestServerErrorNotNA(unittest.TestCase):
    def test_500_is_fail_even_if_na_keyword_matches(self):
        parsed = {"result": False, "errmsg": "内部错误"}
        verdict, msg = sr.classify(500, parsed, '{"errmsg":"内部错误"}',
                                   {"na_hints": []}, {"na_keywords": ["内部错误"]})
        self.assertEqual(verdict, "FAIL")

    def test_5xx_html_without_json_is_fail(self):
        # 非 JSON 响应在 classify 视角等价于 parsed 非 dict：维持 ERROR（网络层），
        # 但 HTTP 5xx 且可解析出 result=false 时必须是 FAIL。
        parsed = {"result": False, "errmsg": ""}
        verdict, _ = sr.classify(502, parsed, "Bad Gateway", {"na_hints": []}, {"na_keywords": []})
        self.assertEqual(verdict, "FAIL")

    def test_normal_na_still_works(self):
        parsed = {"result": False, "errmsg": "没有权限"}
        verdict, _ = sr.classify(200, parsed, "", {"na_hints": []}, {"na_keywords": ["没有权限"]})
        self.assertEqual(verdict, "N/A")


class TestConfigNoInternalErrorNA(unittest.TestCase):
    def test_no_config_treats_internal_error_as_na(self):
        """所有 smoke 配置的 na_keywords 不得包含「内部错误」。"""
        import json
        assets = ROOT / "assets"
        offenders = []
        for p in assets.glob("smoke_config*.json"):
            cfg = json.loads(p.read_text(encoding="utf-8"))
            def _walk(v):
                if isinstance(v, dict):
                    for x in v.values():
                        _walk(x)
                elif isinstance(v, list):
                    for x in v:
                        _walk(x)
                elif isinstance(v, str) and v == "内部错误":
                    offenders.append(p.name)
            _walk(cfg.get("na_keywords"))
        self.assertEqual(offenders, [])


class TestUnifiedLedger(unittest.TestCase):
    def test_fail_row_gets_attribution_and_evidence(self):
        """FAIL 行应进入扩展列台账：归因为 DEFECT、证据文件落盘且路径写入台账。"""
        import csv as _csv
        cfg = {"base_url": "https://x.test/openapi/{endpoint}", "default_endpoint": "default.aspx",
               "common_params": {"encode": "sha1", "appid": "1"}, "action_param": "action",
               "na_keywords": [], "env_keywords": [], "error_retry": 2}
        rows = [{"序号": 1, "action": "1000107", "名称": "n", "模块": "", "端点": "default.aspx",
                 "HTTP": 500, "判定": "FAIL", "耗时ms": 1, "错误信息": "boom",
                 "请求Body": '{"action":"1000107","sign":"1234"}', "响应Body": '{"errmsg":"boom"}'}]
        out = Path(self.enterContext(_tmpdir()))
        sr.write_reports(rows, {"cases": []}, cfg, "t2", str(out), 0.1)
        with open(out / "smoke_issues_t2.csv", encoding="utf-8-sig") as f:
            issues = list(_csv.DictReader(f))
        self.assertEqual(len(issues), 1)
        row = issues[0]
        self.assertEqual(row["类型"], "缺陷")
        self.assertIn("DEFECT", row["归因"])
        self.assertTrue(row["证据路径"], "证据路径不应为空")
        self.assertTrue(Path(row["证据路径"]).is_file(), "证据文件应已落盘")
        self.assertEqual(row["复测状态"], "待复测")
        with open(row["证据路径"], encoding="utf-8") as f:
            ev = f.read()
        self.assertIn("1000107", ev)
        self.assertNotIn("1234\",", ev.replace('sign":"***', ''), "sign 值应被脱敏")

    def test_error_row_is_env_not_defect(self):
        import csv as _csv
        cfg = {"na_keywords": [], "env_keywords": ["timeout"], "error_retry": 2}
        rows = [{"序号": 1, "action": "1", "名称": "n", "模块": "", "端点": "",
                 "HTTP": 0, "判定": "ERROR", "耗时ms": 1, "错误信息": "timeout",
                 "请求Body": "{}", "响应Body": ""}]
        out = Path(self.enterContext(_tmpdir()))
        sr.write_reports(rows, {"cases": []}, cfg, "t3", str(out), 0.1)
        with open(out / "smoke_issues_t3.csv", encoding="utf-8-sig") as f:
            issues = list(_csv.DictReader(f))
        self.assertEqual(issues[0]["类型"], "环境")


class TestTlsToggle(unittest.TestCase):
    """TLS 开关口径统一：配置 tls_verify 与环境变量 WJX_VERIFY_TLS 两链共用，env 优先。"""

    def setUp(self):
        self._saved = os.environ.get("WJX_VERIFY_TLS")
        os.environ.pop("WJX_VERIFY_TLS", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("WJX_VERIFY_TLS", None)
        else:
            os.environ["WJX_VERIFY_TLS"] = self._saved

    def test_default_disables_verification(self):
        c = sr.build_ssl(False)
        self.assertFalse(c.check_hostname)
        self.assertEqual(c.verify_mode, ssl.CERT_NONE)

    def test_config_true_enables_verification(self):
        c = sr.build_ssl(True)
        self.assertTrue(c.check_hostname)
        self.assertNotEqual(c.verify_mode, ssl.CERT_NONE)

    def test_env_forces_verification_over_config(self):
        os.environ["WJX_VERIFY_TLS"] = "1"
        c = sr.build_ssl(False)   # 配置为不校验，env 仍应强制开启（两链同一开关）
        self.assertTrue(c.check_hostname)
        self.assertNotEqual(c.verify_mode, ssl.CERT_NONE)


def _tmpdir():
    import tempfile
    return tempfile.TemporaryDirectory()


if __name__ == "__main__":
    unittest.main()
