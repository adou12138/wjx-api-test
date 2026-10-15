# -*- coding: utf-8 -*-
"""一键流水线：一条命令打通「载入契约 → 判适用性 → 生成用例 → 执行 → 出报告」。

解决的问题：原本要人工走七步（0.5 载入契约 / 1 梳理契约 / 1.5 判适用性 /
2 设计覆盖 / 2.5 过缺陷模式 / 3 收紧断言 / 5 执行），每一步都可能"做少了"。
本脚本把可自动化的部分（0.5 + 1 + 1.5 + 2 + 2.5 + 5）串成一条命令。

用法：
  # 从契约文档直接跑（只生成，不执行）
  python flow.py --contract <契约路径>
  # 从知识库按 action 取契约并生成
  python flow.py --action 1000107 [--kb <KB根目录>]
  # 生成并真实执行
  python flow.py --contract <路径> --execute
  # 只跑指定维度
  python flow.py --contract <路径> --execute --only B,H

产物（--out-dir，默认 ./api_test_out）：
  generated_cases.json   声明式用例（含 COVERAGE_PLAN / N/A 证明 / 命中模式）
  intelligent_test_plan.json   智能计划（契约模型 + 关系 + 显式依赖 + 风险 + 资产 + 阻塞项）
  dependencies.json      契约正文显式声明的跨接口依赖（含原文证据与行号）
  plan.md                人可读计划（用例总览 / 待确认问题 / 前置依赖与关系 / 风险与不适用凭据）
  api_test_issues.csv    问题台账（16 列统一口径：基础 9 列 + 置信度/HTTP状态码/请求ID/TraceID/证据路径/归因/复测状态）
  api_test_report_full.json
  api_test_issues_enhanced.md   增强报告（聚类去重 + 复现命令 + 修复建议）
"""
import argparse
import csv
import json
import os
import sys
import time
from dataclasses import asdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import case_generator  # noqa: E402
import case_runner     # noqa: E402
import api_test_runner as R  # noqa: E402
import report_enhance  # noqa: E402
import discovery_profile  # noqa: E402
from asset_registry import register  # noqa: E402
from contract_model import load_contract_model  # noqa: E402
from relationship_inference import infer_relations  # noqa: E402
from risk_engine import score as score_risk  # noqa: E402
from plan_builder import build_plan  # noqa: E402
import dependency_scan  # noqa: E402
import plan_render  # noqa: E402
import precondition_scan  # noqa: E402
import artifact_readback  # noqa: E402


