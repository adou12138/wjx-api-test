# -*- coding: utf-8 -*-
"""artifact_readback 单测。

写法约定：**真产物 → 回读**。fixture 由真实写盘路径（api_test_runner.finding_row +
标准 csv.writer）产生，不用手搓字符串样例——手搓样例会让校验器与写盘端
一起错在同一个假设上，正是回读校验本该抓的那类bug。

正例/反例成对：每个 check_* 都有一个"应该过"和一个"应该被拦"。
"""
import csv
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: F401,E402 环境替身（必须先于生产模块导入）
import api_test_runner as R          # noqa: E402
import artifact_readback as AR        # noqa: E402

COLS = list(R.FINDING_CSV_COLS)


def _finding(fid, **over):
    d = {"id": fid, "kind": "缺陷", "severity": "高", "status": "待修复",
         "dimension": "B8", "desc": "写入超长标题后接口仍返回成功",
         "request": '{"encode":"json","appid":3585,"action":"1000107","sign":"***",'
                    '"ts":1757000000,"title":"Overlong title, with comma"}',
         "expected": "应返回参数校验失败", "actual": "HTTP 200 result=true",
         "confidence": 0.9, "http_code": 200, "request_id": "req-abc-123",
         "trace_id": "trace-xyz", "evidence_path": "evidence/E01.json",
         "attribution": "DEFECT:契约声明 max_len=100", "retest_status": "待复测"}
    d.update(over)
    return d


