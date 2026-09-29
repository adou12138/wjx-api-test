# -*- coding: utf-8 -*-
"""
personal_config.py — 个人参数配置加载（wjx-api-test skill 共享模块）

被 api_test_runner.py 与 smoke_runner.py 共用，保证「个人参数单一来源」。
个人参数（appid / 账号 / 域名 / corpid / sign / sysid）一律外置，便于 skill 分享给团队其他成员。

优先级：环境变量 > 本地 personal_config.json > 空（由调用方校验报错）。
配置模板：assets/personal_config.example.json（占位符，随 skill 分享，不含真实数据）。
真实配置放 skill 包外：~/.workbuddy/wjx-api-test/personal_config.json，或用 WJX_CONFIG 指定路径。

冒烟配置（smoke_config_*.json）与生成用例（case_generator 越权矩阵）里用占位符承载个人参数，
运行时由 resolve_placeholders() 统一替换为 personal_config 的真实值，避免各模块重复写死个人数据。
占位符支持两种写法：① 简短形式 <APPID>/<主账号>/<子账号>/<跨企业appid>/<CORPID>/<SYSID>/<域名>
（smoke_config_wjx.json 等旧配置仍用）；② 自带中文接口参数含义的长串形式
（<appid 应用ID：...> 等，smoke_config.example.json 用）。两者都解析为同一字段。
"""
import json
import os
from urllib.parse import urlparse

ENCODE = "sha1"
DEFAULT_SIGN = "1234"  # 约定"不校验"时的样板签名值；可在配置/环境变量 WJX_SIGN 覆盖

# 个人参数字段 -> 覆盖它的环境变量名
ENV_MAP = {
    "base_url": "WJX_BASE_URL",
    "appid": "WJX_APPID",
    "sign": "WJX_SIGN",
    "main_account": "WJX_MAIN",
    "sub_account": "WJX_SUB",
    "corpid": "WJX_CORPID",
    "cross_appid": "WJX_CROSS_APPID",
    "sysid": "WJX_SYSID",
}


def _default_config_path():
    return os.path.join(
        os.path.expanduser("~"), ".workbuddy", "wjx-api-test", "personal_config.json"
    )


def _filter_region(envs, region):
    """按 region 过滤环境（如 cn=国内版 / intl=国际版），支持任意多套环境。
    region 为空则不过滤；过滤后为空时打印警告并回退全部环境。"""
    if not region:
        return envs
    matched = [e for e in envs if isinstance(e, dict) and e.get("region") == region]
    if not matched:
        known = sorted({str(e.get("region")) for e in envs if isinstance(e, dict) and e.get("region")})
        print("警告：配置里没有 region=%s 的环境（现有 region：%s），改为在全部环境中选择。" % (region, ", ".join(known) or "未标注"))
        return envs
    return matched


def _select_env(envs, sel):
    """从 envs 数组里选一个环境。sel 可为 name 或数字索引；找不到时回退 envs[0]。"""
    if not isinstance(envs, list) or not envs:
        return {}
    if sel:
        for _e in envs:
            if isinstance(_e, dict) and _e.get("name") == sel:
                return _e
        try:
            return envs[int(sel)]
        except (ValueError, IndexError):
            pass
    return envs[0]


