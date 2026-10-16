# -*- coding: utf-8 -*-
"""v4.18.0 功能：运行时依赖发现（运行实证通道）+ 前置条件未满足的单列口径。

覆盖四件容易回归的事：
  1. **取数纪律**——只让白名单话术进模型、只认 PASS 以外的终态、
     N/A / SKIPPED（"没测"）不得被当成失败；
  2. **机械校验**——证据必须是该条响应的原文子串，臆造 case_no / 自引用要拦；
  3. **闸门**——候选依赖不得生效，只有 confirmed 才允许改变归因；
  4. **增量**——不传依赖时行为与接入前完全一致。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
import _bootstrap  # noqa: F401,E402 环境替身（必须先于生产模块导入）

import precondition_scan as PS  # noqa: E402
import attribution_engine as AT  # noqa: E402

ERR = ("AI主页处于发布状态时不能直接更新，请先调用修改问卷状态接口1000102，"
       "传入state=2暂停主页后再更新。")


def _report(results):
    return {"results": results}


def _fail(case_no="D1-2", name="发布态下直接更新", action="1000108", errmsg=ERR):
    return {"case_no": case_no, "name": name, "action": action, "status": "FAIL",
            "dimension": "D", "response": {"result": False, "errormsg": errmsg}}


def _dep(status="confirmed", **kw):
    base = {"subject_action": "1000108", "subject_name": "更新AI主页",
            "target_action": "1000102", "target_name": "修改问卷状态",
            "condition": "AI主页处于发布状态", "params": {"state": "2"},
            "evidence": ERR, "status": status, "confidence": 0.9}
    base.update(kw)
    return base


class TestHarvest(unittest.TestCase):
    def test_whitelist_hit(self):
        cands = PS.harvest(_report([_fail()]), subject_name="更新AI主页")
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["hint"], "请先调用")
        self.assertEqual(cands[0]["subject_name"], "更新AI主页")
        self.assertEqual(cands[0]["case_name"], "发布态下直接更新")

    def test_plain_param_error_excluded(self):
        """普通参数报错不该被拉进模型——白名单的意义就在这里。"""
        cands = PS.harvest(_report([_fail(errmsg="标题不能为空")]))
        self.assertEqual(cands, [])

    def test_only_failures_considered(self):
        """N/A 与 SKIPPED 是"没测"，不是失败——算进来会把缺基线误判成前置未满足。"""
        recs = [
            dict(_fail(case_no="P1"), status="PASS"),
            dict(_fail(case_no="N1"), status="N/A"),
            dict(_fail(case_no="S1"), status="SKIPPED"),
        ]
        self.assertEqual(PS.harvest(_report(recs)), [])
        # 而已被判为前置未满足的记录仍会被重扫到（便于下一轮重新核对）
        self.assertEqual(len(PS.harvest(_report([dict(_fail(), status="PRECONDITION_UNMET")]))), 1)

    def test_response_from_note_field(self):
        """文案写在 note 里也要能筛到：不同接口把同一层语义放在不同字段。"""
        rec = {"case_no": "X1", "name": "n", "action": "1000108", "status": "FAIL",
               "response": {"note": ERR}}
        self.assertEqual(len(PS.harvest(_report([rec]))), 1)


class TestVerify(unittest.TestCase):
    def setUp(self):
        self.cands = PS.harvest(_report([_fail()]), subject_name="更新AI主页")

    def test_valid_fact_passes_as_candidate(self):
        facts = {"dependencies": [{"case_no": "D1-2", "target_action": "1000102",
                                   "evidence": ERR}]}
        rows, clean = PS.verify(facts, self.cands, known_actions=["1000102"])
        self.assertEqual(len(clean), 1)
        # 关键：通过校验也只是**候选**，不是生效
        self.assertEqual(clean[0]["status"], PS.CANDIDATE)
        self.assertEqual(clean[0]["source"], "runtime")

    def test_forged_evidence_blocked(self):
        facts = {"dependencies": [{"case_no": "D1-2", "target_action": "1000102",
                                   "evidence": "这句话不在响应里"}]}
        _, clean = PS.verify(facts, self.cands)
        self.assertEqual(clean, [])

    def test_fabricated_case_no_blocked(self):
        facts = {"dependencies": [{"case_no": "B9-9", "target_action": "1000102",
                                   "evidence": ERR}]}
        _, clean = PS.verify(facts, self.cands)
        self.assertEqual(clean, [])

    def test_self_reference_blocked(self):
        facts = {"dependencies": [{"case_no": "D1-2", "target_action": "1000108",
                                   "evidence": ERR}]}
        _, clean = PS.verify(facts, self.cands)
        self.assertEqual(clean, [])

    def test_bad_action_shape_blocked(self):
        facts = {"dependencies": [{"case_no": "D1-2", "target_action": "10001022",
                                   "evidence": ERR}]}
        _, clean = PS.verify(facts, self.cands)
        self.assertEqual(clean, [])

    def test_unknown_action_flagged_not_dropped(self):
        """不在已知集合里的 action 不算通过，但要留痕供人核对，而不是静默丢弃。"""
        facts = {"dependencies": [{"case_no": "D1-2", "target_action": "1000999",
                                   "evidence": ERR}]}
        _, clean = PS.verify(facts, self.cands, known_actions=["1000102"])
        self.assertEqual(len(clean), 1)
        self.assertEqual(clean[0]["status"], "UNKNOWN_ACTION")

    def test_missing_action_hinted_only(self):
        """原文没给 action 号：只留痕、不生成可执行依赖——定不位就不调。"""
        facts = {"dependencies": [{"case_no": "D1-2", "target_action": "",
                                   "target_hint": "上传文件接口", "evidence": ERR}]}
        _, clean = PS.verify(facts, self.cands)
        self.assertEqual(clean[0]["status"], "UNRESOLVED")
        self.assertEqual(clean[0]["target_hint"], "上传文件接口")

    def test_duplicate_collapsed(self):
        facts = {"dependencies": [{"case_no": "D1-2", "target_action": "1000102", "evidence": ERR},
                                  {"case_no": "D1-2", "target_action": "1000102", "evidence": ERR}]}
        _, clean = PS.verify(facts, self.cands)
        self.assertEqual(len(clean), 1)


class TestConfirmGate(unittest.TestCase):
    def test_confirm_then_effective(self):
        rows, clean = PS.verify(
            {"dependencies": [{"case_no": "D1-2", "target_action": "1000102", "evidence": ERR}]},
            PS.harvest(_report([_fail()])))
        recs = PS.dedup_merge([], clean)
        self.assertEqual(PS.confirmed_only(recs), [])
        self.assertEqual(PS.confirm(recs, all_flag=True), 1)
        self.assertEqual(len(PS.confirmed_only(recs)), 1)

    def test_dedup_keeps_confirmed_state(self):
        """重跑一次不能把已确认的依赖打回候选——否则等于让人重复确认同一件事。"""
        recs = [_dep(status=PS.CONFIRMED)]
        incoming = [dict(_dep(status=PS.CANDIDATE))]
        merged = PS.dedup_merge(recs, incoming)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["status"], PS.CONFIRMED)

    def test_markdown_marks_status(self):
        md = PS.render_markdown([_dep(status=PS.CONFIRMED), _dep(status="UNKNOWN_ACTION")])
        self.assertIn("运行实证", md)
        self.assertIn("已确认", md)
        self.assertIn("待确认", md)


class TestAttributionGate(unittest.TestCase):
    def test_confirmed_dep_switches_category(self):
        r = AT.attribute(response={"errormsg": ERR}, action="1000108", assertion_ok=False,
                         confirmed_deps=[_dep()])
        self.assertEqual(r["category"], AT.PRECONDITION_UNMET)
        self.assertIn("1000102", r["reason"])

    def test_candidate_dep_does_not_switch(self):
        """最关键的一条：候选不得生效，否则一次误判就能把真缺陷静默吞掉。"""
        r = AT.attribute(response={"errormsg": ERR}, action="1000108", assertion_ok=False,
                         confirmed_deps=[_dep(status=PS.CANDIDATE)])
        self.assertEqual(r["category"], "DEFECT")

    def test_subject_mismatch_does_not_swallow(self):
        r = AT.attribute(response={"errormsg": ERR}, action="1000107", assertion_ok=False,
                         confirmed_deps=[_dep()])
        self.assertEqual(r["category"], "DEFECT")

    def test_action_number_fallback(self):
        """响应文案改写过时，按 action 号兜底仍能命中。"""
        r = AT.attribute(response={"errormsg": "请先调用接口1000102暂停后再试"},
                         action="1000108", assertion_ok=False, confirmed_deps=[_dep()])
        self.assertEqual(r["category"], AT.PRECONDITION_UNMET)

    def test_incremental_without_deps(self):
        """增量接入：不传依赖时与接入前完全一致。"""
        r = AT.attribute(response={"errormsg": ERR}, action="1000108", assertion_ok=False)
        self.assertEqual(r["category"], "DEFECT")
        self.assertEqual(AT.attribute("offline", retries=2)["category"], "ENV")
        self.assertEqual(AT.attribute(response="请求频率过快")["category"], "RATE_LIMIT")


class TestRunnerWiring(unittest.TestCase):
    def test_precondition_status_counts_as_executed(self):
        """跑过但被前置挡住，不能反过来被报成覆盖率缺口。"""
        import api_test_runner as R
        self.assertIn(AT.PRECONDITION_UNMET, R.EXECUTED_STATUSES)

    def test_no_deps_by_default(self):
        import api_test_runner as R
        self.assertEqual(R.CONFIRMED_PRECONDITION_DEPS, [])

    def test_flow_loader_absent_file_is_noop(self):
        import flow
        self.assertEqual(flow.load_confirmed_runtime_deps("no_such_file.json"), [])

    def test_flow_loader_only_takes_confirmed(self):
        import flow
        doc = {"dependencies": [_dep(status=PS.CONFIRMED), _dep(status=PS.CANDIDATE),
                                _dep(status="UNRESOLVED")]}
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "deps.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False)
            got = flow.load_confirmed_runtime_deps(p)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["status"], PS.CONFIRMED)

    def test_flow_loader_survives_broken_json(self):
        import flow
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "bad.json")
            with open(p, "w", encoding="utf-8") as f:
                f.write("{not json")
            self.assertEqual(flow.load_confirmed_runtime_deps(p), [])


if __name__ == "__main__":
    unittest.main()