class ReadbackBase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = self._td.name

    def tearDown(self):
        self._td.cleanup()

    def _write_real(self, findings, name="api_test_issues.csv", encoding="utf-8-sig",
                    report=True, cols=None):
        """走真实写盘路径：finding_row → csv.writer。"""
        cols = cols or COLS
        p = os.path.join(self.dir, name)
        with open(p, "w", encoding=encoding, newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for x in findings:
                w.writerow(R.finding_row(x))
        if report:
            with open(os.path.join(self.dir, "api_test_report_full.json"),
                      "w", encoding="utf-8") as f:
                json.dump({"results": [], "findings": findings},
                          f, ensure_ascii=False, indent=2)
        return p

    def _codes(self, issues):
        return {i["code"] for i in issues}


class TestColumnDefinition(unittest.TestCase):
    """列定义外置的契约。"""

    def test_cols_loaded_from_assets(self):
        """正常路径：16 列全部从 assets 读到，无告警。"""
        self.assertEqual(len(COLS), 16)
        self.assertIsNone(R.FINDING_COLS_WARN)
        self.assertEqual(R._UNKNOWN_HEADERS, [])

    def test_every_col_has_field_mapping(self):
        """每列都有英文键映射——缺一列就渲染空值，且空值不报错。"""
        for h in COLS:
            self.assertIn(h, R._FINDING_FIELD_KEYS,
                          "列「%s」无映射键，finding_row 会把它渲染成空值" % h)

    def test_finding_row_width_matches_cols(self):
        row = R.finding_row(_finding("E01"))
        self.assertEqual(len(row), len(COLS))

    def test_field_order_does_not_shift_values(self):
        """列顺序变了，取值必须跟着走——错位是最难发现的静默失败。

        用受控列序反推：渲染行按 FINDING_CSV_COLS 的位置取值，
        所以把行按反序zip 回列名，各列拿到的仍应是自己的值。"""
        rendered = R.finding_row(_finding("E01"))
        row = dict(zip(COLS, rendered))
        self.assertEqual(row["编号"], "E01")
        self.assertEqual(row["TraceID"], "trace-xyz")
        self.assertEqual(row["HTTP状态码"], 200)
        # 再验顺序无关：反序 zip 会打乱配对关系，因此这里改验"渲染顺序随列定义走"
        reversed_def = list(reversed(COLS))
        import unittest.mock as _m
        with _m.patch.object(R, "FINDING_CSV_COLS", reversed_def):
            row2 = dict(zip(reversed_def, R.finding_row(_finding("E01"))))
        self.assertEqual(row2["编号"], "E01")
        self.assertEqual(row2["TraceID"], "trace-xyz")
        self.assertEqual(row, row2)


class TestHappyPath(ReadbackBase):
    """正例：真产物应该零error。"""

    def test_real_artifact_passes(self):
        p = self._write_real([_finding("E01"), _finding("E02"), _finding("H01")])
        issues, summary = AR.run_readback(dirpath=self.dir, issues_csv=p)
        self.assertEqual(AR.summary if False else summary["n_error"], 0,
                         [i["message"] for i in issues if i["severity"] == AR.ERROR])
        self.assertEqual(summary["n_warning"], 0, [i for i in issues])

    def test_special_chars_survive_roundtrip(self):
        """含逗号/引号/换行的字段，回读后必须原样——这正是要引号包裹的原因。

        换行符在 ``newline=""`` 写出 + 默认 universal_newlines 读回时是\\n 原样，
        这正是引号包裹把含换行字段保护住的可观测证据；不包裹就会跨行撕裂成多行。"""
        tricky = "逗号, 引号\" 单引号' 换行\n第二行 中文"
        p = self._write_real([_finding("E01", desc=tricky)])
        issues = AR.check_csv(p, expect_cols=COLS)
        self.assertEqual(self._codes(issues) & {"ROW_WIDTH", "DECODE"}, set(),
                         [i["message"] for i in issues])
        with open(p, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 1, "含换行的字段未被正确包裹，已被撕裂成多行")
        self.assertEqual(rows[0]["问题描述"], tricky)

    def test_empty_findings_ok(self):
        """零缺陷是合法结果（不是缺口），不该报error。"""
        p = self._write_real([])
        issues = AR.check_csv(p, expect_cols=COLS, expect_rows=0)
        self.assertNotIn("ROW_COUNT", self._codes(issues))


class TestCsvGates(ReadbackBase):
    """反例：每一类静默失败都要被拦住。"""

    def test_missing_bom_caught(self):
        p = self._write_real([_finding("E01")], encoding="utf-8")
        self.assertIn("NO_BOM", self._codes(AR.check_csv(p, expect_cols=COLS)))

    def test_missing_file_caught(self):
        issues = AR.check_csv(os.path.join(self.dir, "nope.csv"), expect_cols=COLS)
        self.assertEqual(self._codes(issues), {"MISSING"})

    def test_zero_byte_caught(self):
        p = os.path.join(self.dir, "empty.csv")
        open(p, "wb").close()
        self.assertIn("EMPTY", self._codes(AR.check_csv(p, expect_cols=COLS)))

    def test_header_mismatch_caught(self):
        """少一列：消费方按列位取值就整体错位。"""
        p = self._write_real([_finding("E01")], cols=[c for c in COLS if c != "TraceID"])
        self.assertIn("HEADER_MISMATCH", self._codes(AR.check_csv(p, expect_cols=COLS)))

    def test_header_reorder_caught(self):
        """列名齐全但顺序不同——同样必须拦。"""
        p = self._write_real([_finding("E01")], cols=list(reversed(COLS)))
        issues = AR.check_csv(p, expect_cols=COLS)
        codes = self._codes(issues)
        self.assertIn("HEADER_MISMATCH", codes)
        hit = [i for i in issues if i["code"] == "HEADER_MISMATCH"][0]
        self.assertIn("顺序", hit["message"])

    def test_row_width_mismatch_caught(self):
        """某行少列——渲染层错位或含逗号字段未引号包裹。"""
        p = os.path.join(self.dir, "bad.csv")
        with open(p, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(COLS)
            w.writerow(R.finding_row(_finding("E01"))[:5])  # 故意截短
        self.assertIn("ROW_WIDTH", self._codes(AR.check_csv(p, expect_cols=COLS)))

    def test_blank_id_caught(self):
        p = self._write_real([_finding("")])
        self.assertIn("BLANK_ID", self._codes(AR.check_csv(p, expect_cols=COLS)))

    def test_duplicate_id_caught(self):
        """重复编号会让复测指向错条目。"""
        p = self._write_real([_finding("E01"), _finding("E01")])
        self.assertIn("DUP_ID", self._codes(AR.check_csv(p, expect_cols=COLS)))

    def test_row_count_drift_caught(self):
        """台账行数 vs findings 数不符——有finding 静默丢失。"""
        p = self._write_real([_finding("E01"), _finding("E02")])
        issues = AR.check_csv(p, expect_cols=COLS, expect_rows=5)
        hit = [i for i in issues if i["code"] == "ROW_COUNT"][0]
        self.assertIn("静默丢失", hit["message"])

    def test_evidence_cols_missing_caught(self):
        p = self._write_real([_finding("E01")],
                             cols=[c for c in COLS if c not in AR.EVIDENCE_COLS])
        self.assertIn("EV_COL_MISSING", self._codes(AR.check_csv(p, expect_cols=COLS)))


class TestReportJson(ReadbackBase):
    def test_broken_json_caught(self):
        p = os.path.join(self.dir, "api_test_report_full.json")
        open(p, "w", encoding="utf-8").write("{broken")
        self.assertIn("JSON_PARSE", self._codes(AR.check_report_json(p)))

    def test_missing_findings_key_caught(self):
        p = os.path.join(self.dir, "r.json")
        json.dump({"results": []}, open(p, "w", encoding="utf-8"))
        self.assertIn("NO_FINDINGS_KEY", self._codes(AR.check_report_json(p)))

    def test_missing_report_is_warning_not_error(self):
        """报告缺失只降级为 warning——它只是交叉核对用的辅助，不该拦住单份台账交付。"""
        issues = AR.check_report_json(os.path.join(self.dir, "nope.json"))
        self.assertEqual(issues[0]["severity"], AR.WARNING)

    def test_smoke_vs_deep_header_drift_detectable(self):
        """冒烟曾少写 请求ID/TraceID。回读必须能把这类两套表头差异点出来。"""
        smoke_cols = [c for c in COLS if c not in ("请求ID", "TraceID")]
        p = self._write_real([_finding("SMK-001")], name="smoke_issues.csv", cols=smoke_cols)
        issues = AR.check_csv(p, expect_cols=COLS)
        codes = self._codes(issues)
        self.assertIn("HEADER_MISMATCH", codes)
        self.assertIn("EV_COL_MISSING", codes)

    def test_all_evidence_cols_empty_caught(self):
        """采集类证据列**整列为空**必须报错。

        这不是构造出来的边角：请求ID/TraceID/证据路径常压根没接采集链，
        只查"列在不在"会把一张没有复现线索的台账判成"可交付"。
        记录类列（置信度/HTTP状态码）几乎总有值，所以判定只看采集类。
        """
        bare = _finding("E01", request_id="", trace_id="", evidence_path="")
        p = self._write_real([bare])
        codes = self._codes(AR.check_csv(p, expect_cols=COLS))
        self.assertIn("EV_ALL_EMPTY", codes)

    def test_evidence_partly_empty_is_warning(self):
        """部分条目缺采集类证据 → warning，不拦交付（可能只是那几条本来就没采到）。"""
        full = _finding("E01")
        bare = _finding("E02", request_id="", trace_id="", evidence_path="")
        p = self._write_real([full, bare])
        issues = AR.check_csv(p, expect_cols=COLS)
        codes = self._codes(issues)
        self.assertNotIn("EV_ALL_EMPTY", codes)
        sev = {i["code"]: i["severity"] for i in issues}
        self.assertEqual(sev.get("EV_PART_EMPTY"), AR.WARNING)

    def test_evidence_present_passes(self):
        """证据链齐全时不得误报——空值检查不能变成"永远报警"。"""
        p = self._write_real([_finding("E01")])
        codes = self._codes(AR.check_csv(p, expect_cols=COLS))
        self.assertNotIn("EV_ALL_EMPTY", codes)
        self.assertNotIn("EV_PART_EMPTY", codes)
        self.assertNotIn("EV_LEDGER_EMPTY", codes)

    def test_empty_ledger_not_flagged_as_no_evidence(self):
        """零缺陷（空台账）合法——不能因为"没有证据链"就报error。"""
        p = self._write_real([])
        codes = self._codes(AR.check_csv(p, expect_cols=COLS))
        self.assertNotIn("EV_ALL_EMPTY", codes)


class TestReportPairing(ReadbackBase):
    """台账与报告必须同前缀配对。"""

    def _write_pair(self, prefix, n_csv, n_json):
        p = os.path.join(self.dir, "%s_issues.csv" % prefix)
        with open(p, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(COLS)
            for i in range(n_csv):
                w.writerow(R.finding_row(_finding("E%02d" % i)))
        fs = [_finding("E%02d" % i) for i in range(n_json)]
        json.dump({"findings": fs},
                  open(os.path.join(self.dir, "%s_report_full.json" % prefix),
                       "w", encoding="utf-8"), ensure_ascii=False)
        return p

    def test_multi_prefix_dir_does_not_cross_pair(self):
        """目录里有多个前缀的产物时，必须按前缀配对，不得跨运行误报行数不符。

        这就是按 mtime 挑报告的真实故障：一次运行留下 chain_（1 条）
        与 noev_（3 条）两套产物，配错对后会拿chain_ 的报告去比 noev_ 的台账，
        报出根本不存在的"finding 静默丢失"。每个前缀自己是对齐的。
        """
        self._write_pair("chain", 1, 1)
        p = self._write_pair("noev", 3, 3)
        _, summary = AR.run_readback(dirpath=self.dir, issues_csv=p)
        self.assertEqual(summary["n_error"], 0,
                         [i["message"] for i in summary["issues"]
                          if i["severity"] == AR.ERROR])
        self.assertTrue(summary["report_json"].endswith("noev_report_full.json"),
                        "应配对 noev_ 前缀，实际配到 %s" % summary["report_json"])
        self.assertEqual(summary["findings_count"], 3)

    def test_same_prefix_drift_still_caught(self):
        """同前缀下行数真的不符时仍必须报错——修误报不能连带漏掉真问题。"""
        self._write_pair("api_test", 1, 1)
        p = self._write_pair("other", 1, 3)
        issues, summary = AR.run_readback(dirpath=self.dir, issues_csv=p)
        self.assertGreater(summary["n_error"], 0)
        self.assertIn("ROW_COUNT", {i["code"] for i in issues})

    def test_no_paired_report_is_warning(self):
        """找不到配对报告 → warning（不是 error），且明说行数这次查不出来。"""
        p = os.path.join(self.dir, "orphan_issues.csv")
        with open(p, "w", encoding="utf-8-sig", newline="") as f:
            csv.writer(f).writerow(COLS)
        issues, summary = AR.run_readback(dirpath=self.dir, issues_csv=p)
        self.assertEqual(summary["n_error"], 0)
        hit = [i for i in issues if i["code"] == "NO_PAIRED_REPORT"]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["severity"], AR.WARNING)
        self.assertEqual(summary["findings_count"], None)

    def test_csv_in_subdir_pairs_by_its_own_dir(self):
        """只给 --issues 子路径、不带 --dir 时，应在台账所在目录下找配对报告，
        而不是在 cwd 找（否则会漏配对、误报 NO_PAIRED_REPORT）。

        复现：csv 与报告都在 sub/，但 dirpath 传父目录。修复后 _pick_report_for
        用 os.path.dirname(csv_path) 定位报告。"""
        sub = os.path.join(self.dir, "sub")
        os.makedirs(sub, exist_ok=True)
        p = os.path.join(sub, "x_issues.csv")
        with open(p, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(COLS)
            w.writerow(R.finding_row(_finding("E01")))
        json.dump({"findings": [_finding("E01")]},
                  open(os.path.join(sub, "x_report_full.json"), "w", encoding="utf-8"),
                  ensure_ascii=False)
        _, summary = AR.run_readback(dirpath=self.dir, issues_csv=p)
        self.assertEqual(summary["n_error"], 0,
                         [i["message"] for i in summary["issues"]
                          if i["severity"] == AR.ERROR])
        self.assertTrue(summary["report_json"].replace("/", "\\").endswith(
            "sub\\x_report_full.json"),
            "应在子目录配对，实际配到 %s" % summary["report_json"])


class TestColumnExternalization(unittest.TestCase):
    """列定义外置不得引入静默失败：内置兜底必须与 assets 一致。"""

    def test_default_cols_matches_current_assets(self):
        """内置默认列必须与 assets 一致，否则回退会悄悄换掉列口径。"""
        self.assertEqual(R.DEFAULT_FINDING_COLS, list(R.FINDING_CSV_COLS))

    def test_loader_rejects_empty_definition(self):
        """assets 里列定义为空/含非法表头时，必须拒绝而不是照单全收。"""
        import tempfile as _tf
        with _tf.TemporaryDirectory() as td:
            p = os.path.join(td, "finding_columns.json")
            for bad in ({"finding_columns": []},
                        {"finding_columns": [{"header": ""}]},
                        {"finding_columns": [{"note": "漏了 header 键"}]}):
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(bad, f)
                cols, warn = R._load_finding_cols_from(p)
                self.assertEqual(cols, list(R.DEFAULT_FINDING_COLS),
                                 "坏定义应回退默认列：%r" % (bad,))
                self.assertIsNotNone(warn, "坏定义必须出声，不能静默回退")

    def test_loader_accepts_good_definition(self):
        import tempfile as _tf
        with _tf.TemporaryDirectory() as td:
            p = os.path.join(td, "finding_columns.json")
            json.dump({"finding_columns": [{"header": "甲"}, {"header": "乙"}]},
                      open(p, "w", encoding="utf-8"))
            cols, warn = R._load_finding_cols_from(p)
            self.assertEqual(cols, ["甲", "乙"])
            self.assertIsNone(warn)

    def test_smoke_uses_shared_column_source(self):
        """冒烟台账必须与深度台账同源列，不允许再写一份硬编码列表。"""
        import smoke_runner as S
        self.assertEqual(list(S.SMOKE_FINDING_COLS), list(R.FINDING_CSV_COLS))


class TestReadbackExitContract(unittest.TestCase):
    """_print_report 的返回契约：只有 error 才判失败。

    warning 是否升级由调用方严格度（--strict / WJX_STRICT_READBACK）决定。
    这里锁死"warning-only 不返回失败"，否则默认温和的约定会被架空——
    CLI 曾在 warning-only 时也无条件 return 1，使 strict 开关形同虚设。
    """

    def _print(self, issues):
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            return AR._print_report(issues)

    def test_warning_only_is_not_failure(self):
        self.assertEqual(self._print([{"severity": AR.WARNING, "code": "X", "message": "m"}]), 0)

    def test_error_is_failure(self):
        self.assertEqual(self._print([{"severity": AR.ERROR, "code": "X", "message": "m"}]), 1)

    def test_clean_passes(self):
        self.assertEqual(self._print([]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)