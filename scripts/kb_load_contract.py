#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kb_load_contract.py — 从 L5 知识库(user_knowledge) 按 action ID / 关键词载入接口契约。

【适用范围】仅适用于含以下结构的知识库：
  <KB_ROOT>/project/showdoc_api_docs/.../*.md    （接口文档 markdown）
  <KB_ROOT>/project/_vector_index/meta.json       （向量索引，可选兜底）
若你的知识库不是此结构（如纯文件目录、Confluence、Swagger JSON 等），
请手动粘贴契约文档，本脚本不适用。
示例知识库：qa-req2testcase-generator 的 user_knowledge 目录（常见位于与本 skill 同级目录：`<skill父目录>/qa-req2testcase-generator/user_knowledge`，可直接作为 `--kb` 参数值）。

检索策略（均不依赖 numpy，纯标准库）：
  1) 文件系统匹配：在 project/showdoc_api_docs 下查找文件名含 [<action>] 或 <keyword> 的 *.md
  2) 兜底：读取 project/_vector_index/meta.json（JSON 数组），模糊匹配 title/rel_path/text 含关键词的条目

用法：
  python kb_load_contract.py --kb <KB_ROOT> --action 1000107
  python kb_load_contract.py --kb <KB_ROOT> --keyword "AI主页" --out contract.md
  python kb_load_contract.py --kb <KB_ROOT> --action 1000108 --no-print   # 只列路径
  python kb_load_contract.py --kb <KB_ROOT> --action 1000107 --endpoint    # 抽取文档里的『接口地址』
  python kb_load_contract.py --action 1000107   # 省略 --kb：自动回退到与 skill 同级目录的 qa-req2testcase-generator/user_knowledge
"""
import argparse
import json
import os
import re
import sys


def find_by_filesystem(kb_root, token):
    """在 project/showdoc_api_docs 下按文件名/路径匹配 token。"""
    hits = []
    base = os.path.join(kb_root, "project", "showdoc_api_docs")
    if not os.path.isdir(base):
        return hits
    token_l = token.lower()
    for root, _dirs, files in os.walk(base):
        for f in files:
            if not f.lower().endswith(".md"):
                continue
            if token_l in f.lower():
                hits.append(os.path.join(root, f))
    return sorted(set(hits))


def find_by_vector_index(kb_root, token):
    """兜底：读 meta.json，按 title/rel_path/text 模糊匹配。返回 (rel_path, title) 列表。"""
    hits = []
    meta_path = os.path.join(kb_root, "project", "_vector_index", "meta.json")
    if not os.path.isfile(meta_path):
        return hits
    try:
        with open(meta_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return hits
    if not isinstance(data, list):
        return hits
    token_l = token.lower()
    seen = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", ""))
        rel_path = str(item.get("rel_path", ""))
        text = str(item.get("text", ""))
        hay = (title + " " + rel_path + " " + text[:2000]).lower()
        # 仅关注接口文档来源
        if "showdoc_api_docs" not in rel_path:
            continue
        if token_l in hay:
            key = rel_path or title
            if key in seen:
                continue
            seen.add(key)
            hits.append((rel_path, title))
    return hits


def extract_endpoints(md_text):
    """从契约 markdown 提取『接口地址』实际 URL。

    策略：
      1) 命中形如 `# 接口地址` / `## 请求地址` 的标题行，取其行 + 后续 3 行内的 URL；
      2) 命中「生产环境：https://...」这类「环境名：URL」写法。
    不抓取文档来源(showdoc.com.cn)与示例载荷(example.wjx.cn)等无关 URL。
    """
    eps = []
    heading_re = re.compile(
        r'^#{1,6}\s*(接口地址|请求地址|请求路径|API地址|接口URL|请求URL|调用地址)', re.I)
    url_re = re.compile(r'https?://[^\s`\'"<>）)】]+')
    env_url_re = re.compile(
        r'(生产环境|测试环境|预发布环境|沙箱环境|联调环境|开发环境|正式环境)[：:]\s*`?'
        r'(https?://[^\s`\'"<>]+)', re.I)
    lines = md_text.splitlines()
    for i, line in enumerate(lines):
        if heading_re.match(line.strip()):
            window = lines[i:i + 4]
            for w in window:
                eps.extend(url_re.findall(w))
        for m in env_url_re.findall(line):
            eps.append(m[1])
    # 去重并保持顺序
    seen = set()
    out = []
    for e in eps:
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out


def default_kb_candidates():
    """未传 --kb 时的默认回退候选列表：与本 skill 同级目录下的 qa-req2testcase-generator/user_knowledge。

    脚本路径：<skill>/scripts/kb_load_contract.py
      parents: scripts(0) -> <skill>(1) -> <skill父目录>(2)
    由于 skill 可能经「真实目录」或「目录 junction」两种路径被调用，分别基于
    abspath 与 realpath 计算候选，取第一个实际存在的目录，保证两种入口都能命中。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    skill_parent = os.path.dirname(os.path.dirname(here))
    cand1 = os.path.join(skill_parent, "qa-req2testcase-generator", "user_knowledge")
    real_skill_parent = os.path.dirname(os.path.dirname(os.path.realpath(here)))
    cand2 = os.path.join(real_skill_parent, "qa-req2testcase-generator", "user_knowledge")
    cands = [cand1]
    if os.path.normpath(cand2) != os.path.normpath(cand1):
        cands.append(cand2)
    return cands


def main():
    ap = argparse.ArgumentParser(description="从 L5 知识库载入接口契约文档")
    ap.add_argument("--kb", help="user_knowledge 根目录绝对路径（可省略：省略时依次回退到 KB_PATH 环境变量、再回退到与 skill 同级目录的 qa-req2testcase-generator/user_knowledge）")
    ap.add_argument("--action", help="动作 ID，如 1000107")
    ap.add_argument("--keyword", help="关键词，如 'AI主页' 或 '创建AI主页'")
    ap.add_argument("--out", help="将命中文档内容拼接写入此文件（可选）")
    ap.add_argument("--no-print", action="store_true", help="只打印命中路径，不打印正文")
    ap.add_argument("--endpoint", action="store_true",
                    help="额外提取契约里的『接口地址』实际 URL 并打印（每行 ENDPOINT: <url>）")
    args = ap.parse_args()

    token = (args.action or args.keyword or "").strip()
    if not token:
        sys.stderr.write("错误：必须提供 --action 或 --keyword\n")
        sys.exit(2)

    # 解析 KB 根目录：显式 --kb > KB_PATH 环境变量 > 与 skill 同级目录的默认回退
    kb_root = args.kb
    src = "--kb 参数"
    if not kb_root:
        env_kb = os.environ.get("KB_PATH")
        if env_kb:
            kb_root = env_kb
            src = "KB_PATH 环境变量"
    if not kb_root:
        src = "默认回退（与 skill 同级目录的 qa-req2testcase-generator/user_knowledge）"
        for c in default_kb_candidates():
            if os.path.isdir(c):
                kb_root = c
                break
        else:
            kb_root = default_kb_candidates()[0]  # 仅用于报错展示

    if not os.path.isdir(kb_root):
        sys.stderr.write("错误：KB 根目录不存在: %s\n" % kb_root)
        sys.stderr.write("提示：可用 --kb <路径> 显式指定，或设置 KB_PATH 环境变量。\n")
        sys.exit(2)
    print("== 使用 KB 根目录（来源：%s）: %s ==" % (src, kb_root))

    # 1) 优先文件系统精确匹配
    fs_hits = find_by_filesystem(kb_root, token)
    fs_norm = {os.path.normpath(p) for p in fs_hits}

    # 2) 向量索引兜底（补充文件系统未命中项）
    vi_hits = find_by_vector_index(kb_root, token)
    vi_paths = []
    base = os.path.join(kb_root, "project", "showdoc_api_docs")
    for rel_path, _title in vi_hits:
        # rel_path 形如 showdoc_api_docs/... 或 project/showdoc_api_docs/...
        cand = os.path.join(kb_root, rel_path)
        if not os.path.isfile(cand):
            cand = os.path.join(kb_root, "project", rel_path)
        if os.path.isfile(cand) and os.path.normpath(cand) not in fs_norm:
            vi_paths.append(cand)

    all_hits = sorted(set(os.path.normpath(p) for p in (fs_hits + vi_paths)))

    print("== 命中契约文档 (%d) ==" % len(all_hits))
    for p in all_hits:
        print("  " + p)

    if args.endpoint:
        print("\n== 契约内『接口地址』抽取 ==")
        for p in all_hits:
            try:
                with open(p, "r", encoding="utf-8") as sf:
                    eps = extract_endpoints(sf.read())
            except Exception:
                eps = []
            if eps:
                for e in eps:
                    print("  ENDPOINT: %s   (来自 %s)" % (e, os.path.basename(p)))
            else:
                print("  （%s 未检出接口地址字段）" % os.path.basename(p))

    if args.no_print:
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                for p in all_hits:
                    fh.write("# %s\n\n" % os.path.basename(p))
                    with open(p, "r", encoding="utf-8") as sf:
                        fh.write(sf.read())
                    fh.write("\n\n")
            print("== 已写出: %s ==" % args.out)
        return

    # 打印正文
    chunks = []
    for p in all_hits:
        try:
            with open(p, "r", encoding="utf-8") as sf:
                body = sf.read()
        except Exception as e:
            body = "[读取失败: %s]" % e
        chunks.append("# %s\n\n%s" % (os.path.basename(p), body))

    full = "\n\n---\n\n".join(chunks)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(full)
        print("\n== 已写出: %s ==" % args.out)
    else:
        print("\n========== 契约正文 ==========\n")
        print(full)


if __name__ == "__main__":
    main()