def load_personal_config(path=None, env=None, region=None):
    """加载个人参数。优先级：环境变量 > 本地 personal_config.json > 空（由调用方校验报错）。

    支持多环境结构（任意多套环境，可按 region 分组，如国内版 cn 2 套 + 国际版 intl 2 套）：
        {"default_env": "x", "envs": [{"name": "x", "region": "cn", ...}, ...], ...}
    环境选择：env 参数（如 smoke_runner 的 --env）或环境变量 WJX_ENV > 顶层 default_env > 候选中第一个。
    region 过滤：region 参数或环境变量 WJX_REGION（如 cn / intl）先筛出该 region 的环境，
    再在筛选结果内按 name 选择；跨 region 的参数不会被加载（防止误用别环境参数）。
    顶层（envs/default_env 之外）的字段视为各环境共享的公共字段，仅在所选环境块未定义时才回退。
    兼容旧的单环境扁平结构（无 envs 键，直接 update）。

    Args:
        path:   显式指定配置文件路径（如 smoke_runner 的 --personal）。
                为 None 时依次取环境变量 WJX_CONFIG、默认路径。
        env:    指定环境名（name）/索引；None 时由 WJX_ENV 决定。
        region: 指定 region 过滤（如 "cn" / "intl"）；None 时由 WJX_REGION 决定。
    """
    cfg = {}
    cfg_path = path or os.environ.get("WJX_CONFIG") or _default_config_path()
    raw = {}
    if cfg_path and os.path.isfile(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as _f:
                raw = json.load(_f) or {}
        except Exception as _e:
            print("警告：读取个人配置文件 %s 失败：%s" % (cfg_path, _e))
            raw = {}

    if isinstance(raw, dict) and isinstance(raw.get("envs"), list) and raw["envs"]:
        region = region or os.environ.get("WJX_REGION")
        candidates = _filter_region(raw["envs"], region)
        sel = env or os.environ.get("WJX_ENV") or raw.get("default_env")
        chosen = _select_env(candidates, sel)
        # 顶层公共字段：作为环境块未定义项的回退
        for _k, _v in raw.items():
            if _k in ("envs", "default_env"):
                continue
            cfg.setdefault(_k, _v)
        if isinstance(chosen, dict):
            cfg.update(chosen)
        # 运行时可见的所选环境元信息（便于日志/产物标注是哪套环境）
        if isinstance(chosen, dict) and chosen.get("name"):
            cfg.setdefault("_selected_env", chosen.get("name"))
            if chosen.get("region"):
                cfg["_selected_region"] = chosen.get("region")
    else:
        cfg.update(raw)

    for _k, _ev in ENV_MAP.items():
        if os.environ.get(_ev):
            cfg[_k] = os.environ[_ev]
    return cfg


def load_cross_env(path=None, env=None, region=None):
    """【异常场景专用】跨环境参数加载（如跨企业/跨租户隔离用例需要另一环境的参数）。

    纪律：正常用例只使用 load_personal_config() 选定的本环境参数；
    仅当确需跨环境（异常注入、越权隔离验证等）时，才显式调用本函数，
    返回值带 _cross_env 标记以便产物里审计。深度测试对应 WJX_CROSS_ENV 环境变量，
    冒烟对应 --cross-env <环境name>。
    """
    cfg = load_personal_config(path, env=env, region=region)
    cfg["_cross_env"] = env or os.environ.get("WJX_ENV") or ""
    return cfg


# 配置与用例里的个人占位符 -> personal_config 字段
# 支持两种写法：① 简短占位符（<APPID> 等，旧配置 smoke_config_wjx.json 仍用）；
#             ② 自带中文接口参数含义的占位符（<appid 应用ID：...> 等，smoke_config.example.json 用）。
# 两者都解析为同一字段，保证旧配置不失效、新配置自解释。
# 生成用例（case_generator 的越权矩阵）同样用这套占位符承载身份——包内零个人数据，
# 换环境只换配置，用例不变。
_PH_MAP = {
    # ① 简短形式（向后兼容）
    "<APPID>": "appid",
    "<主账号>": "main_account",
    "<子账号>": "sub_account",
    "<跨企业appid>": "cross_appid",
    "<CORPID>": "corpid",
    "<SYSID>": "sysid",
    # ② 自带中文接口参数含义的形式
    "<appid 应用ID：所有请求的公共参数 appid，唯一标识你的企业/应用>": "appid",
    "<creater_main 主账号：请求参数 creater/creater_main 下发的创建者主账号登录名>": "main_account",
    "<creater_sub 子账号：请求参数 creater_sub 下发的创建者子账号登录名>": "sub_account",
    "<corpid 企业ID：公共/业务参数 corpid，标识企业主体>": "corpid",
    "<sysid 系统ID：业务参数 sysid，仅部分接口需要>": "sysid",
}


def resolve_placeholders(cfg, personal):
    """把配置/用例里的个人占位符替换为 personal_config 的真实值，返回新的 dict（不修改入参）。

    支持的占位符：简短形式 <APPID> <主账号> <子账号> <跨企业appid> <CORPID> <SYSID> <域名>，
    或自带中文接口参数含义的长串形式（<appid 应用ID：...> 等，见 smoke_config.example.json）。
    <域名> 由 personal_config.base_url 解析（取 netloc，如 https://x.sojump.cn/... -> x.sojump.cn）。
    找不到真实值时保留占位符，由调用方在缺失校验里报错（不静默替换成空串）。
    """
    if not isinstance(personal, dict):
        return cfg

    repl = {}
    for ph, field in _PH_MAP.items():
        val = personal.get(field)
        if val:
            repl[ph] = str(val)

    bu = personal.get("base_url", "")
    if bu:
        netloc = urlparse(bu).netloc
        if netloc:
            repl["<域名>"] = netloc
            repl["<域名：接口根域名，由 personal_config.base_url 解析，如 your-env.sojump.cn>"] = netloc

    if not repl:
        return cfg

    def _walk(v):
        if isinstance(v, str):
            out = v
            for ph, val in repl.items():
                out = out.replace(ph, val)
            return out
        if isinstance(v, dict):
            return {k: _walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [_walk(x) for x in v]
        return v

    return _walk(cfg)


def config_consistency_check(personal, cross_env_requested=False, cross_cfg=None, *, strict=False):
    """【防呆·矛盾拦截】交叉校验个人参数配置的内部矛盾，返回问题列表。

    返回 [(level, msg), ...]，level ∈ {"fatal","warn"}。
    - fatal：必须拦截（调用方应 sys.exit(2) 或禁止运行）；
    - warn：提示性矛盾（strict=True 时升级为拦截，否则仅打印警告）。

    检测项：
      1) region 矛盾：环境变量 WJX_REGION 与所选环境块的 region 不一致（跨 region 参数不应进入本次运行）。
      2) 跨环境参数请求但未加载：设了 WJX_CROSS_ENV/--cross-env 但跨环境配置为空（异常用例无法取参，禁止运行）。
      3) 域名 vs region 启发式：region=intl 却用国内域名 / region=cn 却用国际域名（疑似环境/域不匹配）。

    被 api_test_runner.py 与 smoke_runner.py 共用，保证"配置矛盾"在启动阶段被拦下，
    不让错误参数静默跑完全程、产出一份看似正常却方向错的报告。
    """
    issues = []
    if not isinstance(personal, dict):
        return issues
    env_region = os.environ.get("WJX_REGION")
    sel_region = personal.get("_selected_region")
    if env_region and sel_region and env_region != sel_region:
        issues.append(("fatal",
            "region 选择矛盾：环境变量 WJX_REGION=%s，但所选环境块 region=%s，二者不一致"
            "（跨 region 参数不应进入本次运行，请统一 WJX_REGION 与环境配置的 region）。"
            % (env_region, sel_region)))
    if cross_env_requested and not cross_cfg:
        issues.append(("fatal",
            "跨环境参数请求但未加载：已设置 WJX_CROSS_ENV/--cross-env，但跨环境配置为空，"
            "跨企业/跨租户等异常用例无法取参，禁止运行"
            "（请确认 WJX_CROSS_ENV/WJX_CROSS_CONFIG 指向有效环境）。"))
    bu = personal.get("base_url", "")
    if bu and sel_region:
        host = urlparse(bu).netloc.lower()
        intl_look = ("intl" in host) or host.endswith(".wjx.com") or host.endswith(".sojump.com")
        cn_look = host.endswith(".sojump.cn") or host.endswith(".wjx.cn")
        if sel_region == "intl" and cn_look and not intl_look:
            issues.append(("warn",
                "region=intl 但 base_url 域名 %s 看似国内域名，疑似环境/域不匹配"
                "（如需测国际版请核对接口地址）。" % host))
        elif sel_region == "cn" and intl_look and not cn_look:
            issues.append(("warn",
                "region=cn 但 base_url 域名 %s 看似国际域名，疑似环境/域不匹配。" % host))
    return issues