def verify_artifacts(out_dir, csv_path):
    """产物回读校验：有 error 级问题就出声，并把机器可读结果落盘。

    默认不阻断执行——台账已经写出来了，回读的作用是**让交付方看见**
    "表结构不对/ 证据链空/ finding 丢了"，而不是把已跑完的一轮变成失败。
    设 WJX_STRICT_READBACK=1 才升级为 exit(2)，与覆盖率门禁同一套严格度约定。

    返回 error 数，便于调用方按需处理。
    """
    issues, summary = artifact_readback.run_readback(dirpath=out_dir, issues_csv=csv_path)
    errors = [i for i in issues if i["severity"] == artifact_readback.ERROR]
    warnings = [i for i in issues if i["severity"] == artifact_readback.WARNING]

    rb_path = os.path.join(out_dir, "artifact_readback.json")
    try:
        with open(rb_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print("⚠️ 回读结果落盘失败（%s），校验结论仍见上方输出" % e)

    if not errors and not warnings:
        print("\n产物回读通过：台账编码 / 表头 / 列宽 / 编号 / 行数一致，可交付。")
        return 0
    print("\n产物回读发现问题（详见 %s）：" % rb_path)
    for it in issues:
        icon = "✗" if it["severity"] == artifact_readback.ERROR else "!"
        print("  %s [%s] %s" % (icon, it["severity"].upper(), it["message"]))
    if errors:
        print("  → 台账结构有问题，交付前必须修正（脚本执行成功 ≠ 产物可交付）。")
    if os.environ.get("WJX_STRICT_READBACK") == "1" and (errors or warnings):
        print("  → WJX_STRICT_READBACK=1，升级为失败退出。")
        sys.exit(2)
    return len(errors)

# 写操作类接口：未通过测试环境写门禁时，一律不自动执行（只出计划并要求显式授权）。
WRITE_OPS = ("创建", "新增", "修改", "更新", "删除", "发布", "下线", "导入", "订阅", "清空")

# 执行档位 → 用例筛选规则。plan 档位不执行（在 main 中提前返回）。
PROFILES = {
    # 冒烟档位：只跑 P0 且启用的用例（快速判活）
    "smoke": lambda c: str(c.get("priority")) == "P0" and bool(c.get("enabled", True)),
    "full": lambda c: bool(c.get("enabled", True)),
    "exploratory": lambda c: True,  # 含探索性/低置信度用例
    # 发现档：全选（与 exploratory 同），区别在**生成期**——本档会先激活
    # discovery_profile，放宽超长上限/载荷条数/字段族配额/AF 条数。选择规则
    # 只是"全选"，真正的"多探"发生在限额上，见 discovery_profile。
    "discovery": lambda c: True,
}


def build_adapter(name):
    """按 --adapter 选择适配器。

    支持：rest / sojump / graphql / grpc；名称未匹配时 fallback 到 rest。
    grpc 缺依赖时实例化即报错（懒加载防呆，不静默降级）。

    两点容易误读的地方：
    - 主流程未显式指定时注入的是 **sojump**（见 main 的 adapter_name 默认值），
      好让写操作门禁默认生效——这是有意的保守默认，不能按"rest 默认"去理解。
    - 适配器在当前主链路**只用于写操作门禁**（is_write_allowed）；公共参数由
      api_test_runner 负责、实际发请求也走它，build_request/send 等是预留扩展点。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)  # adapters/ 与 scripts/ 平级，需把 skill 根加入搜索路径
    if name == "sojump":
        from adapters.sojump import SojumpAdapter
        return SojumpAdapter()
    if name == "graphql":
        from adapters.graphql import GraphqlAdapter
        return GraphqlAdapter()
    if name == "grpc":
        from adapters.grpc import GrpcAdapter
        return GrpcAdapter()
    from adapters.rest import RestAdapter
    return RestAdapter()


def load_exec_config(path):
    """加载可选执行配置（缺省返回空配置，不影响默认行为）。"""
    if not path:
        return {}
    if not os.path.isfile(path):
        sys.stderr.write("执行配置不存在：%s\n" % path)
        sys.exit(2)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_gate(adapter, adapter_name, exec_cfg, op_type):
    """测试环境写操作门禁。

    返回 (allowed, reason)。只有显式声明 `environment.type == "test"` 且
    `environment.write_enabled == true` 才放行写操作——仅凭域名像测试环境不足以信任。
    这是 adapters/sojump.py 声明的门禁，此前从未被任何执行入口调用（形同虚设）。
    """
    env = (exec_cfg or {}).get("environment") or {}
    is_write = str(op_type or "") in WRITE_OPS
    if not is_write or adapter_name != "sojump":
        return True, ""
    if adapter.is_write_allowed(env):
        return True, ""
    return False, (
        "写操作门禁未放行：接口操作类型为「%s」，但执行配置未显式声明测试环境。\n"
        "  需满足：environment.type == \"test\" 且 environment.write_enabled == true\n"
        "  （仅凭域名像测试环境不足以信任；见 references/intelligence_policy.md）" % op_type)


def select_by_profile(cases, profile):
    """按档位筛选用例：smoke 只跑 P0，full 跑全部启用项，exploratory 额外纳入探索性用例。"""
    rule = PROFILES.get(profile)
    if not rule:
        return list(cases)
    return [c for c in cases if rule(c)]


def apply_exec_policy(exec_cfg):
    """把执行配置里的策略字段**真正接线**，返回执行期需要的时间/条数策略。

    assets/intelligent_config.example.json 一直声明着 max_cases / request_interval_ms /
    retry_network_errors / minimize_findings，但此前没有任何代码读取它们——配置项形同虚设，
    用户以为设了限流与条数上限，实际全部按硬编码值跑。这里逐项落到真实执行路径上。
    """
    ex = (exec_cfg or {}).get("execution") or {}
    policy = {
        "max_cases": int(ex.get("max_cases") or 0),
        "request_interval_ms": int(ex.get("request_interval_ms") or 0),
        "cleanup_enabled": bool(ex.get("cleanup_enabled")),
    }
    retries = ex.get("retry_network_errors")
    if retries is not None:
        R.RETRY_COUNT = max(0, int(retries))
    if ex.get("minimize_findings") is not None:
        case_runner.MINIMIZE_ENABLED = bool(ex.get("minimize_findings"))
    return policy


def cap_cases(cases, policy):
    """按 max_cases 截断（0 表示不限）。截断必须**说出来**，不能静默少跑。"""
    limit = policy.get("max_cases") or 0
    if limit and len(cases) > limit:
        # 截断按优先级保留：不看 priority 的"前 N 条"会把 P0 全截掉，留下的是错误的子集
        rank = {"P0": 0, "P1": 1, "P2": 2}
        ranked = sorted(cases, key=lambda c: rank.get(str(c.get("priority") or "P2"), 2))
        print("⚠️ max_cases=%d 生效：按优先级保留 %d 条执行，%d 条未执行"
              "（提高 max_cases 或分批运行）" % (limit, limit, len(cases) - limit))
        return ranked[:limit]
    return cases


def order_cases_for_exec(cases):
    """执行顺序治理：限流观测置尾——连发可能触发限流，污染后续所有用例的判定。"""
    tail = [c for c in cases if (c.get("check") or "") == "rate_limit"]
    if not tail:
        return list(cases)
    return [c for c in cases if (c.get("check") or "") != "rate_limit"] + tail


def cleanup_step(exec_cfg, out_dir):
    """执行收尾的残留治理：清单必产（可见性优先），启用清理且提供删除 action 时真删除。

    concurrency / rate_limit / enum 遍历每轮会对写接口真实多次提交，
    残留若不可见，等于没有治理过。"""
    ex = (exec_cfg or {}).get("execution") or {}
    res = list(getattr(R, "CREATED_RESOURCES", []) or [])
    manifest = os.path.join(out_dir, "created_resources.json")
    with open(manifest, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    if not res:
        print("\n[清理] 本次执行未创建任何资源。")
        return
    delete_action = ex.get("cleanup_delete_action")
    if not (ex.get("cleanup_enabled") and delete_action):
        print("\n[清理] 残留 %d 条资源（清单: %s）；未启用自动清理"
              "（需 execution.cleanup_enabled + cleanup_delete_action）" % (len(res), manifest))
        return
    ok, fail = 0, []
    for item in res:
        _c, _r, parsed = R.call(R.build_payload(delete_action, id=item.get("id")))
        if isinstance(parsed, dict) and parsed.get("result") is True:
            ok += 1
        else:
            fail.append(item)
    print("\n[清理] 自动删除 %d/%d 条残留资源（删除 action=%s）%s"
          % (ok, len(res), delete_action,
             ("；失败 %d 条，见清单" % len(fail)) if fail else ""))


def write_regression_artifacts(out_dir, doc):
    """执行收尾产出回归基线快照（regression.py 接入主链路）。"""
    import regression as REG
    cases = doc.get("cases") or []
    chash = REG.contract_hash(doc.get("contract_model") or
                              {k: doc.get(k) for k in ("action", "coverage_plan") if k in doc})
    entries = [{"case_id": c.get("id", ""), "contract_hash": chash,
                "expectations": {"priority": c.get("priority", ""),
                                 "dimension": c.get("dimension", "")}}
               for c in cases]
    path = os.path.join(out_dir, "regression_baseline.json")
    REG.save_baseline(path, entries)
    print("回归基线 -> %s（%d 条快照，--retest 可做复测四态判定）" % (path, len(entries)))


def retest_report(baseline_path, results):
    """复测判定：FIXED / STILL_FAILING / REGRESSED / STALE_BASELINE / UNBASELINED / BLOCKED_ENV。"""
    import regression as REG
    return REG.retest_verdicts(REG.load_baseline(baseline_path), results)


def resolve_contract_from_kb(action, kb=None):
    """用 kb_load_contract 的能力按 action 在知识库里定位契约文件。"""
    try:
        import kb_load_contract as KB
    except Exception as e:
        print("无法导入 kb_load_contract：%s" % e)
        return None
    roots = [kb] if kb else list(KB.default_kb_candidates())
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        try:
            hits = KB.find_by_filesystem(root, str(action)) or []
        except Exception:
            hits = []
        if hits:
            return hits[0] if isinstance(hits[0], str) else str(hits[0])
    return None


def find_local_dep_contract(action, near=None):
    """在同级目录找该 action 的契约文件（"契约集放一起"是本 skill 推荐的用法）。

    只按文件名校验 action 号是否出现，与 kb_load_contract 的文件系统匹配同一标准，
    不做相似性推断。找不到返回 None。
    """
    if not action or not near:
        return None
    d = os.path.dirname(os.path.abspath(near))
    if not os.path.isdir(d):
        return None
    try:
        names = sorted(os.listdir(d))
    except Exception:
        return None
    for n in names:
        if n.lower().endswith(".md") and str(action) in n:
            return os.path.join(d, n)
    return None


def load_confirmed_runtime_deps(path):
    """加载**已确认**的运行实证依赖，供执行期做前置判定。

    只取 `status=confirmed`：候选是"模型说可能有依赖"，未经人核对就让它吞掉失败，
    等于把真缺陷从台账里静默移除。文件不存在 / 解析失败一律返回空列表——
    与不传该参数的行为完全一致，不阻断执行。
    """
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception as exc:
        sys.stderr.write("WARN: 运行实证依赖读取失败，本次不做前置判定：%s\n" % exc)
        return []
    return precondition_scan.confirmed_only(doc.get("dependencies") or [])


def ensure_runtime_dep_contracts(deps, out_dir, kb=None, near=None):
    """执行前确认已确认依赖的目标契约可用，并落盘清单。

    confirmed 依赖的 target_action 没有契约，Agent 构造前置请求时只能靠猜。
    解析顺序：① 主契约同级目录（"契约集放一起"是推荐用法，本地命中不查库）
    → ② 知识库按 action 定位。取不到的进 missing 点名留痕，不猜、不伪造、不阻断执行。
    无 confirmed 依赖时不产出任何文件，行为与未接入时完全一致（增量接入）。
    """
    if not deps:
        return None
    records, seen = [], set()
    for dep in deps:
        act = str(dep.get("target_action") or "").strip()
        if not act or act in seen:
            continue
        seen.add(act)
        local = find_local_dep_contract(act, near)
        path, origin = (local, "local") if local else (resolve_contract_from_kb(act, kb) or "", "kb")
        records.append({
            "target_action": act,
            "target_name": dep.get("target_name") or "",
            "subject_action": dep.get("subject_action") or "",
            "contract_path": path,
            "origin": origin if path else "",
        })
    if not records:
        return None
    missing = [r["target_action"] for r in records if not r["contract_path"]]
    local_n = len([r for r in records if r["origin"] == "local"])
    out = {"contracts": records, "missing": missing}
    path = os.path.join(out_dir, "runtime_dep_contracts.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("依赖契约清单 -> %s（本地 %d 条 / 知识库 %d 条 / 缺失 %d 条）"
          % (path, local_n, len(records) - len(missing) - local_n, len(missing)))
    return out


def reset_runtime():
    """清空一次执行的全局结果，避免同一进程多次运行相互污染。"""
    for name in ("results", "findings"):
        value = getattr(R, name, None)
        if isinstance(value, list):
            value.clear()
    if hasattr(R, "shallow_passes"):
        R.shallow_passes = 0
    if hasattr(R, "coverage_blocked"):
        R.coverage_blocked = False
    if hasattr(R, "CREATED_RESOURCES"):
        R.CREATED_RESOURCES.clear()
    R.COVERAGE_PLAN = []


def load_history(spec):
    """历史问题输入：可写成 CSV 路径，也可内联列表；用于风险排序（不单独作为硬断言）。"""
    if not spec:
        return []
    if isinstance(spec, str):
        if not os.path.isfile(spec):
            sys.stderr.write("历史问题文件不存在，已忽略：%s\n" % spec)
            return []
        with open(spec, "r", encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    if isinstance(spec, list):
        return spec
    return []


def resolve_inputs(exec_cfg, contract=None):
    """把执行配置 `inputs` 段接进流程。

    此前该段被整段忽略：连 `ui_baseline` 都没有传下去，导致**即使用户在配置里指了基线
    文件，plan_builder 依旧认为"缺少页面基线"而阻塞 AB 维度**——配了也白配。
    """
    ins = (exec_cfg or {}).get("inputs") or {}
    contracts = ins.get("contracts") or []
    chosen = contract or (contracts[0] if contracts else None)
    return {
        "contract": chosen,
        "ui_baseline": ins.get("ui_baseline"),
        "history": load_history(ins.get("historical_issues")),
    }


def build_intelligence_artifacts(contract_path, out_dir, action=None, history=None, ui_baseline=None):
    """构建统一的契约模型、关系、风险与可审计执行计划。"""
    model = load_contract_model(contract_path, action=action)
    model.relations = infer_relations(model)
    # 显式声明的跨接口依赖：只认契约正文白纸黑字写出的前置接口，不做相似性推断。
    # 追加进 relations（不改动上面推断出的操作类型关系），并单独落 dependencies 字段。
    dep_result = dependency_scan.annotate(model, contract_path)
    applicability = case_generator.judge(case_generator.annotate(case_generator.load_contract(contract_path, action=action)))
    assets = [register("contract", contract_path, trust="high")]
    if ui_baseline:
        assets.append(register("ui_baseline", ui_baseline, trust="medium"))
    risk = score_risk(model, history=history or [])
    plan = build_plan(model, applicability=applicability, risk=risk, assets=assets,
                      dependencies=dep_result.get("dependencies"),
                      dependency_unknowns=dep_result.get("unknowns"))
    plan["contract"] = model.to_dict()
    plan["contract"]["source"] = contract_path
    # 用 asdict 而非 __dict__：关系一旦带上 evidence（Evidence 对象）就不是
    # JSON 可序列化的了。历史实现因关系证据恒为空而从未暴露此问题。
    plan["relations"] = [asdict(r) for r in model.relations]
    plan["assets"] = [a.to_dict() for a in assets]
    os.makedirs(out_dir, exist_ok=True)
    plan_path = os.path.join(out_dir, "intelligent_test_plan.json")
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    dep_path = os.path.join(out_dir, "dependencies.json")
    with open(dep_path, "w", encoding="utf-8") as f:
        json.dump({"self_action": dep_result.get("self_action") or model.operation,
                   "contract": contract_path,
                   "dependencies": plan["dependencies"],
                   "unknowns": plan["dependency_unknowns"]},
                  f, ensure_ascii=False, indent=2)
    return {"model": model, "applicability": applicability, "risk": risk,
            "plan": plan, "plan_path": plan_path, "dependency_path": dep_path,
            "dependencies": plan["dependencies"]}


def main():
    ap = argparse.ArgumentParser(description="wjx-api-test 一键流水线")
    ap.add_argument("--contract", help="契约文档路径（ShowDoc md / OpenAPI json）")
    ap.add_argument("--action", help="action ID；与 --kb 配合从知识库定位契约")
    ap.add_argument("--kb", help="知识库根目录；省略时回退 KB_PATH 或 skill 同级目录的常见位置")
    ap.add_argument("--out-dir", default="api_test_out", help="产物目录")
    ap.add_argument("--execute", action="store_true", help="真实执行（默认只生成用例）")
    ap.add_argument("--plan-only", action="store_true", help="只生成智能计划和用例，不进入执行阶段")
    ap.add_argument("--dry-run", action="store_true", help="列出执行计划但不发起请求")
    ap.add_argument("--profile", choices=("plan", "smoke", "full", "exploratory", "discovery"), default=None,
                    help="执行档位；省略时取 --config 的 execution.profile → WJX_PROFILE，再回退 full。"
                         "discovery=发现档（生成期放宽探测广度：超长上限/载荷条数/字段族配额/AF 条数）")
    ap.add_argument("--config", help="可选执行配置 JSON")
    ap.add_argument("--adapter", choices=("rest", "sojump", "graphql", "grpc"), default=None,
                    help="适配器类型；省略时取 --config 的 adapter，再回退 sojump")
    ap.add_argument("--only", help="只跑指定维度，逗号分隔")
    ap.add_argument("--out-prefix", default="api_test", help="产物文件名前缀")
    ap.add_argument("--llm-facts", help="可选：大模型语义层事实 JSON（llm_semantics --mode prep 产出、"
                                        "经 --mode merge 校验后的那份）。不传则纯规则链路，行为不变")
    ap.add_argument("--runtime-deps", default="runtime_dependencies.json",
                    help="运行实证依赖记录（precondition_scan 产出）。只采信 status=confirmed "
                         "的记录做前置判定；文件不存在则不做任何前置判定，行为与不传时一致")
    ap.add_argument("--retest", help="对照回归基线做复测判定（传基线 JSON 路径）")
    args = ap.parse_args()

    exec_cfg = load_exec_config(args.config)
    # 档位必须在**生成之前**解析并激活：限额（超长上限/载荷条数/字段族配额）是
    # 生成期读取的，晚于生成再设置就等于没设。优先级：命令行 > 执行配置 > 环境变量 > full。
    profile = (args.profile or (exec_cfg.get("execution") or {}).get("profile")
               or discovery_profile.env_profile() or "full")
    discovery_profile.activate(profile if profile in ("conservative", "discovery") else "")
    if discovery_profile.is_discovery():
        print("发现档（discovery）：探测广度已放宽 → %s；断言口径与门禁不变（增量接入）。"
              % {k: discovery_profile.get(k) for k in (
                  "big_cap", "payload_per_field_per_category", "null_state_fields",
                  "url_fields", "ui_proxy_fields", "af_max_cases", "overlong_probe")})
    inputs = resolve_inputs(exec_cfg, args.contract)

    if not (args.contract or args.action or inputs["contract"]):
        ap.error("必须提供 --contract / --action，或在执行配置 inputs.contracts 中给出契约路径")

    contract_path = args.contract or inputs["contract"]
    if not contract_path and args.action:
        contract_path = resolve_contract_from_kb(args.action, args.kb)
        if not contract_path:
            sys.stderr.write("未在知识库中找到 action=%s 的契约；请改用 --contract 指定路径。\n" % args.action)
            sys.exit(2)
        print("从知识库定位契约：%s" % contract_path)
    elif not args.contract and inputs["contract"]:
        print("契约来自执行配置 inputs.contracts：%s" % contract_path)

    if not os.path.isfile(contract_path):
        sys.stderr.write(
            "契约文件不存在：%s\n"
            "  支持格式：ShowDoc Markdown(.md) / OpenAPI 3.x / Swagger 2.0(.json/.yaml/.yml)\n"
            "  也可改用 --action 从知识库定位，或在执行配置 inputs.contracts 中给出路径。\n"
            % contract_path)
        sys.exit(2)

    os.makedirs(args.out_dir, exist_ok=True)
    reset_runtime()
    if inputs["ui_baseline"]:
        # 让 case_runner 的基线加载器（ui_baseline.load）也能看到同一份基线：
        # 否则"配置里指了基线"与"执行时找不到基线"会各说各话，AB 依旧被阻塞。
        os.environ.setdefault("WJX_UI_BASELINE", str(inputs["ui_baseline"]))
    intelligence = build_intelligence_artifacts(
        contract_path, args.out_dir, action=args.action,
        history=inputs["history"], ui_baseline=inputs["ui_baseline"])
    print("智能计划：%s | 风险=%s(%s)" % (
        intelligence["plan_path"], intelligence["risk"]["score"], intelligence["risk"]["level"]))
    doc = case_generator.generate(contract_path, args.action, llm_facts=args.llm_facts)

    cases_path = os.path.join(args.out_dir, "generated_cases.json")
    with open(cases_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)

    print("=" * 70)
    print("接口：%s（action=%s）  操作类型=%s  国际版=%s" %
          (doc.get("name"), doc.get("action"), doc.get("op_type"), doc.get("is_intl")))
    print("生成用例 %d 条 -> %s" % (len(doc.get("cases") or []), cases_path))
    print("COVERAGE_PLAN（门禁必测）: %s" % ", ".join(doc.get("coverage_plan") or []))
    print("建议覆盖（选测）: %s" % ", ".join(doc.get("optional_plan") or []))
    print("命中缺陷模式 %d 条: %s" % (len(doc.get("matched_patterns") or []),
                                 ", ".join("%s(%d)" % (m["id"], m["cases"]) for m in doc.get("matched_patterns") or [])))
    print("=" * 70)

    # 人可读计划：执行前先让人看一眼，避免跑完才发现场景设计不对。
    # 在 plan 档位同样产出，这正是它最有用的场合。
    plan_md_path = os.path.join(args.out_dir, "plan.md")
    plan_md = plan_render.render_plan_md(
        intelligence["plan"], doc=doc, contract_path=contract_path,
        generated_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    with open(plan_md_path, "w", encoding="utf-8") as f:
        f.write(plan_md)
    pending = plan_render.collect_pending(intelligence["plan"], doc)
    print("人可读计划 -> %s（待确认问题 %d 项）" % (plan_md_path, len(pending)))
    print("显式依赖 %d 条 / 无法定位 %d 条 -> %s" % (
        len(intelligence["dependencies"]),
        len(intelligence["plan"].get("dependency_unknowns") or []),
        intelligence["dependency_path"]))

    # profile 已在生成之前解析并激活（见上），此处直接复用，不再重复解析。
    # 默认 sojump 是保守安全默认：让写操作门禁默认生效（写接口须显式声明 test 环境
    # 或 --adapter rest 才放行）。改成 rest 会削弱 sojump 写保护，勿轻易改。
    adapter_name = args.adapter or exec_cfg.get("adapter") or "sojump"
    if args.plan_only or profile == "plan" or not args.execute:
        print("\n未执行（计划模式，profile=%s）。用例已生成，可直接审阅或人工裁剪后再跑。"
              % profile)
        return

    adapter = build_adapter(adapter_name)
    allowed, why = write_gate(adapter, adapter_name, exec_cfg, doc.get("op_type"))
    if not allowed:
        sys.stderr.write("\n" + why + "\n")
        print("已保留生成的用例与智能计划；补齐执行配置后可加 --execute 重新运行。")
        return

    cases = select_by_profile(doc.get("cases") or [], profile)
    if args.only:
        wanted = {d.strip() for d in args.only.split(",") if d.strip()}
        cases = [c for c in cases if (c.get("dimension") or "") in wanted]
    policy = apply_exec_policy(exec_cfg)
    cases = cap_cases(cases, policy)
    cases = order_cases_for_exec(cases)
    R.COVERAGE_PLAN = doc.get("coverage_plan") or []
    R.CHECK_KEY_MISSING = doc.get("check_key") or {}
    # 【前置判定】注入已人工确认的运行实证依赖。文件不存在或全为候选时列表为空，
    # 此时不做任何前置判定，行为与接入前完全一致（增量接入）。
    R.CONFIRMED_PRECONDITION_DEPS = load_confirmed_runtime_deps(args.runtime_deps)
    if R.CONFIRMED_PRECONDITION_DEPS:
        print("已加载运行实证依赖 %d 条（将用于前置判定，命中者不计缺陷）"
              % len(R.CONFIRMED_PRECONDITION_DEPS))
        # 依赖契约就位检查：confirmed 的目标接口若缺契约，先看主契约同级目录，
        # 再从知识库按 action 取回路径，落盘 runtime_dep_contracts.json；
        # 取不到的点名留痕，不阻断执行。
        ensure_runtime_dep_contracts(R.CONFIRMED_PRECONDITION_DEPS,
                                     args.out_dir, kb=args.kb, near=contract_path)
    interval = policy.get("request_interval_ms") or 0
    print("\n适配器=%s 档位=%s 开始执行 %d 条用例%s..." % (
        adapter_name, profile, len(cases),
        "（间隔 %dms）" % interval if interval else ""))
    for idx, c in enumerate(cases):
        if idx and interval:
            time.sleep(interval / 1000.0)  # 尊重服务端限流：用例间留出间隔，避免把限流误判成缺陷
        case_runner.exec_case(c, dry=args.dry_run)
    if args.dry_run:
        print("\n[dry-run] 未发起任何请求。")
        return

    print("\n" + "#" * 70)
    counts = {}
    for r in R.results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("结果统计：%s" % counts)
    R.coverage_gate()
    R.check_key_gate()
    case_runner.write_reports(os.path.join(args.out_dir, args.out_prefix))

    # 【运行实证依赖发现】执行报告是唯一能看到"真实失败文案"的地方，就地筛一遍：
    # 命中白名单话术的失败会被整理成提示词，供大模型抽事实、机械校验后成为**候选**依赖。
    # 这里只产候选：不写知识库、不改变任何判定、不影响本轮结论——确认是显式动作。
    report_path = os.path.join(args.out_dir, "%s_report_full.json" % args.out_prefix)
    if os.path.isfile(report_path):
        try:
            with open(report_path, "r", encoding="utf-8") as f:
                report = json.load(f)
        except Exception:
            report = {}
        cands = precondition_scan.harvest(report or {}, subject_name=doc.get("name") or "")
        if cands:
            prompt_path = os.path.join(args.out_dir, "precondition_prompt.md")
            with open(prompt_path, "w", encoding="utf-8") as f:
                f.write(precondition_scan.build_prompt(cands, artifact=report_path))
            print("\n疑似「前置条件未满足」的失败 %d 条（**不是**缺陷，待确认后才会生效）:" % len(cands))
            for c in cands:
                print("  [%s] %s | 命中「%s」| %s" % (
                    c["case_no"], c["case_name"][:34], c["hint"], c["response_excerpt"][:60]))
            print("抽取提示词 -> %s" % prompt_path)
            print("确认流程：大模型按提示词产事实 → precondition_scan --merge <facts.json> "
                  "→ 核对后 --confirm 转正")

    csv_path = os.path.join(args.out_dir, "%s_issues.csv" % args.out_prefix)
    if os.path.isfile(csv_path):
        report_enhance.enhance(csv_path, os.path.join(args.out_dir, "%s_issues_enhanced" % args.out_prefix))

    cleanup_step(exec_cfg, args.out_dir)
    write_regression_artifacts(args.out_dir, doc)

    # 产物回读校验（交付前自检）。放在 report_enhance **之后**：增强会重写 CSV，
    # 放在它前面校验的是即将被替换的文件，等于校验了一个没人会拿到的产物。
    if not args.dry_run and os.path.isfile(csv_path):
        verify_artifacts(args.out_dir, csv_path)

    if args.retest:
        rows = []
        for r in R.results:
            resp = r.get("response") if isinstance(r.get("response"), dict) else {}
            rows.append({"case_id": r.get("case_no"), "passed": r.get("status") == "PASS",
                         "error": str(resp.get("note") or "")})
        verdicts = retest_report(args.retest, rows)
        vc = {}
        for v in verdicts:
            vc[v["verdict"]] = vc.get(v["verdict"], 0) + 1
        print("复测判定：%s" % vc)
        with open(os.path.join(args.out_dir, "retest_verdicts.json"), "w", encoding="utf-8") as f:
            json.dump(verdicts, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
